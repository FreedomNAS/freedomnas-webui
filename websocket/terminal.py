"""
/ws/updates/terminal — WebSocket endpoint that streams topgrade output.

The client authenticates by sending {"auth": "<base64 user:password>"} as its
first frame — the same Basic blob the REST calls carry, since a browser
cannot set an Authorization header on a WebSocket. Nothing runs before that
check passes: this endpoint starts a system update.

The authenticated name is also what the update runs as. topgrade refuses to
run as root by design — it calls sudo itself for the steps that need it — so
the profile wraps it in `su - {username}`, and setup grants that user's group
passwordless sudo.

Behaviour (spec §7.7):
- Command retrieved from engine profile via get_command("updates", "run_topgrade")
- Pre-update snapshot taken if auto_snapshot is enabled
- asyncio.create_subprocess_exec used (never subprocess.run, never shell=True)
- Output streamed line-by-line as JSON: {"line": "...", "stream": "stdout"|"stderr"}
- {"exit_code": N} sent as the final message when the process finishes
"""

import asyncio
import base64
import binascii
import json
import shlex
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from api.deps import get_config, get_engine, verify_credentials

AUTH_TIMEOUT_SECONDS = 10

router = APIRouter()


async def _send(ws: WebSocket, stream: str, line: str) -> None:
    await ws.send_text(json.dumps({"stream": stream, "line": line}))


async def _authenticate_socket(websocket: WebSocket):
    """
    Read the opening frame and check it against PAM.

    Returns the username, or None once the socket has been closed.
    """
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS
        )
        blob = json.loads(raw).get("auth", "")
        username, _, password = base64.b64decode(blob).decode().partition(":")
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError,
            binascii.Error, UnicodeDecodeError):
        await _send(websocket, "stderr", "Authentication required")
        await websocket.close(code=4401)
        return None

    if not username or not verify_credentials(username, password):
        await _send(websocket, "stderr", "Invalid credentials")
        await websocket.close(code=4401)
        return None

    return username


@router.websocket("/ws/updates/terminal")
async def updates_terminal(websocket: WebSocket):
    await websocket.accept()

    username = await _authenticate_socket(websocket)
    if username is None:
        return

    engine = get_engine()
    config = get_config()

    try:
        command = engine.get_command("updates", "run_topgrade", username=username)
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
