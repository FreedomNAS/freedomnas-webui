"""
/api/storage/* — ZFS pool, dataset and snapshot management.

GET  /pools               — list ZFS pools
GET  /datasets            — list ZFS datasets
GET  /snapshots           — list snapshots across all datasets
POST /snapshots           — create a snapshot
POST /pools/{pool}/devices — attach a cache (L2ARC) or log (SLOG) device
DELETE /pools/{pool}/devices?device=… — detach one again
GET  /properties          — the ZFS properties this edition supports
GET  /datasets/{name}/properties — what a dataset has them set to
PUT  /datasets/{name}/properties — change them
GET  /snapshot-policies   — list the scheduled-snapshot policies
PUT  /snapshot-policies/{dataset} — set how many to keep, per interval
DELETE /snapshot-policies/{dataset} — stop scheduling (keeps existing snapshots)
"""

import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import pools as pool_devices
from api.deps import authenticate, get_config, get_engine

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
