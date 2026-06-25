"""
/api/system/* — system identity, status, and power management.

GET  /status   — health check (no auth, always accessible)
GET  /info     — hostname, timezone, uptime
POST /hostname — set hostname
POST /reboot   — reboot the system
POST /shutdown — shut down the system
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


@router.get("/status")
async def status():
    """Health check — always accessible, no auth required."""
    return {"status": "ok"}


@router.get("/info")
async def info(username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()
    try:
        uptime, _ = engine.execute("system", "uptime")
        timezone, _ = engine.execute("system", "timezone_get")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {
        "hostname": config.get_hostname(),
        "edition": config.get_edition(),
        "timezone": timezone,
        "uptime_since": uptime,
        "initialized": config.is_initialized(),
    }


@router.post("/hostname")
async def set_hostname(body: dict, username: str = Depends(authenticate)):
    hostname = body.get("hostname", "").strip()
    if not hostname:
        return JSONResponse(status_code=422, content={"detail": "hostname is required"})
    engine = get_engine()
    config = get_config()
    try:
        # Dual write: OS first, then config (spec §7.5)
        engine.execute("system", "hostname_set", hostname=hostname)
        config.set_hostname(hostname)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"hostname": hostname}


@router.post("/reboot")
async def reboot(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        engine.execute("system", "reboot", check=False)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Rebooting"}


@router.post("/shutdown")
async def shutdown(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        engine.execute("system", "shutdown", check=False)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Shutting down"}
