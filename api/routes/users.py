"""
/api/users/* — OS user management.

GET    /                    — list users from config
POST   /                    — create OS user + persist to config
POST   /{username}/password — set an existing user's password
POST   /{username}/admin    — give or take the administrator role
DELETE /{username}          — delete OS user + remove from config

Passwords always reach the OS through execute_with_stdin (spec 13.2): the
profile's `users.set_password` variant puts them in the command string,
where ps and the logs would pick them up. Where Samba is installed, the
same password goes into its own database too, so the account can open
shares.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas.package_manager import admin_groups_present
from freedomnas import groupshares
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


MIN_PASSWORD_LENGTH = 8
_CONTROL_CHARS = "\n\r\0"


def _credentials_problem(username: str, password: str) -> str:
    if ":" in username or any(c in username for c in _CONTROL_CHARS):
        return "username may not contain ':' or control characters"
    if any(c in password for c in _CONTROL_CHARS):
        return "password may not contain line breaks or control characters"
    return ""


def _set_password(engine, target: str, password: str) -> None:
    """
    Set an account's password without it ever appearing in a command line.

    Refuses what could end the payload's line early: chpasswd reads one
    "user:password" per line, and a newline in the password would start
    another one, for another account.

    The payload shape differs per OS — chpasswd wants "user:password", pw
    wants the password alone — so the profile carries it as
    users.password_stdin_format.
    """
    problem = _credentials_problem(target, password)
    if problem:
        raise CommandTranslationError(problem)
    payload = engine.get_profile_value("users", "password_stdin_format")
    engine.execute_with_stdin(
        "users", "set_password_stdin",
        payload.format(username=target, password=password),
        username=target,
    )
    if _samba_installed(engine):
        payload = engine.get_profile_value("samba", "password_stdin_format")
        engine.execute_with_stdin(
            "samba", "set_user_password",
            payload.format(username=target, password=password),
            username=target,
        )


def _samba_installed(engine) -> bool:
    """
    Samba keeps its own password database: an account without an entry
    there cannot open a share, whatever its system password. Where Samba is
    not installed there is nothing to keep in step.
    """
    try:
        engine.execute("samba", "check_installed")
    except (CommandExecutionError, CommandTranslationError):
        return False
    return True


def _delete_samba_user(engine, target: str) -> None:
    if _samba_installed(engine):
        engine.execute("samba", "delete_user", username=target, check=False)


@router.get("/")
async def list_users(username: str = Depends(authenticate)):
    config = get_config()
    sudo_group = get_engine().get_profile_value("users", "sudo_group")
    users = []
    for user in config.get_users():
        groups = list(user.get("groups") or [])
        if user.get("admin") and sudo_group and sudo_group not in groups:
            groups.insert(0, sudo_group)
        users.append({**user, "groups": groups})
    return {"users": users}


@router.post("/")
async def create_user(body: dict, username: str = Depends(authenticate)):
    new_user = body.get("username", "").strip()
    if not new_user:
        return JSONResponse(status_code=422, content={"detail": "username is required"})

    password = body.get("password", "")
    problem = password_problem(new_user, password)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})

    groups = body.get("groups", [])
    refused = _reserved_groups_refused(get_engine(), groups)
    if refused:
        return refused
    try:
        create_account(
            get_engine(), get_config(), new_user, password,
            groups=groups,
            shell=body.get("shell", "/bin/bash"),
            uid=body.get("uid"),
            gid=body.get("gid"),
        )
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"username": new_user}


def _reserved_groups_refused(engine, groups) -> Optional[JSONResponse]:
    """
    The groups administrators get (docker, libvirt: each gives root's power)
    come only with the administrators' group, never on their own.
    """
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return JSONResponse(status_code=422, content={"detail": "groups must be a list of names"})
    sudo_group = engine.get_profile_value("users", "sudo_group")
    if sudo_group in groups:
        return None
    reserved = [g for g in admin_groups_present(engine) if g in groups and g != sudo_group]
    if reserved:
        return JSONResponse(status_code=422, content={
            "detail": f"{', '.join(reserved)}: only administrators may be in "
                      f"{'this group' if len(reserved) == 1 else 'these groups'}; "
                      f"make the account an administrator instead"})
    return None


def password_problem(username: str, password: str) -> str:
    if not password:
        return "password is required"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return _credentials_problem(username, password)


def create_account(engine, config, new_user: str, password: str, groups=(),
                   shell: str = "/bin/bash", uid=None, gid=None) -> None:
    """
    Create an OS account with its system and Samba password, and record it.
    Raises on failure; an account that failed after useradd is taken back.
    """
    groups = list(groups) + [g for g in groupshares.everyone_groups(config) if g not in groups]
    if engine.get_profile_value("users", "sudo_group") in groups:
        groups += [g for g in admin_groups_present(engine) if g not in groups]
    engine.execute(
        "users", "create_with_uid" if uid else "create",
        username=new_user,
        groups=",".join(groups),
        shell=shell,
        uid=uid or "",
        gid=gid or "",
    )
    try:
        _set_password(engine, new_user, password)
        config.add_user({
            "username": new_user,
            "groups": groups,
            "shell": shell,
            **account_ids(engine, new_user),
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException):
        _undo_create(engine, new_user)
        raise


def set_temporary_password(engine, config, username: str, password: str) -> None:
    """
    Give an account a password it must change before using the system (after
    a restore, which brings accounts back without their passwords): set for
    the system and Samba, expired (`users.password_expire`), and recorded as
    temporary until its owner changes it (/api/account/password).
    """
    problem = password_problem(username, password)
    if problem:
        raise CommandTranslationError(problem)
    _set_password(engine, username, password)
    engine.execute("users", "password_expire", username=username)
    user = config.get_user(username) or {"username": username}
    config.add_user({**user, "temporary_password": True})


def set_own_password(engine, config, username: str, password: str) -> None:
    """An account's own new password: no longer expired nor temporary."""
    _set_password(engine, username, password)
    engine.execute("users", "password_fresh", username=username, check=False)
    user = config.get_user(username)
    if user and user.get("temporary_password"):
        config.add_user({k: v for k, v in user.items() if k != "temporary_password"})


def account_ids(engine, name: str) -> dict:
    """
    The user and group ID the system gave an account, for its configuration
    entry: files on a pool carry these numbers, and an account recreated
    elsewhere (a restore, another edition) must get the same ones back to
    still own them.
    """
    try:
        stdout, _ = engine.execute("users", "get_user_info", username=name, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return {}
    fields = (stdout or "").strip().split(":")
    if len(fields) < 4 or not fields[2].isdigit() or not fields[3].isdigit():
        return {}
    return {"uid": int(fields[2]), "gid": int(fields[3])}


def _undo_create(engine, target: str) -> None:
    """
    Take back an account whose creation failed halfway, so that it neither
    lingers on the system unknown to the configuration nor blocks the next
    attempt with "user already exists".
    """
    try:
        _delete_samba_user(engine, target)
        engine.execute("users", "delete", username=target, check=False)
    except (CommandExecutionError, CommandTranslationError):
        pass


@router.post("/{target}/password")
async def set_user_password(target: str, body: dict, username: str = Depends(authenticate)):
    password = body.get("password", "")
    if not password:
        return JSONResponse(status_code=422, content={"detail": "password is required"})
    if len(password) < MIN_PASSWORD_LENGTH:
        return JSONResponse(
            status_code=422,
            content={"detail": f"password must be at least {MIN_PASSWORD_LENGTH} characters"},
        )
    problem = _credentials_problem(target, password)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})

    engine = get_engine()
    try:
        _set_password(engine, target, password)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"Password updated for {target}"}


def _keeps_an_administrator(config, target: str, username: str, own: str) -> Optional[JSONResponse]:
    """The refusal when taking target's role or account would leave the caller or the NAS without one."""
    if target == username:
        return JSONResponse(status_code=422, content={
            "detail": f"You cannot {own}: another administrator has to"})
    user = config.get_user(target) or {}
    others = [u for u in config.get_users() if u.get("admin") and u.get("username") != target]
    if user.get("admin") and not others:
        return JSONResponse(status_code=422, content={
            "detail": f"{target} is the last administrator: make someone else one first"})
    return None


@router.post("/{target}/admin")
async def set_user_admin(target: str, body: dict, username: str = Depends(authenticate)):
    """
    The administrator role of an existing account: the edition's
    administrator group and the groups administrators get with it (docker…)
    are added or taken, and the configuration records `admin`. Nobody takes
    their own role, and the last administrator keeps it.
    """
    want = body.get("admin")
    if not isinstance(want, bool):
        return JSONResponse(status_code=422, content={"detail": "admin must be true or false"})
    engine, config = get_engine(), get_config()
    user = config.get_user(target)
    if user is None:
        return JSONResponse(status_code=404, content={"detail": f"There is no user named {target}"})
    if bool(user.get("admin")) == want:
        return {"username": target, "admin": want}
    if not want:
        refused = _keeps_an_administrator(config, target, username, "take your own administrator role")
        if refused:
            return refused
    sudo_group = engine.get_profile_value("users", "sudo_group")
    groups = [sudo_group] + [g for g in admin_groups_present(engine) if g != sudo_group]
    try:
        stdout, _ = engine.execute("users", "list_groups", username=target, check=False)
        have = set(stdout.split())
        for group in groups:
            if want and group not in have:
                engine.execute("users", "add_to_group", group=group, username=target)
            elif not want and group in have:
                engine.execute("users", "remove_from_group", group=group, username=target)
        config.add_user({**user, "admin": want,
                         "groups": [g for g in user.get("groups") or [] if g not in groups]})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"username": target, "admin": want}


@router.delete("/{target}")
async def delete_user(target: str, username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()
    if target == "root":
        return JSONResponse(status_code=422, content={"detail": "root cannot be deleted"})
    refused = _keeps_an_administrator(config, target, username, "delete your own account")
    if refused:
        return refused
    try:
        _delete_samba_user(engine, target)
        engine.execute("users", "delete", username=target)
        config.remove_user(target)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"User {target} deleted"}
