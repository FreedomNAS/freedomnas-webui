"""
/api/account/* — what any account may do for itself, administrator or not.

POST /password — {username, password, new_password}: change one's own
                 password, for the system and Samba at once. The current
                 password may be a temporary one that has expired (given
                 after a restore): that is what it is for. Signing in is
                 limited as everywhere else.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import check_password, get_config, get_engine
from api.routes.users import password_problem, set_own_password

router = APIRouter()


@router.post("/password")
async def change_password(body: dict):
    username = str(body.get("username") or "").strip()
    current = body.get("password") or ""
    new = body.get("new_password") or ""
    if not username or username == "root" or not isinstance(current, str) or not isinstance(new, str):
        return JSONResponse(status_code=422, content={"detail": "username, password and new_password are required"})
    if not check_password(username, current):
        return JSONResponse(status_code=401, content={"detail": "Wrong user name or password"})
    if new == current:
        return JSONResponse(status_code=422, content={"detail": "Choose a password different from the current one"})
    problem = password_problem(username, new)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    try:
        set_own_password(get_engine(), get_config(), username, new)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"username": username, "detail": "Password changed"}
