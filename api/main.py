"""
FreedomNAS WebUI — FastAPI application entry point.

Responsibilities:
- Lifespan: warm up engine and config on startup
- First-run guard middleware: returns 503 for all routes except /api/setup/*
  and /api/system/status when the system is not yet initialized
- Router registration
- Placeholder routes (501) for future phases
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.deps import get_config
from api.routes import network, setup, shares, storage, system, updates, users
from websocket import terminal

EXEMPT_PREFIXES = ("/api/setup",)
EXEMPT_EXACT = {"/api/system/status"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_config()  # warm up config (and engine via config.py import chain)
    yield


app = FastAPI(title="FreedomNAS WebUI", version="0.2.0", lifespan=lifespan)

templates = Jinja2Templates(directory="frontend/templates")


# ---------------------------------------------------------------------------
# First-run guard middleware (spec §7.3)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def first_run_guard(request: Request, call_next):
    path = request.url.path
    exempt = path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIXES)
    if not exempt and not get_config().is_initialized():
        return JSONResponse(
            status_code=503,
            content={"detail": "System not initialized — complete /api/setup/ first."},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(system.router,  prefix="/api/system",  tags=["system"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
app.include_router(network.router, prefix="/api/network", tags=["network"])
app.include_router(users.router,   prefix="/api/users",   tags=["users"])
app.include_router(shares.router,  prefix="/api/shares",  tags=["shares"])
app.include_router(updates.router, prefix="/api/updates", tags=["updates"])
app.include_router(setup.router,   prefix="/api/setup",   tags=["setup"])
app.include_router(terminal.router)


# ---------------------------------------------------------------------------
# Placeholder routes — planned for future phases (spec §7.8)
# ---------------------------------------------------------------------------

_NOT_IMPLEMENTED = JSONResponse(
    status_code=501,
    content={"detail": "Not implemented — planned for a future phase."},
)


@app.api_route("/api/packages/", methods=["GET", "POST"])
@app.api_route("/api/packages/{path:path}", methods=["GET", "POST", "DELETE"])
async def packages_placeholder(path: str = ""):
    return _NOT_IMPLEMENTED


@app.api_route("/api/backup/", methods=["GET", "POST"])
@app.api_route("/api/backup/{path:path}", methods=["GET", "POST", "DELETE"])
async def backup_placeholder(path: str = ""):
    return _NOT_IMPLEMENTED


# ---------------------------------------------------------------------------
# Frontend root
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/")
async def root():
    if not get_config().is_initialized():
        return RedirectResponse("/setup/")
    return RedirectResponse("/static/index.html")
