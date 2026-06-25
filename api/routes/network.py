"""
/api/network/* — network interface listing and static IP configuration.

GET  /interfaces              — list physical interfaces with OS info
POST /interfaces/{name}/ip    — set static IP on a logical interface
"""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
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
