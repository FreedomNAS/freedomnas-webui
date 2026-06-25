"""
/ws/updates/terminal — WebSocket endpoint that streams topgrade output.

Behaviour (spec §7.7):
- Command retrieved from engine profile via get_command("updates", "run_topgrade")
- Pre-update snapshot taken if auto_snapshot is enabled
- asyncio.create_subprocess_exec used (never subprocess.run, never shell=True)
- Output streamed line-by-line as JSON: {"line": "...", "stream": "stdout"|"stderr"}
- {"exit_code": N} sent as the final message when the process finishes
"""

import asyncio
import json
import shlex
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from api.deps import get_config, get_engine

router = APIRouter()


async def _send(ws: WebSocket, stream: str, line: str) -> None:
    await ws.send_text(json.dumps({"stream": stream, "line": line}))


@router.websocket("/ws/updates/terminal")
async def updates_terminal(websocket: WebSocket):
    await websocket.accept()
    engine = get_engine()
    config = get_config()

    try:
        command = engine.get_command("updates", "run_topgrade")
    except CommandTranslationError as exc:
        await _send(websocket, "stderr", f"Profile error: {exc}")
        await websocket.close(code=1011)
        return

    if isinstance(command, list):
        await _send(websocket, "stderr", "run_topgrade must be a single command, not a list")
        await websocket.close(code=1011)
        return

    # Pre-update snapshot (spec §7.7)
    if config.get("updates.topgrade.auto_snapshot", True):
        snap_name = f"pre-update-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            pools = config.get_pools()
            for pool in pools:
                dataset = pool.get("name", "")
                if dataset:
                    engine.execute("zfs", "snapshot_create", dataset=dataset, snap_name=snap_name)
                    await _send(websocket, "stdout", f"Snapshot created: {dataset}@{snap_name}")
        except (CommandExecutionError, CommandTranslationError) as exc:
            await _send(websocket, "stderr", f"Warning: snapshot failed: {exc}")

    # Launch topgrade via asyncio (spec §7.7 — no shell=True, no subprocess.run)
    args = shlex.split(command)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        await _send(websocket, "stderr", f"Failed to start process: {exc}")
        await websocket.close(code=1011)
        return

    async def stream_pipe(pipe, stream_name: str):
        async for raw in pipe:
            line = raw.decode(errors="replace").rstrip()
            try:
                await _send(websocket, stream_name, line)
            except WebSocketDisconnect:
                return

    await asyncio.gather(
        stream_pipe(proc.stdout, "stdout"),
        stream_pipe(proc.stderr, "stderr"),
    )
    exit_code = await proc.wait()

    try:
        await websocket.send_text(json.dumps({"exit_code": exit_code}))
        await websocket.close()
    except WebSocketDisconnect:
        pass
