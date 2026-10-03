"""
/api/updates/* — update management via Topgrade.

GET  /status     — last known pending package count, refreshed in background
POST /configure  — write topgrade.toml and persist to config (once)

Actual update execution happens via WebSocket at /ws/updates/terminal.

Counting pending updates is not a quick question on every system: apt
answers from its local lists in milliseconds, while Portage resolves the
whole dependency graph and takes minutes. So /status never waits for it —
it answers with the last known count and starts a refresh in the
background when that count has gone stale.
"""

import asyncio
import json
import pathlib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()

DEFAULT_STEPS = ["system", "containers", "firmware"]

# How long a count stays fresh, and how long the package manager gets to
# produce one. Portage has been measured at ~4 minutes on a NAS-class box.
CACHE_TTL = timedelta(hours=1)
LIST_TIMEOUT_SECONDS = 900

_pending: dict = {"count": None, "checked_at": None, "error": None}
_refresh: Optional[asyncio.Task] = None


def _count_pending_updates() -> int:
    """
    Blocking: runs the package manager. Every profile's
    `packages.list_updatable` prints one line per pending update.
    """
    engine = get_engine()
    stdout, _ = engine.execute(
        "packages", "list_updatable", check=False, timeout=LIST_TIMEOUT_SECONDS
    )
    return len([line for line in stdout.splitlines() if line.strip()])


async def _refresh_pending_updates() -> None:
    try:
        count = await asyncio.to_thread(_count_pending_updates)
    except (CommandExecutionError, CommandTranslationError) as exc:
        _pending["error"] = str(exc)
        return
    _pending.update(count=count, checked_at=datetime.now().isoformat(), error=None)


def _is_stale() -> bool:
    if _pending["checked_at"] is None:
        return True
    checked = datetime.fromisoformat(_pending["checked_at"])
    return datetime.now() - checked > CACHE_TTL


@router.get("/status")
async def update_status(username: str = Depends(authenticate)):
    global _refresh
    config = get_config()

    if _is_stale() and (_refresh is None or _refresh.done()):
        _refresh = asyncio.create_task(_refresh_pending_updates())

    return {
        "pending_updates": _pending["count"],
        "checked_at": _pending["checked_at"],
        "refreshing": _refresh is not None and not _refresh.done(),
        "error": _pending["error"],
        "topgrade_configured": config.is_topgrade_configured(),
        "enabled_steps": (config.get("updates.topgrade.enabled_steps")
                          if config.is_topgrade_configured() else DEFAULT_STEPS),
        "auto_snapshot": config.get("updates.topgrade.auto_snapshot", True),
    }


def write_topgrade_config(engine, enabled_steps: list) -> str:
    """
    Write topgrade.toml for these steps and return where it went.

    Called by /configure, and by the WebSocket handler when an update starts
    on a system where nobody has been through /configure yet: without the
    file, topgrade runs every step it knows — pip, cargo, flatpak and the
    rest — instead of the three this page offers.
    """
    config_path = engine.get_profile_value("updates", "topgrade_config")

    # topgrade rejects the whole file on one unknown field and silently
    # falls back to its defaults, so every key here has to be one it
    # actually knows: disable_tmux, [unstable] and [notifications] were
    # none of them. `only` lives in [misc], and its value is a TOML array
    # — json.dumps writes one, and quotes the step names properly.
    # assume_yes because nobody is at a keyboard when the WebUI runs this.
    toml_content = (
        "[misc]\n"
        "assume_yes = true\n"
        "no_retry = true\n"
        "run_in_tmux = false\n"
        "skip_notify = true\n"
        f"only = {json.dumps(enabled_steps)}\n"
    )

    # An edition may need to correct topgrade itself. Gentoo does: the
    # default emerge_update_flags carry -a (--ask), and --yes does not
    # remove it, so an unattended run would sit at a prompt forever.
    try:
        toml_content += "\n" + engine.get_profile_value("updates", "topgrade_extra_config")
    except CommandTranslationError:
        pass

    path = pathlib.Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(toml_content)
    return config_path


@router.post("/configure")
async def configure_topgrade(body: dict, username: str = Depends(authenticate)):
    enabled_steps = body.get("enabled_steps", DEFAULT_STEPS)
    auto_snapshot = body.get("auto_snapshot", True)
    engine = get_engine()
    config = get_config()
    try:
        write_topgrade_config(engine, enabled_steps)
        config.configure_topgrade(enabled_steps)
        config.set("updates.topgrade.auto_snapshot", auto_snapshot)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException, OSError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Topgrade configured", "enabled_steps": enabled_steps}
