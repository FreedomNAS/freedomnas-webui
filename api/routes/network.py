"""
/api/network/* — network interface listing and static IP configuration.

GET  /interfaces              — list physical interfaces with OS info
POST /interfaces/{name}/ip    — set static IP on a logical interface
GET  /config                  — saved configuration of each interface, and any pending change
PUT  /interfaces/{name}       — apply a configuration under a checkpoint (202, pending)
GET  /pending                 — the change waiting for confirmation, if any
POST /confirm                 — keep the pending change and save it
POST /virtual                 — create a bridge, bond or VLAN under a checkpoint (202, pending)
DELETE /virtual/{name}        — remove one under a checkpoint (202, pending)
GET  /wireguard               — the WireGuard VPN and its devices, with their last handshake
POST /wireguard               — create it (fnas_wg1); DELETE /wireguard removes it
POST /wireguard/peers         — add a device: its configuration and QR code, shown once
DELETE /wireguard/peers/{name} — the device can no longer connect
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
from freedomnas import netconfig, netnames, wireguard
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


def _activate(engine, plan: list, checkpoint: str) -> None:
    try:
        netconfig.run(engine, plan)
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


def _start(name: str, config: dict, plan: list, keep) -> JSONResponse:
    """Run `plan` under a checkpoint; `keep(cfg)` records it in config.yaml once confirmed."""
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
        _pending.update(interface=name, config=config, checkpoint=checkpoint, token=token, keep=keep,
                        state="applying", deadline=time.monotonic() + ROLLBACK_SECONDS)
    threading.Thread(target=_activate, args=(engine, plan, checkpoint), daemon=True).start()
    return JSONResponse(status_code=202, content=dict(_pending_view(), token=token))


def _present(engine) -> dict:
    """Interfaces on this system → MAC address."""
    stdout, _ = engine.execute("network", "list_physical")
    present = {c["name"]: c["mac"] for c in netnames.parse_physical(stdout)}
    try:
        stdout, _ = engine.execute("network", "list_interfaces")
        for link in json.loads(stdout):
            if isinstance(link, dict) and link.get("ifname"):
                present.setdefault(link["ifname"], link.get("address") or "")
    except (ValueError, TypeError):
        pass
    return present


@router.get("/config")
async def network_config(username: str = Depends(authenticate)):
    config = get_config()
    entries = [{k: v for k, v in i.items() if k != "private_key"}
               for i in config.get_interfaces() if i.get("logical_name")]
    saved = {i["logical_name"]: i.get("config") or {} for i in entries}
    return {"interfaces": saved, "entries": entries, "rollback_seconds": ROLLBACK_SECONDS,
            "pending": _pending_view()}


@router.put("/interfaces/{logical_name}")
async def apply_interface(logical_name: str, body: dict, username: str = Depends(authenticate)):
    problem = netconfig.interface_problem(logical_name)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    try:
        config = netconfig.normalize(body)
    except netconfig.NetConfigProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    entry = get_config().get_interface(logical_name) or {"logical_name": logical_name}
    if entry.get("master"):
        return JSONResponse(status_code=409, content={
            "detail": f"{logical_name} is a port of {entry['master']}: configure {entry['master']} instead"})

    def keep(cfg):
        iface = cfg.get_interface(logical_name) or {"logical_name": logical_name}
        iface["config"] = config
        cfg.add_interface(iface)

    return _start(logical_name, config, netconfig.reconfigure_steps(entry, config), keep)


@router.post("/virtual")
async def create_virtual(body: dict, username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        entry = netconfig.normalize_virtual(body, get_config().get_interfaces(), _present(engine))
    except netconfig.NetConfigProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    def keep(cfg):
        for port in entry.get("ports") or []:
            iface = cfg.get_interface(port) or {"logical_name": port}
            iface.pop("config", None)
            iface["master"] = entry["logical_name"]
            cfg.add_interface(iface)
        cfg.add_interface(dict(entry))

    response = _start(entry["logical_name"], entry["config"], netconfig.virtual_steps(entry), keep)
    if response.status_code == 202:
        body = json.loads(response.body)
        body["entry"] = entry
        return JSONResponse(status_code=202, content=body)
    return response


@router.delete("/virtual/{logical_name}")
async def delete_virtual(logical_name: str, username: str = Depends(authenticate)):
    cfg = get_config()
    entry = cfg.get_interface(logical_name)
    if not entry or entry.get("type") not in netconfig.VIRTUAL_TYPES:
        return JSONResponse(status_code=404, content={"detail": f"No bridge, bond or VLAN named {logical_name}"})
    users = [i["logical_name"] for i in cfg.get_interfaces()
             if i.get("parent") == logical_name or i.get("master") == logical_name and i["logical_name"] not in (entry.get("ports") or [])]
    if users:
        return JSONResponse(status_code=409, content={
            "detail": f"{logical_name} is still used by {', '.join(users)}"})

    def keep(c):
        for port in entry.get("ports") or []:
            iface = c.get_interface(port)
            if iface is not None:
                iface.pop("master", None)
                c.add_interface(iface)
        c.remove_interface(logical_name)

    return _start(logical_name, {}, netconfig.virtual_remove_steps(entry), keep)


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
        checkpoint, name, config, keep = (_pending["checkpoint"], _pending["interface"],
                                          _pending["config"], _pending["keep"])
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
        keep(get_config())
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


def _networks_in_use(engine) -> list:
    networks = []
    try:
        stdout, _ = engine.execute("network", "list_interfaces")
        for link in json.loads(stdout):
            if not isinstance(link, dict) or str(link.get("ifname", "")).startswith(("lo", "fnas_wg")):
                continue
            for addr in link.get("addr_info") or []:
                if addr.get("family") == "inet" and addr.get("local"):
                    networks.append(f"{addr['local']}/{addr.get('prefixlen', 32)}")
    except (CommandExecutionError, CommandTranslationError, ValueError, TypeError):
        pass
    return networks


def _wireguard_view(engine, entry: dict) -> dict:
    try:
        live = wireguard.status(engine, entry["logical_name"])
    except (CommandExecutionError, CommandTranslationError):
        live = {}
    server = {k: v for k, v in entry.items() if k not in ("private_key", "peers")}
    server["public_key"] = wireguard.public_key(entry["private_key"])
    peers = [dict(p, **live.get(p["public_key"], {"latest_handshake": 0, "rx": 0, "tx": 0, "endpoint": ""}))
             for p in entry.get("peers") or []]
    return {"server": server, "peers": peers}


@router.get("/wireguard")
async def wireguard_get(username: str = Depends(authenticate)):
    entry = wireguard.find(get_config().get_interfaces())
    if not entry:
        return {"server": None, "peers": []}
    return _wireguard_view(get_engine(), entry)


@router.post("/wireguard")
async def wireguard_create(body: dict, username: str = Depends(authenticate)):
    cfg = get_config()
    if wireguard.find(cfg.get_interfaces()):
        return JSONResponse(status_code=409, content={"detail": "The WireGuard VPN exists already"})
    engine = get_engine()
    try:
        entry = wireguard.new_server(body, _networks_in_use(engine))
        wireguard.apply(engine, entry)
        cfg.add_interface(entry)
    except wireguard.WireGuardProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return _wireguard_view(engine, entry)


@router.delete("/wireguard")
async def wireguard_delete(username: str = Depends(authenticate)):
    cfg = get_config()
    entry = wireguard.find(cfg.get_interfaces())
    if not entry:
        return JSONResponse(status_code=404, content={"detail": "There is no WireGuard VPN"})
    try:
        wireguard.remove(get_engine(), entry["logical_name"])
        cfg.remove_interface(entry["logical_name"])
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": "WireGuard VPN removed"}


@router.post("/wireguard/peers")
async def wireguard_add_peer(body: dict, username: str = Depends(authenticate)):
    cfg = get_config()
    entry = wireguard.find(cfg.get_interfaces())
    if not entry:
        return JSONResponse(status_code=404, content={"detail": "Create the WireGuard VPN first"})
    engine = get_engine()
    try:
        peer, client_conf = wireguard.add_peer(entry, str((body or {}).get("name") or ""))
        updated = dict(entry, peers=(entry.get("peers") or []) + [peer])
        wireguard.apply(engine, updated)
        cfg.add_interface(updated)
    except wireguard.WireGuardProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    try:
        qr, _ = engine.execute_with_stdin("network", "qr_svg", client_conf)
    except (CommandExecutionError, CommandTranslationError):
        qr = ""
    return {"peer": peer, "config": client_conf, "qr_svg": qr}


@router.delete("/wireguard/peers/{name}")
async def wireguard_remove_peer(name: str, username: str = Depends(authenticate)):
    cfg = get_config()
    entry = wireguard.find(cfg.get_interfaces())
    if not entry:
        return JSONResponse(status_code=404, content={"detail": "There is no WireGuard VPN"})
    try:
        updated = wireguard.remove_peer(entry, name)
        wireguard.apply(get_engine(), updated)
        cfg.add_interface(updated)
    except wireguard.WireGuardProblem as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"{name} can no longer connect"}
