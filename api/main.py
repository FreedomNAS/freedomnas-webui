"""
FreedomNAS WebUI — FastAPI application entry point.

Responsibilities:
- Lifespan: warm up engine and config on startup
- First-run guard middleware: returns 503 for /api/* routes (except /api/setup/* and
  /api/system/status) when the system is not yet initialized; page routes are always
  served and rely on client-side JS for the redirect
- Router registration
- Jinja2 page routes (all authenticated pages)
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
EXEMPT_EXACT    = {"/api/system/status"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_config()  # warm up config singleton (and engine via import chain)
    yield


app = FastAPI(title="FreedomNAS WebUI", version="0.2.0", lifespan=lifespan)

templates = Jinja2Templates(directory="frontend/templates")


# ---------------------------------------------------------------------------
# First-run guard middleware (spec §7.3)
# Only gates /api/* routes — page routes are always served so the browser
# can redirect the user to /setup/ via client-side JS.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def first_run_guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    exempt = path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIXES)
    if not exempt and not get_config().is_initialized():
        return JSONResponse(
            status_code=503,
            content={"detail": "System not initialized — complete /setup/ first."},
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
# Static files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


# ---------------------------------------------------------------------------
# Page routes — serve Jinja2 templates
# Auth and first-run state are managed client-side via JS.
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    if not get_config().is_initialized():
        return RedirectResponse("/setup/")
    return RedirectResponse("/dashboard/")


@app.get("/auth/")
async def page_auth(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request})


@app.get("/setup/")
async def page_setup(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})


@app.get("/dashboard/")
async def page_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/storage/")
async def page_storage(request: Request):
    return templates.TemplateResponse("storage.html", {"request": request})


@app.get("/shares/")
async def page_shares(request: Request):
    return templates.TemplateResponse("shares.html", {"request": request})


@app.get("/network/")
async def page_network(request: Request):
    return templates.TemplateResponse("network.html", {"request": request})


@app.get("/users/")
async def page_users(request: Request):
    return templates.TemplateResponse("users.html", {"request": request})


@app.get("/updates/")
async def page_updates(request: Request):
    return templates.TemplateResponse("updates.html", {"request": request})


@app.get("/system/")
async def page_system(request: Request):
    return templates.TemplateResponse("system.html", {"request": request})
