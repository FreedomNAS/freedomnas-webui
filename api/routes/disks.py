"""
/api/storage/disks/* — the health of the physical disks, from SMART.

GET  /                 — every disk smartctl finds, with its health
POST /{disk}/test      — start a short or long SMART self-test
"""

import json
import logging
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from api.deps import authenticate, get_engine

logger = logging.getLogger("freedomnas.webui.disks")

router = APIRouter()

TEST_TYPES = {"short", "long"}

ATA_COUNTERS = {
    5: "reallocated",
    187: "reported_uncorrect",
    197: "pending",
    198: "offline_uncorrectable",
    199: "crc_errors",
}

COUNTER_REASONS = {
    "reallocated": "{n} reallocated sectors",
    "reported_uncorrect": "{n} errors the disk could not correct",
    "pending": "{n} sectors waiting to be reallocated",
    "offline_uncorrectable": "{n} unreadable sectors found offline",
    "crc_errors": "{n} transfer (CRC) errors: check the cable",
}

SMARTCTL_STANDBY = 2

_LSBLK_FIELD = re.compile(r'(\w+)="([^"]*)"')


def _smartctl_json(stdout: str):
    try:
        return json.loads(stdout)
    except (TypeError, ValueError):
        return None


def scan_disks(engine):
    """The device paths smartctl offers, or None when smartctl is not installed."""
    try:
        stdout, _ = engine.execute("media", "smart_scan", check=False)
    except (CommandExecutionError, CommandTranslationError):
        return None
    data = _smartctl_json(stdout)
    if data is None:
        return None
    return [d["name"] for d in data.get("devices", []) if d.get("name")]


def _disk_of(path: str, parents: dict) -> str:
    while parents.get(path):
        path = parents[path]
    return path


def pool_members(engine) -> dict:
    """Disk path -> names of the pools that have a partition (or all) of it."""
    parents = {}
    try:
        stdout, _ = engine.execute("media", "list_devices", check=False)
        for line in stdout.splitlines():
            fields = dict(_LSBLK_FIELD.findall(line))
            if fields.get("NAME"):
                parents[fields["NAME"]] = fields.get("PKNAME", "")
    except (CommandExecutionError, CommandTranslationError):
        pass
    try:
        stdout, _ = engine.execute("zfs", "pool_member_devices", check=False)
    except (CommandExecutionError, CommandTranslationError):
        return {}
    members: dict = {}
    pool = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pool:"):
            pool = stripped.split(":", 1)[1].strip()
        elif pool and stripped.startswith("/dev/"):
            disk = _disk_of(stripped.split()[0], parents)
            if pool not in members.setdefault(disk, []):
                members[disk].append(pool)
    return members


def _kind(data: dict) -> str:
    if data.get("device", {}).get("protocol") == "NVMe":
        return "NVMe"
    rate = data.get("rotation_rate")
    if rate == 0:
        return "SSD"
    if isinstance(rate, int) and rate > 0:
        return "HDD"
    return ""


def summarize(path: str, data, pools: list) -> dict:
    """What the Disks tab shows for one disk, with a single health verdict and why."""
    disk = {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "pools": pools,
        "model": "",
        "serial": "",
        "size": 0,
        "kind": "",
        "protocol": "",
        "health": "unknown",
        "reasons": [],
        "temperature": None,
        "power_on_hours": None,
        "counters": {},
        "last_test": None,
        "test_running": None,
        "error_count": None,
        "attributes": [],
    }
    if data is None:
        disk["reasons"].append("smartctl gave no answer for this disk")
        return disk

    exit_status = data.get("smartctl", {}).get("exit_status", 0)
    disk.update({
        "model": data.get("model_name") or data.get("scsi_model_name", ""),
        "serial": data.get("serial_number", ""),
        "size": data.get("user_capacity", {}).get("bytes", 0),
        "kind": _kind(data),
        "protocol": data.get("device", {}).get("protocol", ""),
        "temperature": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
    })

    if "smart_status" not in data:
        if exit_status & SMARTCTL_STANDBY and "ata_smart_attributes" not in data:
            disk["health"] = "standby"
            disk["reasons"].append("The disk is asleep; it was not woken up to read it")
        else:
            disk["reasons"].append("The disk does not report SMART data (or the USB adapter hides it)")
        return disk

    failing, warning = [], []
    if not data["smart_status"].get("passed", True):
        failing.append("The disk's own SMART check reports it is failing")

    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        raw = attr.get("raw", {}).get("value", 0)
        disk["attributes"].append({
            "id": attr.get("id"),
            "name": attr.get("name", ""),
            "value": attr.get("value"),
            "worst": attr.get("worst"),
            "thresh": attr.get("thresh"),
            "raw": attr.get("raw", {}).get("string", str(raw)),
            "when_failed": attr.get("when_failed", ""),
        })
        if attr.get("when_failed") == "now":
            failing.append(f"{attr.get('name')} is below its failure threshold")
        elif attr.get("when_failed") == "past":
            warning.append(f"{attr.get('name')} was below its failure threshold in the past")
        key = ATA_COUNTERS.get(attr.get("id"))
        if key:
            disk["counters"][key] = raw
            if raw:
                warning.append(COUNTER_REASONS[key].format(n=raw))

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        disk["counters"].update({
            "percentage_used": nvme.get("percentage_used"),
            "media_errors": nvme.get("media_errors"),
            "available_spare": nvme.get("available_spare"),
        })
        if nvme.get("critical_warning"):
            failing.append("The NVMe controller raises a critical warning")
        if nvme.get("media_errors"):
            warning.append(f"{nvme['media_errors']} media errors")
        if (nvme.get("percentage_used") or 0) >= 90:
            warning.append(f"{nvme['percentage_used']}% of the rated endurance used")
        spare, threshold = nvme.get("available_spare"), nvme.get("available_spare_threshold")
        if spare is not None and threshold is not None and spare <= threshold:
            failing.append("Spare capacity is at or below the threshold")
        if disk["power_on_hours"] is None:
            disk["power_on_hours"] = nvme.get("power_on_hours")

    tests = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    if tests:
        last = tests[0]
        disk["last_test"] = {
            "type": last.get("type", {}).get("string", ""),
            "status": last.get("status", {}).get("string", ""),
            "passed": last.get("status", {}).get("passed"),
            "hours": last.get("lifetime_hours"),
        }
        if last.get("status", {}).get("passed") is False:
            warning.append(f"The last self-test failed: {disk['last_test']['status']}")
    status = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    if status.get("remaining_percent") is not None:
        disk["test_running"] = {"remaining_percent": status["remaining_percent"]}

    disk["error_count"] = data.get("ata_smart_error_log", {}).get("summary", {}).get("count")
    if disk["error_count"]:
        warning.append(f"{disk['error_count']} errors in the disk's error log")

    disk["health"] = "failing" if failing else "warning" if warning else "ok"
    disk["reasons"] = failing + warning
    return disk


@router.get("/")
async def list_disks(username: str = Depends(authenticate)):
    engine = get_engine()
    paths = scan_disks(engine)
    if paths is None:
        return {"available": False, "disks": []}
    members = pool_members(engine)
    disks = []
    for path in paths:
        try:
            stdout, _ = engine.execute("media", "smart_info", device=path, check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("SMART read of %s failed: %s", path, exc)
            stdout = ""
        disks.append(summarize(path, _smartctl_json(stdout), members.get(path, [])))
    return {"available": True, "disks": disks}


@router.post("/{disk}/test")
async def start_test(disk: str, body: dict, username: str = Depends(authenticate)):
    test_type = body.get("type")
    if test_type not in TEST_TYPES:
        return JSONResponse(status_code=400, content={"detail": "type must be short or long"})
    engine = get_engine()
    paths = scan_disks(engine) or []
    path = next((p for p in paths if p.rsplit("/", 1)[-1] == disk), None)
    if path is None:
        return JSONResponse(status_code=404, content={"detail": f"No disk named {disk}"})
    try:
        engine.execute("media", "smart_test", test_type=test_type, device=path)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    logger.info("%s started a %s SMART self-test on %s", username, test_type, path)
    return {"detail": f"{test_type.capitalize()} self-test started on {disk}"}
