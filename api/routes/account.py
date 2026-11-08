"""
/api/account/* — what any account may do for itself, administrator or not.

GET  /me       — the signed-in account: its groups, whether it administers
                 the NAS, the shares it may use, the web pages of the NAS
                 (modules' links(), asked of all modules at once; one that
                 has not answered within 5 s is left out) and the
                 certificate authority's fingerprint, for the account page.
POST /password — {username, password, new_password}: change one's own
                 password, for the system and Samba at once. The current
                 password may be a temporary one that has expired (given
                 after a restore): that is what it is for. Signing in is
                 limited as everywhere else.
"""

import asyncio
import logging
import threading
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas import orchestrator, tls
from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
from api.deps import authenticate_account, check_password, get_config, get_engine, is_administrator
from api.routes.users import password_problem, set_own_password

logger = logging.getLogger("freedomnas.webui.account")

router = APIRouter()


def _groups(engine, username: str) -> list:
    try:
        groups, _ = engine.execute("users", "list_groups", username=username)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Cannot read the groups of %s: %s", username, exc)
        return []
    return groups.split()


def _smb_shares(config, username: str, groups: list) -> list:
    found = []
    for share in config.get_smb_shares():
        if (share.get("owner") == username or username in (share.get("valid_users") or [])
                or (share.get("group") and share["group"] in groups)):
            found.append({"name": share["name"], "path": share.get("path", ""),
                          "comment": share.get("comment", ""), "read_only": bool(share.get("read_only"))})
    return found


def _nfs_shares(config, username: str) -> list:
    return [{"name": s["name"], "path": s.get("path", "")}
            for s in config.get_nfs_shares() if s.get("owner") == username]


def _module_links(manifest: dict, module) -> list:
    try:
        module.start()
        return module.links()
    except Exception as exc:
        logger.warning("Module %s gave no links: %s", manifest.get("id", module.name), exc)
        return []


def _links() -> list:
    joined = {}
    try:
        modules = ModuleLoader(get_engine(), get_config()).instances()
    except ModuleLoaderException as exc:
        logger.warning("Cannot load the modules: %s", exc)
        return []
    found: dict = {}

    def ask(index, manifest, module):
        found[index] = _module_links(manifest, module)

    workers = []
    for index, (manifest, module) in enumerate(modules):
        worker = threading.Thread(target=ask, args=(index, manifest, module),
                                  name=f"fnas-links-{manifest.get('id', module.name)}", daemon=True)
        worker.start()
        workers.append((manifest, module, worker))
    deadline = time.monotonic() + orchestrator.CHECK_TIMEOUT
    for manifest, module, worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            logger.warning("Module %s gave no links within %s s", manifest.get("id", module.name),
                           orchestrator.CHECK_TIMEOUT)
    for index in range(len(workers)):
        for link in found.get(index) or []:
            name = str(link.get("name") or "")
            if name:
                joined.setdefault(name, {}).update({k: v for k, v in link.items()
                                                    if k not in joined.get(name, {})})
    return sorted((link for link in joined.values() if link.get("url") or link.get("port")),
                  key=lambda link: str(link.get("title") or link["name"]).lower())


@router.get("/me")
async def me(username: str = Depends(authenticate_account)):
    engine, config = get_engine(), get_config()
    groups = _groups(engine, username)
    return {
        "username": username,
        "admin": is_administrator(username),
        "groups": groups,
        "hostname": config.get_hostname(),
        "edition": config.get_edition(),
        "shares": {"smb": _smb_shares(config, username, groups), "nfs": _nfs_shares(config, username)},
        "links": await asyncio.to_thread(_links),
        "ca_fingerprint": tls.ca_fingerprint() if tls.ca_file().exists() else None,
    }


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
