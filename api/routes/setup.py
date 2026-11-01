"""
/api/setup/* — first-run wizard (spec §7.2).

These routes are exempt from the first-run guard middleware. All but
/status need an administrator's credentials: on a system installed from an
image those are the ones it shipped with, which the wizard replaces.

GET  /status      — whether the system is initialized, or restored and waiting
GET  /whoami      — who the credentials belong to (sign-in before setup)
GET  /restored    — what a configuration restored from a pool brought along
GET  /devices     — block devices, for the data pool
GET  /pools       — imported pools, for where the configuration lives
POST /initialize  — administrator, hostname, timezone, optional data pool,
                     the pool that keeps a copy of the configuration (its
                     own lives on the root pool); marks initialized.
                     On a restored system: administrator and SSH only

A disk image leaves a setup-pending file on its system disk. When the first
boot finds a .freedomnas dataset on an attached pool it restores that
configuration, which is marked initialized, but the image's shipped account
and password are still there and the restored users have no passwords: the
wizard stays in front of everything until the pending file is gone.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException, normalize_user
from freedomnas import shadow, usershares
from freedomnas.package_manager import admin_groups_present
from api.deps import _basic_credentials, authenticate, get_config, get_engine
from api.routes import storage
from api.routes.system import timezone_problem
from api.routes.users import (
    MIN_PASSWORD_LENGTH,
    account_ids,
    _credentials_problem,
    _delete_samba_user,
    _set_password,
    password_problem,
    set_temporary_password,
)

logger = logging.getLogger("freedomnas.webui.setup")

router = APIRouter()

_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _admin_problem(admin: dict, login: str, login_password: str, chosen: bool = False) -> Optional[str]:
    name = (admin.get("username") or "").strip() or login
    password = admin.get("password") or ""
    if not _USERNAME.match(name):
        return "The administrator's name starts with a lowercase letter and holds lowercase letters, digits, _ and -"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The administrator's password must be at least {MIN_PASSWORD_LENGTH} characters"
    if password == login_password and not chosen:
        return "Choose a new password: the one this system started with cannot stay"
    return _credentials_problem(name, password) or None


def _pool_names(engine) -> list:
    stdout, _ = engine.execute("zfs", "pool_list")
    return [line.split("\t")[0] for line in stdout.splitlines() if line.strip()]


def _account_exists(engine, name: str) -> bool:
    try:
        engine.execute("users", "get_user_info", username=name)
    except CommandExecutionError:
        return False
    return True


def _as_admin(config, name: str, sudo_group: str) -> dict:
    """
    The user's entry, restored fields kept, as an administrator — who also
    joins the administrators' groups this system has (`docker`, for the
    containers step of an update), which a configuration restored from
    another edition does not name.
    """
    engine = get_engine()
    entry = next((dict(u) for u in config.get_users() if u.get("username") == name), {"username": name})
    groups = list(entry.get("groups") or [])
    if sudo_group not in groups:
        groups.append(sudo_group)
    for group in admin_groups_present(engine):
        if group not in groups:
            engine.execute("users", "add_to_group", username=name, group=group, check=False)
            groups.append(group)
    entry.update(groups=groups, admin=True)
    if "uid" not in entry:
        entry.update(account_ids(engine, name))
    return entry


def _is_admin(user: dict, sudo_group: str) -> bool:
    return bool(normalize_user(user).get("admin")) or sudo_group in (user.get("groups") or [])


def _set_up_admin(engine, config, admin: dict, login: str) -> str:
    """
    The administrator the system keeps. A blank name keeps the account the
    wizard was opened with; its password is replaced either way. With a new
    name, the old account is removed if asked, and otherwise given the new
    password too, so the one the image shipped with works nowhere. root is
    never an image's shipped account, and is left as it is. A name a
    restored configuration already created is taken over, not created again.
    """
    name = (admin.get("username") or "").strip() or login
    password = admin["password"]
    sudo_group = engine.get_profile_value("users", "sudo_group")
    if name != login and not _account_exists(engine, name):
        engine.execute("users", "create", username=name, groups=sudo_group,
                       shell="/bin/bash", uid="", gid="")
    _set_password(engine, name, password)
    config.add_user(_as_admin(config, name, sudo_group))
    if name != login and login != "root":
        if admin.get("remove_default"):
            _delete_samba_user(engine, login)
            engine.execute("users", "delete", username=login)
            config.remove_user(login)
        else:
            _set_password(engine, login, password)
            config.add_user(_as_admin(config, login, sudo_group))
    return name


def _set_ssh(engine, config, enabled: bool) -> None:
    service = engine.get_profile_value("system", "ssh_service")
    if enabled:
        engine.execute("services", "enable", service_name=service)
        engine.execute("services", "start", service_name=service)
    config.set("system.ssh_enabled", enabled)


def _restoring(config) -> bool:
    return config.is_initialized() and config.setup_pending()


@router.get("/status")
async def setup_status():
    config = get_config()
    return {
        "initialized": not config.needs_setup(),
        "restored": _restoring(config),
        "password_chosen": config.setup_pending_kind() == "install",
        "edition": config.get_edition(),
        "detected_edition": get_engine().os_type,
    }


@router.get("/whoami")
async def whoami(username: str = Depends(authenticate)):
    return {"username": username}


@router.get("/restored")
async def setup_restored(username: str = Depends(authenticate)):
    config = get_config()
    if not _restoring(config):
        return JSONResponse(status_code=409, content={"detail": "No restored configuration is waiting"})
    engine = get_engine()
    sudo_group = engine.get_profile_value("users", "sudo_group")
    users = config.get_users()
    try:
        dataset, _ = engine.execute("zfs", "dataset_at_path",
                                    path=str(Path(config.config_path).parent), check=False)
    except (CommandExecutionError, CommandTranslationError):
        dataset = ""
    return {
        "hostname": config.get_hostname(),
        "timezone": config.get("system.timezone"),
        "state_dataset": dataset.strip() or None,
        "admins": [u["username"] for u in users if _is_admin(u, sudo_group)],
        "users": [u["username"] for u in users if not _is_admin(u, sudo_group)],
        "ssh_enabled": bool(config.get("system.ssh_enabled")),
    }


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

    if _restoring(config):
        return _finish_restore(engine, config, body, request, username)
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
    problem = _admin_problem(admin, username, login_password, config.setup_pending_kind() == "install")
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
        dataset = shadow.activate(engine, state_pool, state=Path(config.config_path).parent)
        engine.execute("system", "hostname_set", hostname=hostname)
        config.set_hostname(hostname)
        engine.execute("system", "timezone_set", timezone=timezone)
        config.set("system.timezone", timezone)
        usershares.prepare(engine)
        admin_name = _set_up_admin(engine, config, admin, username)
        config.set("system.ssh_enabled", False)
        config.mark_initialized(edition=edition, initialized_by="webui")
        config.clear_setup_pending()
        shadow.sync(engine, str(config.config_path))
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


def _finish_restore(engine, config, body: dict, request: Request, username: str):
    """
    The restored configuration keeps its hostname, users and shares. What the
    image brought along does not stay: the administrator gets a new password,
    and SSH, held off by the first boot, comes back only if asked for.
    """
    admin = body.get("admin") or {}
    _, login_password = _basic_credentials(request)
    problem = _admin_problem(admin, username, login_password, config.setup_pending_kind() == "install")
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    if not isinstance(body.get("ssh", False), bool):
        return JSONResponse(status_code=422, content={"detail": "ssh must be true or false"})
    keeper = (admin.get("username") or "").strip() or username
    temporary = body.get("users") or {}
    waiting = [u["username"] for u in config.get_users() if u.get("username") not in (keeper, username)]
    if not isinstance(temporary, dict) or sorted(temporary) != sorted(waiting):
        return JSONResponse(status_code=422, content={
            "detail": f"A temporary password is needed for every restored account: {', '.join(waiting)}"})
    for name in waiting:
        problem = password_problem(name, str(temporary[name]))
        if problem:
            return JSONResponse(status_code=422, content={"detail": f"{name}: {problem}"})

    try:
        _set_ssh(engine, config, body.get("ssh", False))
        # The configuration may come from another edition's install.
        config.set("system.edition", engine.os_type)
        usershares.prepare(engine)
        admin_name = _set_up_admin(engine, config, admin, username)
        for name in waiting:
            set_temporary_password(engine, config, name, str(temporary[name]))
        config.clear_setup_pending()
        shadow.sync(engine, str(config.config_path))
    except (
        CommandExecutionError,
        CommandTranslationError,
        ConfigurationException,
        OSError,
    ) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return {
        "detail": "System restored",
        "hostname": config.get_hostname(),
        "admin": admin_name,
        "temporary_passwords": waiting,
    }
