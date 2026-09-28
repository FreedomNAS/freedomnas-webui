"""
/api/setup/* — first-run wizard (spec §7.2).

These routes are exempt from the first-run guard middleware. All but
/status need an administrator's credentials: on a system installed from an
image those are the ones it shipped with, which the wizard replaces.

GET  /status      — whether the system is initialized
GET  /whoami      — who the credentials belong to (sign-in before setup)
GET  /devices     — block devices, for the data pool
GET  /pools       — imported pools, for where the configuration lives
POST /initialize  — administrator, hostname, timezone, optional data pool,
                     the pool that holds .freedomnas; marks initialized
"""

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import shadow
from api.deps import _basic_credentials, authenticate, get_config, get_engine
from api.routes import storage
from api.routes.system import timezone_problem
from api.routes.users import (
    MIN_PASSWORD_LENGTH,
    _credentials_problem,
    _delete_samba_user,
    _set_password,
)

logger = logging.getLogger("freedomnas.webui.setup")

router = APIRouter()

_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _prepare_usershares(engine) -> None:
    """
    Make Samba usershares possible, so shares can be created later (spec §7.6).

    The [global] usershare settings are appended only when smb.conf lacks
    them: an installation that wrote a complete smb.conf already has them,
    and a second copy would be noise. The directory is created through the
    profile, since where it lives and how it is created is the edition's.
    """
    smb_conf = engine.get_profile_value("samba", "config_file")
    usershare_path = engine.get_profile_value("samba", "usershare_path")
    try:
        with open(smb_conf) as f:
            current = f.read()
    except FileNotFoundError:
        current = ""
    if "usershare path" not in current:
        with open(smb_conf, "a") as f:
            f.write(
                "\n[global]\n"
                f"   usershare path = {usershare_path}\n"
                "   usershare max shares = 100\n"
                "   usershare allow guests = no\n"
            )
    engine.execute("samba", "usershare_prepare", path=usershare_path)


def _admin_problem(admin: dict, login: str, login_password: str) -> Optional[str]:
    name = (admin.get("username") or "").strip() or login
    password = admin.get("password") or ""
    if not _USERNAME.match(name):
        return "The administrator's name starts with a lowercase letter and holds lowercase letters, digits, _ and -"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The administrator's password must be at least {MIN_PASSWORD_LENGTH} characters"
    if password == login_password:
        return "Choose a new password: the one this system started with cannot stay"
    return _credentials_problem(name, password) or None


def _pool_names(engine) -> list:
    stdout, _ = engine.execute("zfs", "pool_list")
    return [line.split("\t")[0] for line in stdout.splitlines() if line.strip()]


def _set_up_admin(engine, config, admin: dict, login: str) -> str:
    """
    The administrator the system keeps. A blank name keeps the account the
    wizard was opened with; its password is replaced either way. With a new
    name, the old account is removed if asked, and otherwise given the new
    password too, so the one the image shipped with works nowhere. root is
    never an image's shipped account, and is left as it is.
    """
    name = (admin.get("username") or "").strip() or login
    password = admin["password"]
    sudo_group = engine.get_profile_value("users", "sudo_group")
    if name != login:
        engine.execute("users", "create", username=name, groups=sudo_group,
                       shell="/bin/bash", uid="", gid="")
    _set_password(engine, name, password)
    config.add_user({"username": name, "groups": [sudo_group], "admin": True})
    if name != login and login != "root":
        if admin.get("remove_default"):
            _delete_samba_user(engine, login)
            engine.execute("users", "delete", username=login)
        else:
            _set_password(engine, login, password)
            config.add_user({"username": login, "groups": [sudo_group], "admin": True})
    return name


@router.get("/status")
async def setup_status():
    config = get_config()
    return {
        "initialized": config.is_initialized(),
        "edition": config.get_edition(),
        "detected_edition": get_engine().os_type,
    }


@router.get("/whoami")
async def whoami(username: str = Depends(authenticate)):
    return {"username": username}


@router.get("/devices")
async def setup_devices(username: str = Depends(authenticate)):
    return await storage.list_devices(username=username)


@router.get("/pools")
async def setup_pools(username: str = Depends(authenticate)):
    listed = await storage.list_pools(username=username)
    if isinstance(listed, JSONResponse):
        return listed
    try:
        root, _ = get_engine().execute("zfs", "dataset_at_path", path="/", check=False)
    except (CommandExecutionError, CommandTranslationError):
        root = ""
    return {**listed, "system_pool": root.strip().split("/", 1)[0] or None}


@router.post("/initialize")
async def initialize(body: dict, request: Request, username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()

    if config.is_initialized():
        return JSONResponse(status_code=409, content={"detail": "System already initialized"})

    hostname = (body.get("hostname") or "freedomnas").strip()
    timezone = (body.get("timezone") or "UTC").strip()
    edition = (body.get("edition") or engine.os_type).strip()
    admin = body.get("admin") or {}
    pool_cfg = body.get("pool")
    state_pool = (body.get("state_pool") or "").strip()

    if edition != engine.os_type:
        return JSONResponse(status_code=422, content={
            "detail": f"This system runs the {engine.os_type} edition, not {edition}"})
    if not _HOSTNAME.match(hostname):
        return JSONResponse(status_code=422, content={
            "detail": "The hostname holds letters, digits and hyphens only"})
    problem = timezone_problem(timezone)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    _, login_password = _basic_credentials(request)
    problem = _admin_problem(admin, username, login_password)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    if not state_pool:
        return JSONResponse(status_code=422, content={
            "detail": "Choose the pool that keeps FreedomNAS's configuration"})

    try:
        pools = _pool_names(engine)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    new_pool = (pool_cfg or {}).get("name", "").strip() if pool_cfg else ""
    if state_pool not in pools and state_pool != new_pool:
        return JSONResponse(status_code=422, content={"detail": f"No pool named {state_pool}"})

    if pool_cfg:
        created = await storage.create_pool(pool_cfg, username=username)
        if isinstance(created, JSONResponse):
            return created

    try:
        engine.execute("system", "hostname_set", hostname=hostname)
        config.set_hostname(hostname)
        engine.execute("system", "timezone_set", timezone=timezone)
        config.set("system.timezone", timezone)
        _prepare_usershares(engine)
        admin_name = _set_up_admin(engine, config, admin, username)
        config.set("system.ssh_enabled", False)
        config.mark_initialized(edition=edition, initialized_by="webui")
        dataset = shadow.activate(engine, state_pool, state=Path(config.config_path).parent)
    except (
        CommandExecutionError,
        CommandTranslationError,
        ConfigurationException,
        OSError,
    ) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return {
        "detail": "System initialized",
        "hostname": hostname,
        "edition": edition,
        "admin": admin_name,
        "state_dataset": dataset,
    }
