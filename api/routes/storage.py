"""
/api/storage/* — ZFS pool, dataset and snapshot management.

GET  /pools               — list ZFS pools
GET  /datasets            — list ZFS datasets
GET  /snapshots           — list snapshots across all datasets
POST /snapshots           — create a snapshot
GET  /devices             — block devices, and what is already on them
POST /pools               — create a pool
POST /pools/{pool}/devices — attach a cache (L2ARC) or log (SLOG) device
DELETE /pools/{pool}/devices?device=… — detach one again
GET  /properties          — the ZFS properties this edition supports
GET  /datasets/{name}/properties — what a dataset has them set to
PUT  /datasets/{name}/properties — change them
GET  /snapshot-policies   — list the scheduled-snapshot policies
PUT  /snapshot-policies/{dataset} — set how many to keep, per interval
DELETE /snapshot-policies/{dataset} — stop scheduling (keeps existing snapshots)
"""

import logging
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import pools as pool_devices
from freedomnas import shadow
from freedomnas.keymgmt import PoolProvisioner, KeyManagementError
from api.deps import authenticate, get_config, get_engine

logger = logging.getLogger("freedomnas.webui.storage")

router = APIRouter()


@router.get("/pools")
async def list_pools(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "pool_list")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    # What is attached as cache or log is not in `zpool list`: it is in
    # config.yaml, written when the device was attached.
    recorded = {p.get("name"): p for p in get_config().get_pools()}

    pools = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            entry = {"name": parts[0], "size": parts[1]}
            known = recorded.get(parts[0], {})
            for key in ("cache_devices", "log_devices", "vdev_type", "encrypted"):
                if known.get(key):
                    entry[key] = known[key]
            pools.append(entry)
    return {"pools": pools}


@router.get("/datasets")
async def list_datasets(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "dataset_list")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    datasets = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            datasets.append({
                "name": parts[0],
                "used": parts[1],
                "avail": parts[2],
                "refer": parts[3],
                "mountpoint": parts[4],
            })
    return {"datasets": datasets}


@router.get("/snapshots")
async def list_snapshots(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "snapshot_list")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    snapshots = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            snapshots.append({"name": parts[0], "creation": parts[1], "used": parts[2]})
    return {"snapshots": snapshots}


@router.post("/snapshots")
async def create_snapshot(body: dict, username: str = Depends(authenticate)):
    dataset = body.get("dataset", "").strip()
    snap_name = body.get("snap_name", "").strip()
    if not dataset or not snap_name:
        return JSONResponse(status_code=422, content={"detail": "dataset and snap_name are required"})
    engine = get_engine()
    config = get_config()
    try:
        engine.execute("zfs", "snapshot_create", dataset=dataset, snap_name=snap_name)
        config.add_root_snapshot(f"{dataset}@{snap_name}")
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"snapshot": f"{dataset}@{snap_name}"}


# ---------------------------------------------------------------------------
# Scheduled snapshots
#
# The route only writes the policy; cron runs `freedomnas snapshot-run` and
# the scheduler in core does the work. Nothing here talks to ZFS, so a policy
# can be set for a dataset that is temporarily unavailable.
# ---------------------------------------------------------------------------


@router.get("/snapshot-policies")
async def list_snapshot_policies(username: str = Depends(authenticate)):
    return {"policies": get_config().get_snapshot_policies()}


@router.put("/snapshot-policies/{dataset:path}")
async def set_snapshot_policy(
    dataset: str, body: dict, username: str = Depends(authenticate)
):
    config = get_config()
    keep = {}
    for interval in config.SNAPSHOT_INTERVALS:
        value = body.get(interval, 0)
        try:
            keep[interval] = int(value)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=422,
                content={"detail": f"{interval} must be a whole number, got {value!r}"},
            )

    if not any(keep.values()):
        return JSONResponse(
            status_code=422,
            content={"detail": "A policy that keeps nothing would take snapshots and "
                               "delete them again — remove the policy instead"},
        )

    try:
        config.set_snapshot_policy(dataset, keep)
    except ConfigurationException as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return {"dataset": dataset, **keep}


@router.delete("/snapshot-policies/{dataset:path}")
async def delete_snapshot_policy(dataset: str, username: str = Depends(authenticate)):
    if not get_config().remove_snapshot_policy(dataset):
        return JSONResponse(status_code=404, content={"detail": "No policy for that dataset"})
    return {"deleted": dataset}


# ---------------------------------------------------------------------------
# ZFS properties
#
# The catalogue lives in the profile (`zfs_properties`), because which
# properties exist — and which values they take — is an edition's business:
# xattr=sa and acltype=posix are Linux, aclmode and aclinherit are FreeBSD.
#
# Each entry carries the UI level it belongs to. The WebUI hides anything
# above the level in use; the API returns everything, so a client that knows
# what it is doing is never locked out of its own storage.
# ---------------------------------------------------------------------------

_SIZE = re.compile(r"^(none|0|\d+(\.\d+)?[KMGTPE]?)$", re.IGNORECASE)


def _catalogue():
    return get_engine().get_profile_section("zfs_properties")


def _validate(catalogue: dict, name: str, value: str):
    """Returns an error string, or None when the value is acceptable."""
    spec = catalogue.get(name)
    if spec is None:
        return f"Unknown property: {name}"
    if spec.get("create_only"):
        return f"{name} can only be set when the dataset is created"
    if spec["type"] == "enum" and value not in spec["values"]:
        return f"{name} accepts {', '.join(spec['values'])} — not {value!r}"
    if spec["type"] == "size" and not _SIZE.match(value):
        return f"{name} wants a size such as 100G, or 'none' — not {value!r}"
    return None


@router.get("/properties")
async def list_properties(username: str = Depends(authenticate)):
    return {"properties": _catalogue()}


@router.get("/datasets/{dataset:path}/properties")
async def get_dataset_properties(dataset: str, username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "dataset_get", dataset=dataset)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    catalogue = _catalogue()
    current = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] in catalogue:
            current[parts[0]] = {"value": parts[1], "source": parts[2]}
    return {"dataset": dataset, "properties": current}


@router.put("/datasets/{dataset:path}/properties")
async def set_dataset_properties(
    dataset: str, body: dict, username: str = Depends(authenticate)
):
    catalogue = _catalogue()
    wanted = {k: str(v) for k, v in body.items()}
    if not wanted:
        return JSONResponse(status_code=422, content={"detail": "Nothing to set"})

    errors = [e for e in (_validate(catalogue, k, v) for k, v in wanted.items()) if e]
    if errors:
        return JSONResponse(status_code=422, content={"detail": "; ".join(errors)})

    engine = get_engine()
    applied, failed = [], []
    for name, value in wanted.items():
        try:
            engine.execute("zfs", "dataset_set", dataset=dataset, property=name, value=value)
            applied.append(name)
        except (CommandExecutionError, CommandTranslationError) as exc:
            failed.append({"property": name, "error": str(exc)})

    status = 200 if not failed else 207
    return JSONResponse(
        status_code=status,
        content={"dataset": dataset, "applied": applied, "failed": failed},
    )


# ---------------------------------------------------------------------------
# Cache (L2ARC) and log (SLOG) devices
#
# Neither holds data: they are attached to a pool that already exists, which
# is why the same endpoint serves "add the SSD I just bought" and "add one
# while creating the pool" — creation attaches them straight afterwards.
# ---------------------------------------------------------------------------


@router.post("/pools/{pool_name}/devices")
async def attach_pool_devices(
    pool_name: str, body: dict, username: str = Depends(authenticate)
):
    role = (body.get("role") or "").strip()
    devices = [str(d).strip() for d in (body.get("devices") or []) if str(d).strip()]
    if role not in pool_devices.ROLES:
        return JSONResponse(
            status_code=422,
            content={"detail": f"role must be one of {', '.join(sorted(pool_devices.ROLES))}"},
        )
    if not devices:
        return JSONResponse(status_code=422, content={"detail": "devices must not be empty"})

    engine, config = get_engine(), get_config()
    result = pool_devices.attach_devices(engine, pool_name, **{role: devices})
    if result["errors"]:
        return JSONResponse(status_code=500, content={"detail": result["errors"][0]["error"]})

    for pool in config.get_pools():
        if pool.get("name") == pool_name:
            key = f"{role}_devices"
            pool[key] = sorted(set(pool.get(key, []) + result[role]))
            config.add_pool(pool)
            break
    return {"pool": pool_name, "role": role, "attached": result[role]}


@router.delete("/pools/{pool_name}/devices")
async def detach_pool_device(
    pool_name: str, device: str, username: str = Depends(authenticate)
):
    """
    The device arrives as a query parameter, not a path segment: `/dev/disk/
    by-id/...` in a path loses its leading slash, and `zpool remove` would be
    handed a relative name that means something else.
    """
    engine, config = get_engine(), get_config()
    try:
        pool_devices.detach_device(engine, pool_name, device)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    for pool in config.get_pools():
        if pool.get("name") == pool_name:
            changed = False
            for key in ("cache_devices", "log_devices"):
                if device in pool.get(key, []):
                    pool[key] = [d for d in pool[key] if d != device]
                    changed = True
            if changed:
                config.add_pool(pool)
            break
    return {"pool": pool_name, "detached": device}


# ---------------------------------------------------------------------------
# Creating a pool
#
# This is the one operation in the WebUI that destroys what it touches, so it
# is deliberately literal: it asks for whole device paths, refuses anything
# ambiguous, and never passes `-f`. ZFS already refuses a disk that carries
# somebody else's pool, and that refusal is better coming from ZFS than from
# a guess made here.
# ---------------------------------------------------------------------------

_POOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_LSBLK_FIELD = re.compile(r'(\w+)="([^"]*)"')
MIN_PASSPHRASE_LENGTH = 12


@router.get("/devices")
async def list_devices(username: str = Depends(authenticate)):
    """
    Block devices with what is already on them, so the pool form can show
    "in use" instead of letting somebody pick their running system.
    """
    engine = get_engine()
    try:
        stdout, _ = engine.execute("media", "list_devices")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    if engine.get_os_info().get("os_family") == "bsd":
        # `sysctl -n kern.disks` gives names and nothing else.
        return {"devices": [{"name": f"/dev/{n}", "type": "disk"}
                            for n in stdout.split()]}

    devices = []
    for line in stdout.splitlines():
        fields = dict(_LSBLK_FIELD.findall(line))
        if not fields.get("NAME"):
            continue
        devices.append({
            "name": fields["NAME"],
            "size": int(fields.get("SIZE") or 0),
            "type": fields.get("TYPE", ""),
            "fstype": fields.get("FSTYPE", ""),
            "mountpoint": fields.get("MOUNTPOINT", ""),
            "model": fields.get("MODEL", ""),
            "in_use": bool(fields.get("FSTYPE") or fields.get("MOUNTPOINT")),
        })
    return {"devices": devices}


@router.post("/pools")
async def create_pool(body: dict, username: str = Depends(authenticate)):
    name = (body.get("name") or "").strip()
    vdev_type = (body.get("vdev_type") or "").strip()
    devices = [str(d).strip() for d in (body.get("devices") or []) if str(d).strip()]
    cache = [str(d).strip() for d in (body.get("cache_devices") or []) if str(d).strip()]
    log = [str(d).strip() for d in (body.get("log_devices") or []) if str(d).strip()]
    encrypt = bool(body.get("encrypted"))
    passphrase = body.get("master_passphrase") or ""

    if not _POOL_NAME.match(name):
        return JSONResponse(status_code=422, content={
            "detail": "A pool name starts with a letter and holds letters, digits, _ . : -"})
    if not devices:
        return JSONResponse(status_code=422, content={"detail": "Select at least one device"})

    seen = set()
    for role, group in (("devices", devices), ("cache_devices", cache), ("log_devices", log)):
        for device in group:
            if device in seen:
                return JSONResponse(status_code=422, content={
                    "detail": f"{device} is listed twice — a device belongs to one role"})
            seen.add(device)

    if encrypt and len(passphrase) < MIN_PASSPHRASE_LENGTH:
        return JSONResponse(status_code=422, content={
            "detail": f"The passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"})

    engine, config = get_engine(), get_config()
    try:
        if encrypt:
            PoolProvisioner(engine).create_encrypted_pool(
                pool_name=name,
                vdev_type=vdev_type,
                devices=" ".join(devices),
                master_passphrase=passphrase,
                usb_devices=body.get("usb_devices") or [],
            )
        else:
            engine.execute(
                "zfs", "pool_create",
                pool_name=name,
                vdev_type=vdev_type,
                devices=" ".join(devices),
                ashift=body.get("ashift", 12),
                compression=body.get("compression", "lz4"),
            )
    except (CommandExecutionError, CommandTranslationError, KeyManagementError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    attached = pool_devices.attach_devices(engine, name, cache=cache, log=log)

    try:
        shadow.ensure_dataset(engine, name)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("No shadow dataset on %s: %s", name, exc)

    config.add_pool({
        "name": name,
        "vdev_type": vdev_type,
        "encrypted": encrypt,
        "cache_devices": attached["cache"],
        "log_devices": attached["log"],
    })
    return {"pool": name, "attached": attached, "warnings": attached["errors"]}
