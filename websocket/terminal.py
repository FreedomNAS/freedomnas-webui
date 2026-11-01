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
- Pre-update snapshot, recursive, of the root pool and every recorded pool,
  if auto_snapshot is enabled
- asyncio.create_subprocess_exec used (never subprocess.run, never shell=True)
- stdin is /dev/null: nobody can answer a prompt here, so a command that
  asks for one has to fail instead of hanging the page forever
- Output streamed line-by-line as JSON: {"line": "...", "stream": "stdout"|"stderr"}
- {"restart_needed": bool, "reboot_needed": bool} once it finishes: whether
  FreedomNAS itself or the kernel changed, for the page's notices
- {"exit_code": N} sent as the final message when the process finishes
"""

import asyncio
import base64
import binascii
import json
import os
import shlex
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import bootenv, bootstate
from api import restart
from api.deps import get_config, get_engine, is_administrator, verify_credentials
from api.routes import plugins
from api.routes.updates import DEFAULT_STEPS, write_topgrade_config

AUTH_TIMEOUT_SECONDS = 10

router = APIRouter()

running = 0


# What a closed socket raises. Starlette reports WebSocketDisconnect when it
# sees the close frame, but uvicorn raises RuntimeError if something is written
# after the close has gone out — which is what a browser tab closed mid-update
# produces, and it used to escape as a 500 in the server log.
CLOSED = (WebSocketDisconnect, RuntimeError)


async def _send(ws: WebSocket, stream: str, line: str) -> bool:
    """Send a line. Returns False once the socket is gone."""
    try:
        await ws.send_text(json.dumps({"stream": stream, "line": line}))
        return True
    except CLOSED:
        return False


def _pools_to_snapshot(engine, config) -> list:
    """
    The pool the system runs from, then every recorded pool. The system
    lives in <pool>/ROOT/<edition>, below the pool's top dataset, so the
    snapshot is recursive: that is what zz-dracut-uki pairs with the
    rotated-out UKI. A system set up from an image does not record its root
    pool, so it is asked for rather than read from the configuration.
    """
    pools = []
    try:
        root, _ = engine.execute("zfs", "dataset_at_path", path="/", check=False)
        if root.strip():
            pools.append(root.strip().split("/", 1)[0])
    except (CommandExecutionError, CommandTranslationError):
        pass
    for pool in config.get_pools():
        name = pool.get("name", "")
        if name and name not in pools:
            pools.append(name)
    return pools


async def _close(ws: WebSocket, code: int = 1000) -> None:
    """Close the socket, tolerating one the client already closed."""
    try:
        await ws.close(code=code)
    except CLOSED:
        pass


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
        await _close(websocket, 4401)
        return None

    if not username or not verify_credentials(username, password):
        await _send(websocket, "stderr", "Invalid credentials")
        await _close(websocket, 4401)
        return None

    # This socket runs a system update as that user: the HTTP routes are not
    # the only door that has to ask whether they may administer the machine.
    if not is_administrator(username):
        await _send(websocket, "stderr", "This account is not an administrator")
        await _close(websocket, 4403)
        return None

    return username


async def _run_streamed(websocket: WebSocket, args) -> Optional[int]:
    """Run one command (spec §7.7: never shell=True), its output streamed line by line."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        await _send(websocket, "stderr", f"Failed to start process: {exc}")
        return None

    async def stream_pipe(pipe, stream_name: str):
        """
        Drain the pipe even after the client is gone: the update keeps running
        either way, and a full pipe would block the package manager halfway
        through a transaction.
        """
        connected = True
        async for raw in pipe:
            if not connected:
                continue
            line = raw.decode(errors="replace").rstrip()
            connected = await _send(websocket, stream_name, line)

    await asyncio.gather(
        stream_pipe(proc.stdout, "stdout"),
        stream_pipe(proc.stderr, "stderr"),
    )
    return await proc.wait()


async def _update_live(websocket: WebSocket, engine, command: str, snap_name: str) -> Optional[int]:
    """topgrade on the running system, the boot image (or system) kept first where the edition can."""
    if snap_name:
        try:
            kept = await asyncio.to_thread(bootenv.create_before_update, engine, snap_name[len("pre-update-"):])
            if kept:
                await _send(websocket, "stdout", f"Boot environment created: {kept} (the system as it is now)")
        except (CommandExecutionError, CommandTranslationError) as exc:
            await _send(websocket, "stderr", f"Warning: the boot environment was not created: {exc}")

    try:
        stdout, _ = engine.execute("bootloader", "uki_backup", snapshot=snap_name)
        for line in stdout.splitlines():
            await _send(websocket, "stdout", line)
    except CommandTranslationError:
        pass
    except CommandExecutionError as exc:
        await _send(websocket, "stderr", f"Warning: the boot image was not backed up: {exc}")

    return await _run_streamed(websocket, shlex.split(command))


_COPY_RESULTS = {
    "trial": "The update is ready in {be}. Restart to use it: if the updated system does not finish "
             "starting, the start after it goes back to the system as it is now, by itself.",
    "default": "The update is ready in {be}, which starts from the next restart. The system as it is "
               "now stays under System > Previous systems.",
    "unchanged": "Nothing to update: the system is up to date.",
    "failed": "The update did not complete: its copy was dropped, and the running system was never touched.",
}


async def _update_in_copy(websocket: WebSocket, engine, config, stamp: str) -> Optional[int]:
    """
    The update goes into a new boot environment while the running system
    stays as it is (bootenv.py); the copy starts on trial at the next restart.
    """
    await _send(websocket, "stdout", "Making a copy of the system to install the update into...")
    try:
        update = await asyncio.to_thread(bootenv.begin_update, engine, stamp)
    except (CommandExecutionError, CommandTranslationError) as exc:
        await _send(websocket, "stderr", f"Could not prepare the copy: {exc}")
        return 1
    await _send(websocket, "stdout", f"Copy {update['be']} ready at {update['root']}: the running system is not touched")

    succeeded = True
    for command in bootenv.update_commands(engine, update["root"]):
        code = await _run_streamed(websocket, shlex.split(command))
        if code != 0:
            await _send(websocket, "stderr", f"This step ended with exit code {code}")
            succeeded = False
            break

    try:
        outcome = await asyncio.to_thread(bootenv.finish_update, engine, config, update, succeeded)
    except (CommandExecutionError, CommandTranslationError) as exc:
        await _send(websocket, "stderr", f"Could not finish the copy {update['be']}: {exc}")
        return 1
    stream = "stderr" if outcome["result"] == "failed" else "stdout"
    await _send(websocket, stream, _COPY_RESULTS[outcome["result"]].format(be=outcome["be"]))
    return 0 if succeeded else 1


@router.websocket("/ws/updates/terminal")
async def updates_terminal(websocket: WebSocket):
    await websocket.accept()

    username = await _authenticate_socket(websocket)
    if username is None:
        return

    if plugins.running():
        await _send(websocket, "stderr", "A module is being installed or removed: start the update once it has finished")
        await _close(websocket, 1013)
        return

    engine = get_engine()
    config = get_config()

    try:
        command = engine.get_command("updates", "run_topgrade", username=username)
    except CommandTranslationError as exc:
        await _send(websocket, "stderr", f"Profile error: {exc}")
        await _close(websocket, 1011)
        return

    if isinstance(command, list):
        await _send(websocket, "stderr", "run_topgrade must be a single command, not a list")
        await _close(websocket, 1011)
        return

    # An update can be started from the page without going through
    # /configure first, and then there is no topgrade.toml: topgrade would
    # run every step it knows instead of the three this page offers.
    try:
        config_path = engine.get_profile_value("updates", "topgrade_config")
        if not os.path.exists(config_path):
            if config.is_topgrade_configured():
                write_topgrade_config(engine, config.get("updates.topgrade.enabled_steps"))
                await _send(websocket, "stdout", f"Wrote the saved configuration to {config_path}")
            else:
                write_topgrade_config(engine, DEFAULT_STEPS)
                config.configure_topgrade(DEFAULT_STEPS)
                await _send(websocket, "stdout", f"Wrote default configuration to {config_path}")
    except (CommandTranslationError, ConfigurationException, OSError) as exc:
        await _send(websocket, "stderr", f"Warning: could not write topgrade.toml: {exc}")

    # Pre-update snapshot (spec §7.7)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    snap_name = ""
    if config.get("updates.topgrade.auto_snapshot", True):
        snap_name = f"pre-update-{stamp}"
        for pool in _pools_to_snapshot(engine, config):
            try:
                engine.execute("zfs", "snapshot_create_recursive", dataset=pool, snap_name=snap_name)
                await _send(websocket, "stdout", f"Snapshot created: {pool}@{snap_name} (recursive)")
            except (CommandExecutionError, CommandTranslationError) as exc:
                await _send(websocket, "stderr", f"Warning: snapshot of {pool} failed: {exc}")

    global running
    running += 1
    try:
        if await asyncio.to_thread(bootenv.updates_inside, engine):
            exit_code = await _update_in_copy(websocket, engine, config, stamp)
        else:
            exit_code = await _update_live(websocket, engine, command, snap_name)
    finally:
        running -= 1
    if exit_code is None:
        await _close(websocket, 1011)
        return

    notices = {
        "restart_needed": await asyncio.to_thread(restart.restart_needed),
        "reboot_needed": (await asyncio.to_thread(bootstate.summary, engine, config)).get("reboot_needed", False),
    }
    try:
        await websocket.send_text(json.dumps(notices))
        await websocket.send_text(json.dumps({"exit_code": exit_code}))
    except CLOSED:
        pass
    await _close(websocket)
