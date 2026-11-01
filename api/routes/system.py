"""
/api/system/* — system identity, status, and power management.

GET  /status   — health check (no auth, always accessible)
GET  /info     — hostname, timezone, uptime, and what the documentation needs
                 to match this system: version, init system, firmware
GET  /health   — each FreedomNAS module's health, and why
POST /hostname — set hostname
POST /timezone — set timezone
GET  /ssh      — whether SSH is enabled
POST /ssh      — enable or disable SSH
GET  /boot     — automatic rollback: setting, boot state, last rollback
POST /boot/autorollback — turn the automatic rollback on or off
POST /boot/forget       — clear the record of a rollback, optionally
                          destroying the failed root dataset it kept
GET  /boot-environments — the copies of the system the boot loader can
                 start (FreeBSD), which runs, which boots next
POST /boot-environments/{name}/activate — boot that one from the next start
DELETE /boot-environments/{name} — destroy one that neither runs nor boots next
GET  /webui    — whether the WebUI must restart to use updated files, and
                 the work a restart would cut off
POST /webui/restart — restart the WebUI; refused while an update or a module
                 install/removal runs, and
                 while other jobs run unless {"force": true}
POST /reboot   — reboot the system
POST /shutdown — shut down the system
GET  /certificate — the WebUI certificate's fingerprints, names, addresses and
                 expiry, and the fingerprint of FreedomNAS's own CA (served
                 without credentials at /freedomnas-ca.crt)
GET  /config   — download this appliance's configuration
POST /config   — load a configuration back and apply it
"""

import asyncio
import re
import zoneinfo
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas.reconcile import ReconciliationEngine
from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
from freedomnas import bootenv, bootstate, replication, shadow, tls
from api import restart
from api.deps import authenticate, get_config, get_engine
from api.routes import plugins
from websocket import terminal

router = APIRouter()

_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z][A-Za-z0-9_+-]*)*$")
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def timezone_problem(timezone: str):
    """
    Several editions set the zone with `ln -sf /usr/share/zoneinfo/{timezone}
    /etc/localtime`, so the name is a path there: every part starts with a
    letter, which rules out `..` and options. Where this host has a zone
    database, the name must also be in it.
    """
    if not _ZONE.match(timezone):
        return f"{timezone!r} is not a timezone name such as Europe/Rome"
    known = zoneinfo.available_timezones()
    if known and timezone not in known:
        return f"Unknown timezone: {timezone}"
    return None


def _installed_version():
    try:
        return _VERSION_FILE.read_text().strip() or None
    except OSError:
        return None


def _firmware(engine):
    try:
        out, _ = engine.execute("bootloader", "firmware_mode")
    except (CommandExecutionError, CommandTranslationError):
        return None
    return out.strip() or None


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
        "version": _installed_version(),
        "init_system": engine.init_system,
        "firmware": _firmware(engine),
    }


def _module_health() -> list:
    """
    Ask every module this edition supports how it is doing. Nothing keeps
    the modules running between requests — no orchestrator runs on an
    appliance — so each is started, asked and forgotten; start() only reads
    the configuration.
    """
    report = []
    for manifest, module in ModuleLoader(get_engine(), get_config()).instances():
        entry = {"id": manifest.get("id", module.name), "name": manifest.get("name", module.name)}
        try:
            module.start()
            healthy, detail = module.health()
            entry.update(state="healthy" if healthy else "unhealthy", detail=detail)
        except Exception as exc:
            entry.update(state="error", detail=str(exc))
        report.append(entry)
    return report


@router.get("/health")
async def health(username: str = Depends(authenticate)):
    try:
        modules = await asyncio.to_thread(_module_health)
    except ModuleLoaderException as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"modules": modules}


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


@router.get("/ssh")
async def ssh_status(username: str = Depends(authenticate)):
    try:
        service = get_engine().get_profile_value("system", "ssh_service")
    except CommandTranslationError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"enabled": bool(get_config().get("system.ssh_enabled")), "service": service}


@router.post("/ssh")
async def set_ssh(body: dict, username: str = Depends(authenticate)):
    if not isinstance(body.get("enabled"), bool):
        return JSONResponse(status_code=422, content={"detail": "enabled must be true or false"})
    enabled = body["enabled"]
    engine = get_engine()
    config = get_config()
    try:
        service = engine.get_profile_value("system", "ssh_service")
        if enabled:
            engine.execute("services", "enable", service_name=service)
            engine.execute("services", "start", service_name=service)
        else:
            engine.execute("services", "stop", service_name=service, check=False)
            engine.execute("services", "disable", service_name=service, check=False)
        config.set("system.ssh_enabled", enabled)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"enabled": enabled, "service": service}


@router.post("/timezone")
async def set_timezone(body: dict, username: str = Depends(authenticate)):
    timezone = str(body.get("timezone", "")).strip()
    if not timezone:
        return JSONResponse(status_code=422, content={"detail": "timezone is required"})
    problem = timezone_problem(timezone)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    engine = get_engine()
    config = get_config()
    try:
        engine.execute("system", "timezone_set", timezone=timezone)
        config.set("system.timezone", timezone)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"timezone": timezone}


@router.get("/boot")
async def boot_status(username: str = Depends(authenticate)):
    return bootstate.summary(get_engine(), get_config())


@router.post("/boot/autorollback")
async def set_autorollback(body: dict, username: str = Depends(authenticate)):
    if not isinstance(body.get("enabled"), bool):
        return JSONResponse(status_code=422, content={"detail": "enabled must be true or false"})
    engine = get_engine()
    config = get_config()
    if bootstate.read_state(engine) is None and not bootenv.updates_inside(engine):
        return JSONResponse(status_code=409, content={"detail": "This edition does not boot a UKI"})
    try:
        bootstate.set_autorollback(engine, config, body["enabled"])
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return bootstate.summary(engine, config)


@router.post("/boot/forget")
async def forget_rollback(body: dict, username: str = Depends(authenticate)):
    destroy = body.get("destroy_failed", False)
    if not isinstance(destroy, bool):
        return JSONResponse(status_code=422, content={"detail": "destroy_failed must be true or false"})
    engine = get_engine()
    if bootstate.read_state(engine) is None and not bootenv.updates_inside(engine):
        return JSONResponse(status_code=409, content={"detail": "This edition does not boot a UKI"})
    try:
        destroyed = bootstate.forget(engine, destroy)
    except bootenv.BootEnvProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"destroyed": destroyed, **bootstate.summary(engine, get_config())}


@router.get("/boot-environments")
async def boot_environments(username: str = Depends(authenticate)):
    engine = get_engine()
    if not bootenv.supported(engine):
        return {"supported": False, "environments": []}
    try:
        environments = await asyncio.to_thread(bootenv.list_all, engine)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"supported": True, "environments": environments}


@router.post("/boot-environments/{name}/activate")
async def activate_boot_environment(name: str, username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        await asyncio.to_thread(bootenv.activate, engine, name)
        environments = await asyncio.to_thread(bootenv.list_all, engine)
    except bootenv.BootEnvProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"supported": True, "environments": environments}


@router.delete("/boot-environments/{name}")
async def destroy_boot_environment(name: str, username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        await asyncio.to_thread(bootenv.destroy, engine, name)
        environments = await asyncio.to_thread(bootenv.list_all, engine)
    except bootenv.BootEnvProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"supported": True, "environments": environments}


def _busy() -> dict:
    return {
        "updates": terminal.running,
        "plugins": 1 if plugins.running() else 0,
        "replication": sum(1 for job in replication.jobs() if job.get("state") == "running"),
    }


@router.get("/webui")
async def webui_state(username: str = Depends(authenticate)):
    return {
        "started": restart.STARTED,
        "restart_needed": await asyncio.to_thread(restart.restart_needed),
        "busy": _busy(),
    }


@router.post("/webui/restart")
async def webui_restart(body: dict = None, username: str = Depends(authenticate)):
    body = body or {}
    force = body.get("force", False)
    if not isinstance(force, bool):
        return JSONResponse(status_code=422, content={"detail": "force must be true or false"})
    busy = _busy()
    if busy["updates"]:
        return JSONResponse(status_code=409, content={
            "detail": "An update is running: restart the WebUI once it has finished", "busy": busy})
    if busy["plugins"]:
        return JSONResponse(status_code=409, content={
            "detail": "A module is being installed or removed: restart the WebUI once it has finished", "busy": busy})
    if busy["replication"] and not force:
        return JSONResponse(status_code=409, content={
            "detail": "Replication jobs are running: a restart stops them", "busy": busy})
    try:
        restart.request_restart(get_engine())
    except OSError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Restarting", "started": restart.STARTED}


@router.get("/certificate")
async def certificate(username: str = Depends(authenticate)):
    try:
        return await asyncio.to_thread(tls.describe)
    except (OSError, ValueError) as exc:
        return JSONResponse(status_code=404, content={"detail": f"No certificate: {exc}"})


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
