"""
/api/network/* — network interface listing and static IP configuration.

GET  /interfaces              — list physical interfaces with OS info
POST /interfaces/{name}/ip    — set static IP on a logical interface
GET  /config                  — saved configuration of each interface, and any pending change
PUT  /interfaces/{name}       — apply a configuration under a checkpoint (202, pending)
GET  /pending                 — the change waiting for confirmation, if any
POST /confirm                 — keep the pending change and save it
POST /revert                  — undo the pending change now
POST /confirm-token           — keep it, from a browser that reached the new address
                                 (no login: the one-time token PUT returned is the proof)

A change applied through PUT is undone by the system itself after
ROLLBACK_SECONDS unless /confirm is called, from the old address or the new
one: a wrong address cannot lock the administrator out.
"""

import json
import secrets
import threading
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import netconfig
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


@router.get("/interfaces")
async def list_interfaces(username: str = Depends(authenticate)):
    engine = get_engine()
    os_info = engine.get_os_info()
    try:
        if os_info["os_family"] == "bsd":
            # FreeBSD: use the JSON-capable key (spec §11.3)
            stdout, _ = engine.execute("network", "list_interfaces_json")
            return {"interfaces": json.loads(stdout)}
        else:
            stdout, _ = engine.execute("network", "list_interfaces")
            return {"interfaces": json.loads(stdout)}
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.post("/interfaces/{logical_name}/ip")
async def set_static_ip(logical_name: str, body: dict, username: str = Depends(authenticate)):
    required = {"ip", "cidr", "gateway", "dns"}
    if not required.issubset(body):
        return JSONResponse(status_code=422, content={"detail": f"Required fields: {required}"})

    engine = get_engine()
    config = get_config()
    dns = body["dns"] if isinstance(body["dns"], str) else " ".join(body["dns"])
    try:
        engine.execute(
            "network", "set_static_ip",
            conn_name=logical_name,
            interface=logical_name,
            ip=body["ip"],
            cidr=body["cidr"],
            gateway=body["gateway"],
            dns=dns,
        )
        iface = config.get_interface(logical_name) or {"logical_name": logical_name}
        iface["config"] = {
            "method": "static",
            "ip": body["ip"],
            "cidr": body["cidr"],
            "gateway": body["gateway"],
            "dns": body["dns"],
        }
        config.add_interface(iface)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"Static IP set on {logical_name}"}


ROLLBACK_SECONDS = 90

_lock = threading.Lock()
_pending: dict = {}


def _pending_view() -> dict:
    with _lock:
        if not _pending:
            return {"pending": False}
        left = max(0, int(_pending["deadline"] - time.monotonic()))
        if left == 0:
            failed = _pending["state"] == "failed"
            _pending.clear()
            return {"pending": False, "expired": not failed}
        return {"pending": True, "interface": _pending["interface"], "state": _pending["state"],
                "seconds_left": left, "config": _pending["config"], "error": _pending.get("error")}


def _activate(engine, name: str, config: dict, checkpoint: str) -> None:
    try:
        netconfig.apply(engine, name, config)
    except (CommandExecutionError, CommandTranslationError) as exc:
        try:
            netconfig.checkpoint_rollback(engine, checkpoint)
        except (CommandExecutionError, CommandTranslationError):
            pass
        with _lock:
            if _pending.get("checkpoint") == checkpoint:
                _pending.update(state="failed", error=str(exc), deadline=time.monotonic() + 30)
        return
    with _lock:
        if _pending.get("checkpoint") == checkpoint:
            _pending["state"] = "waiting"


@router.get("/config")
async def network_config(username: str = Depends(authenticate)):
    config = get_config()
    saved = {i["logical_name"]: i.get("config") or {}
             for i in config.get_interfaces() if i.get("logical_name")}
    return {"interfaces": saved, "rollback_seconds": ROLLBACK_SECONDS, "pending": _pending_view()}


@router.put("/interfaces/{logical_name}")
async def apply_interface(logical_name: str, body: dict, username: str = Depends(authenticate)):
    problem = netconfig.interface_problem(logical_name)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    try:
        config = netconfig.normalize(body)
    except netconfig.NetConfigProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    view = _pending_view()
    if view["pending"] and view["state"] != "failed":
        return JSONResponse(status_code=409, content={
            "detail": "Another network change is waiting for confirmation"})

    engine = get_engine()
    try:
        checkpoint = netconfig.checkpoint_create(engine, ROLLBACK_SECONDS)
    except (CommandExecutionError, CommandTranslationError, netconfig.NetConfigProblem) as exc:
        return JSONResponse(status_code=500, content={
            "detail": f"Not applied: the safety checkpoint could not be created ({exc})"})
    with _lock:
        _pending.clear()
        token = secrets.token_urlsafe(24)
        _pending.update(interface=logical_name, config=config, checkpoint=checkpoint, token=token,
                        state="applying", deadline=time.monotonic() + ROLLBACK_SECONDS)
    threading.Thread(target=_activate, args=(engine, logical_name, config, checkpoint),
                     daemon=True).start()
    return JSONResponse(status_code=202, content=dict(_pending_view(), token=token))


@router.get("/pending")
async def pending(username: str = Depends(authenticate)):
    return _pending_view()


@router.post("/confirm")
async def confirm(username: str = Depends(authenticate)):
    return _confirm()


@router.post("/confirm-token")
async def confirm_token(body: dict):
    token = str((body or {}).get("token") or "")
    with _lock:
        expected = _pending.get("token") or ""
    if not token or not expected or not secrets.compare_digest(token, expected):
        view = _pending_view()
        return JSONResponse(status_code=409, content={
            "detail": "The change was already undone" if view.get("expired") or not view["pending"]
            else "This link does not belong to the pending change"})
    return _confirm()


def _confirm():
    view = _pending_view()
    if not view["pending"]:
        return JSONResponse(status_code=409, content={
            "detail": "Nothing to confirm: the change was already undone" if view.get("expired")
            else "Nothing to confirm"})
    if view["state"] != "waiting":
        return JSONResponse(status_code=409, content={
            "detail": view.get("error") or "The change is still being applied"})
    with _lock:
        checkpoint, name, config = _pending["checkpoint"], _pending["interface"], _pending["config"]
    engine = get_engine()
    try:
        netconfig.checkpoint_confirm(engine, checkpoint)
    except (CommandExecutionError, CommandTranslationError) as exc:
        with _lock:
            _pending.clear()
        return JSONResponse(status_code=409, content={
            "detail": f"Too late: the change was already undone ({exc})"})
    with _lock:
        _pending.clear()
    try:
        cfg = get_config()
        iface = cfg.get_interface(name) or {"logical_name": name}
        iface["config"] = config
        cfg.add_interface(iface)
    except ConfigurationException as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"Network configuration of {name} kept", "config": config}


@router.post("/revert")
async def revert(username: str = Depends(authenticate)):
    with _lock:
        checkpoint = _pending.get("checkpoint")
        state = _pending.get("state")
        _pending.clear()
    if not checkpoint or state == "failed":
        return {"detail": "Nothing to undo"}
    try:
        netconfig.checkpoint_rollback(get_engine(), checkpoint)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "Network change undone"}
