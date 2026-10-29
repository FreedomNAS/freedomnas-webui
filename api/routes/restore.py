"""
/api/restore/* — the restore page's two calls, open without signing in:
while a restore runs the accounts may not have their passwords yet.

GET  /progress — the restore lock as the reconcile writes it ({active: false}
                 when there is none): reason, steps with their state and
                 error, finished, failed, errors.
POST /continue — take a finished lock away after a failed step, so the
                 WebUI opens; 409 while the restore still runs.

A WebUI that started while the lock was there loaded the configuration and
the modules from before the restore: once the lock is gone it restarts
itself (the supervisor starts it again at once) and keeps showing the
restore page, as "restarting", until then.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from freedomnas import restoring
from api import restart
from api.deps import get_engine

router = APIRouter()

_started_restoring = None
_restarting = False


def lock():
    return restoring.lock_file(get_engine())


def state() -> str:
    """"restoring", "restarting" (the restore ended, this WebUI is from before it) or "off"."""
    global _started_restoring, _restarting
    try:
        on = lock().exists()
    except Exception:
        on = False
    if _started_restoring is None:
        _started_restoring = on
    if on:
        return "restoring"
    if not _started_restoring:
        return "off"
    if not _restarting:
        _restarting = True
        restart.request_restart(get_engine(), delay=2.0)
    return "restarting"


@router.get("/progress")
async def progress():
    if state() == "restarting":
        return {"active": True, "restarting": True}
    data = restoring.read(lock())
    if data is None:
        return {"active": False}
    return {"active": True, **data}


@router.post("/continue")
async def go_on():
    if not restoring.clear(lock()):
        return JSONResponse(status_code=409, content={"detail": "The restore is still running"})
    return {"active": False}
