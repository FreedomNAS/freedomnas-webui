"""
/api/users/* — OS user management.

GET    /                    — list users from config
POST   /                    — create OS user + persist to config
POST   /{username}/password — set an existing user's password
DELETE /{username}          — delete OS user + remove from config

Passwords always reach the OS through execute_with_stdin (spec 13.2): the
profile's `users.set_password` variant puts them in the command string,
where ps and the logs would pick them up.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


MIN_PASSWORD_LENGTH = 8


def _set_password(engine, target: str, password: str) -> None:
    """
    Set an account's password without it ever appearing in a command line.

    The payload shape differs per OS — chpasswd wants "user:password", pw
    wants the password alone — so the profile carries it as
    users.password_stdin_format.
    """
    payload = engine.get_profile_value("users", "password_stdin_format")
    engine.execute_with_stdin(
        "users", "set_password_stdin",
        payload.format(username=target, password=password),
        username=target,
    )


@router.get("/")
async def list_users(username: str = Depends(authenticate)):
    config = get_config()
    return {"users": config.get_users()}


@router.post("/")
async def create_user(body: dict, username: str = Depends(authenticate)):
    new_user = body.get("username", "").strip()
    if not new_user:
        return JSONResponse(status_code=422, content={"detail": "username is required"})

    password = body.get("password", "")
    if not password:
        return JSONResponse(status_code=422, content={"detail": "password is required"})
    if len(password) < MIN_PASSWORD_LENGTH:
        return JSONResponse(
            status_code=422,
            content={"detail": f"password must be at least {MIN_PASSWORD_LENGTH} characters"},
        )

    groups = body.get("groups", [])
    shell = body.get("shell", "/bin/bash")
    uid = body.get("uid")
    gid = body.get("gid")

    engine = get_engine()
    config = get_config()
    try:
        engine.execute(
            "users", "create",
            username=new_user,
            groups=",".join(groups),
            shell=shell,
            uid=uid or "",
            gid=gid or "",
        )
        _set_password(engine, new_user, password)
        config.add_user({
            "username": new_user,
            "groups": groups,
            "shell": shell,
            **({"uid": uid} if uid else {}),
            **({"gid": gid} if gid else {}),
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"username": new_user}


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

    engine = get_engine()
    try:
        _set_password(engine, target, password)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"Password updated for {target}"}


@router.delete("/{target}")
async def delete_user(target: str, username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()
    try:
        engine.execute("users", "delete", username=target)
        config.remove_user(target)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"User {target} deleted"}
