"""
/api/services/* — the system's services, through the init profile.

GET  /                  — every service the init knows: running, enabled at boot
POST /{name}/start      — start it now
POST /{name}/stop       — stop it now
POST /{name}/restart    — restart it now
POST /{name}/enable     — start it at boot, recorded in config.yaml
POST /{name}/disable    — keep it from starting at boot, recorded in config.yaml

start, stop and restart last until the next boot. enable and disable are
written to config.yaml too, so the reconciler keeps them at every boot: the
SSH service through system.ssh_enabled, every other one under
services.boot.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()

ACTIONS = ("start", "stop", "restart", "enable", "disable")

_ROLES = (
    (("system", "ssh_service"), "SSH"),
    (("samba", "smb_service"), "SMB file sharing"),
    (("nfs", "nfs_service"), "NFS file sharing"),
)


def _service_names(engine) -> list:
    stdout, _ = engine.execute("services", "list_all")
    return sorted({line.strip() for line in stdout.splitlines() if line.strip()})


def _roles(engine) -> dict:
    roles = {}
    for (category, key), role in _ROLES:
        try:
            roles[engine.get_profile_value(category, key)] = role
        except CommandTranslationError:
            pass
    return roles


def _succeeds(engine, action: str, name: str) -> bool:
    try:
        stdout, _ = engine.execute("services", action, service_name=name, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return False
    return stdout.strip() == "yes"


def _state(engine, name: str, roles: dict) -> dict:
    return {
        "name": name,
        "running": _succeeds(engine, "is_running", name),
        "enabled": _succeeds(engine, "is_enabled", name),
        "role": roles.get(name),
    }


def _list(engine) -> list:
    names = _service_names(engine)
    roles = _roles(engine)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda n: _state(engine, n, roles), names))


@router.get("/")
async def list_services(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        services = await asyncio.to_thread(_list, engine)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"services": services}


@router.post("/{name}/{action}")
async def service_action(name: str, action: str, username: str = Depends(authenticate)):
    if action not in ACTIONS:
        return JSONResponse(status_code=404, content={"detail": f"No action {action}"})
    engine = get_engine()
    config = get_config()
    try:
        if name not in await asyncio.to_thread(_service_names, engine):
            return JSONResponse(status_code=404, content={"detail": f"No service {name}"})
        await asyncio.to_thread(engine.execute, "services", action, service_name=name)
        roles = _roles(engine)
        if action in ("enable", "disable"):
            enabled = action == "enable"
            if roles.get(name) == _ROLES[0][1]:
                config.set("system.ssh_enabled", enabled)
            else:
                config.set_service_boot(name, enabled)
        return await asyncio.to_thread(_state, engine, name, roles)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
