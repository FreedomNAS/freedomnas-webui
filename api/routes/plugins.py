"""
/api/plugins/* — community modules installed from the edition's package repository.

A community module is a package named freedomnas-module-<id> (on Gentoo
app-nas/freedomnas-module-<id>) in the repositories the system already uses.
The profile commands packages.module_search and packages.module_installed
list them as name<TAB>version<TAB>description lines, so nothing here knows
which package manager runs underneath.

GET    /                — the modules offered and installed, the running job
GET    /failed          — the community modules the WebUI could not load at start, with the reason
GET    /job?since=N     — the job's output lines from line N on
POST   /refresh         — refresh the package index (a job)
POST   /{id}/install    — install the module's package (a job)
DELETE /{id}            — remove the module's package (a job); its configuration
                          and datasets stay, so installing it again finds them

Jobs run one at a time and never next to a system update. The WebUI loads
modules once, at start-up: after a job the pages offer to restart it.
"""

import asyncio
import json
import re
import shlex
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.package_manager import module_package_entries
from api import restart
from api.deps import authenticate, get_engine
from api import module_discovery
from api.module_discovery import _default_modules_dir

router = APIRouter()

MODULE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
OUTPUT_LINES = 2000

TIMEOUTS = {"refresh": 3600, "install": 7200, "remove": 3600}


def _modules_at_start(base: Path) -> set:
    ids = set()
    if not base.is_dir():
        return ids
    for entry in base.iterdir():
        manifest = entry / "manifest.json"
        if entry.name.startswith(("_", ".")) or not manifest.is_file():
            continue
        try:
            ids.add(json.loads(manifest.read_text()).get("id", entry.name))
        except (OSError, json.JSONDecodeError):
            continue
    return ids


LOADED = _modules_at_start(_default_modules_dir())

_job = {"kind": None, "module": None, "state": "idle", "error": None,
        "started": None, "finished": None, "exit_code": None}
_output: deque = deque(maxlen=OUTPUT_LINES)
_dropped = 0


def running() -> bool:
    return _job["state"] == "running"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _list(engine, action: str) -> dict:
    return module_package_entries(engine, action)


def catalogue(engine) -> dict:
    available = _list(engine, "module_search")
    installed = _list(engine, "module_installed")
    modules = []
    for module_id in sorted(set(available) | set(installed) | LOADED):
        offered = available.get(module_id)
        present = installed.get(module_id)
        known = present or offered or {}
        modules.append({
            "id": module_id,
            "package": known.get("package"),
            "description": known.get("description", ""),
            "available": offered["version"] if offered else None,
            "installed": present["version"] if present else None,
            "loaded": module_id in LOADED,
        })
    return {"supported": True, "modules": modules}


def job_state(since: int = 0) -> dict:
    first = _dropped
    start = max(since, first)
    lines = list(_output)[start - first:]
    return {**_job, "lines": lines, "next": first + len(_output), "from": start}


async def _run(kind: str, command: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        _job.update(state="failed", error=f"Could not start: {exc}", finished=_now())
        return

    async def pump():
        global _dropped
        async for raw in proc.stdout:
            if len(_output) == _output.maxlen:
                _dropped += 1
            _output.append(raw.decode(errors="replace").rstrip())

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), TIMEOUTS[kind])
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _job.update(state="failed", error=f"Stopped after {TIMEOUTS[kind] // 60} minutes",
                    exit_code=proc.returncode, finished=_now())
        return
    failed = proc.returncode != 0
    _job.update(
        state="failed" if failed else "done",
        error=f"The command ended with exit code {proc.returncode}" if failed else None,
        exit_code=proc.returncode,
        finished=_now(),
    )


def _start(kind: str, module_id, command) -> dict:
    global _dropped
    if isinstance(command, list):
        command = " && ".join(command)
    _output.clear()
    _dropped = 0
    _output.append(f"$ {command}")
    _job.update(kind=kind, module=module_id, state="running", error=None,
                started=_now(), finished=None, exit_code=None)
    asyncio.get_running_loop().create_task(_run(kind, command))
    return {"job": job_state()}


def _refused():
    from websocket import terminal
    if running():
        return JSONResponse(status_code=409, content={"detail": f"A module {_job['kind']} is already running"})
    if terminal.running:
        return JSONResponse(status_code=409, content={"detail": "A system update is running: try again once it has finished"})
    return None


def _command(engine, action: str, **kwargs):
    command = engine.get_command("packages", action, **kwargs)
    steps = command if isinstance(command, list) else [command]
    if any(re.search(r"[|&;<>$`]", step) for step in steps):
        raise CommandTranslationError(f"packages.{action} must be a plain command to run as a job")
    return command


@router.get("/")
async def plugins(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        result = await asyncio.to_thread(catalogue, engine)
    except CommandTranslationError:
        result = {"supported": False, "modules": []}
    except CommandExecutionError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    result["job"] = job_state(len(_output) + _dropped)
    result["restart_needed"] = await asyncio.to_thread(restart.restart_needed)
    return result


@router.get("/failed")
async def failed(username: str = Depends(authenticate)):
    return {"failed": [{"id": m.id, "reason": m.reason} for m in module_discovery.failed_modules]}


@router.get("/job")
async def job(since: int = 0, username: str = Depends(authenticate)):
    return job_state(max(since, 0))


@router.post("/refresh")
async def refresh(username: str = Depends(authenticate)):
    refused = _refused()
    if refused:
        return refused
    try:
        command = _command(get_engine(), "update_cache")
    except CommandTranslationError as exc:
        return JSONResponse(status_code=501, content={"detail": str(exc)})
    return _start("refresh", None, command)


async def _module_job(kind: str, module_id: str, listing: str, action: str, missing: str):
    if not MODULE_ID.fullmatch(module_id):
        return JSONResponse(status_code=422, content={"detail": "Invalid module name"})
    refused = _refused()
    if refused:
        return refused
    engine = get_engine()
    try:
        found = await asyncio.to_thread(_list, engine, listing)
        if module_id not in found:
            return JSONResponse(status_code=404, content={"detail": missing.format(id=module_id)})
        command = _command(engine, action, package_name=found[module_id]["package"])
    except CommandTranslationError as exc:
        return JSONResponse(status_code=501, content={"detail": str(exc)})
    refused = _refused()
    if refused:
        return refused
    return _start(kind, module_id, command)


@router.post("/{module_id}/install")
async def install(module_id: str, username: str = Depends(authenticate)):
    return await _module_job("install", module_id, "module_search", "install",
                             "The repository has no module '{id}'")


@router.delete("/{module_id}")
async def remove(module_id: str, username: str = Depends(authenticate)):
    return await _module_job("remove", module_id, "module_installed", "module_remove",
                             "No module '{id}' is installed from a package")
