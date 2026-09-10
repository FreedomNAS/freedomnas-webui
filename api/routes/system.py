"""
/api/system/* — system identity, status, and power management.

GET  /status   — health check (no auth, always accessible)
GET  /info     — hostname, timezone, uptime
POST /hostname — set hostname
POST /reboot   — reboot the system
POST /shutdown — shut down the system
GET  /config   — download this appliance's configuration
POST /config   — load a configuration back and apply it
"""

import asyncio

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas.reconcile import ReconciliationEngine
from freedomnas import shadow
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


# ---------------------------------------------------------------------------
# Configuration export and import
#
# FreedomNAS restores a system by re-applying the desired state, not by
# putting saved files back: the export is that desired state, and the import
# hands it to the same reconciler the first boot uses.
# ---------------------------------------------------------------------------


@router.get("/config")
async def export_config(username: str = Depends(authenticate)):
    config = get_config()
    hostname = config.get_hostname() or "freedomnas"
    body = yaml.safe_dump(config.config, sort_keys=False, allow_unicode=True)
    return Response(
        content=body,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{hostname}-config.yaml"'},
    )


@router.post("/config")
async def import_config(request: Request, username: str = Depends(authenticate)):
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        incoming = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return JSONResponse(status_code=422, content={"detail": f"Not valid YAML: {exc}"})

    if not isinstance(incoming, dict):
        return JSONResponse(status_code=422, content={"detail": "Expected a YAML mapping"})

    config = get_config()
    previous = config.config
    config.config = incoming
    try:
        config.save()          # validates the structure before it touches the disk
    except ConfigurationException as exc:
        config.config = previous
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    reconciler = ReconciliationEngine(get_engine(), config)
    try:
        results = await asyncio.to_thread(reconciler.reconcile, None, "declarative")
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Configuration saved, but applying it failed: {exc}"},
        )

    # An imported configuration is worth mirroring straight away: waiting for
    # the nightly run would leave the data pool holding the old one.
    mirrored = await asyncio.to_thread(shadow.sync, get_engine(), str(config.config_path))

    return {
        "mirrored_to": [r["dataset"] for r in mirrored if r["copied"]],
        "applied": [r.action for r in results if r.target_state == "applied"],
        "skipped": [r.action for r in results if r.target_state == "skipped"],
        "failed": [{"action": r.action, "error": r.error}
                   for r in results if r.target_state == "failed"],
    }
