"""
/api/updates/* — update management via Topgrade.

GET  /status     — pending package count (from OS package manager)
POST /configure  — write topgrade.toml and persist to config (once)

Actual update execution happens via WebSocket at /ws/updates/terminal.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()

DEFAULT_STEPS = ["system", "containers", "firmware"]


@router.get("/status")
async def update_status(username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()
    try:
        stdout, _ = engine.execute("packages", "list_updatable", check=False)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    count = len([l for l in stdout.splitlines() if l.strip()])
    return {
        "pending_updates": count,
        "topgrade_configured": config.is_topgrade_configured(),
        "auto_snapshot": config.get("updates.topgrade.auto_snapshot", True),
    }


@router.post("/configure")
async def configure_topgrade(body: dict, username: str = Depends(authenticate)):
    enabled_steps = body.get("enabled_steps", DEFAULT_STEPS)
    auto_snapshot = body.get("auto_snapshot", True)
    engine = get_engine()
    config = get_config()
    try:
        config_path = engine.get_profile_value("updates", "topgrade_config")
        toml_content = (
            "[misc]\n"
            "assume_yes = false\n"
            "disable_tmux = true\n"
            "no_retry = true\n\n"
            "[unstable]\n"
            f"only = {enabled_steps!r}\n\n"
            "[notifications]\n"
            "enable = false\n"
        )
        import pathlib
        pathlib.Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(config_path).write_text(toml_content)
        config.configure_topgrade(enabled_steps)
        config.set("updates.topgrade.auto_snapshot", auto_snapshot)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException, OSError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Topgrade configured", "enabled_steps": enabled_steps}
