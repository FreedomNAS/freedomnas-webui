"""
/api/users/* — OS user management.

GET    /         — list users from config
POST   /         — create OS user + persist to config
DELETE /{username} — delete OS user + remove from config
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


@router.get("/")
async def list_users(username: str = Depends(authenticate)):
    config = get_config()
    return {"users": config.get_users()}


@router.post("/")
async def create_user(body: dict, username: str = Depends(authenticate)):
    new_user = body.get("username", "").strip()
    if not new_user:
        return JSONResponse(status_code=422, content={"detail": "username is required"})

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
