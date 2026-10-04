"""
/api/storage/* — ZFS pool, dataset and snapshot management.

GET  /pools               — list ZFS pools
GET  /datasets            — list ZFS datasets
POST /datasets            — create a dataset
GET  /datasets/{name}/dependents — children, snapshots, shares and policies it has
PUT  /datasets/{name}/mountpoint — move it (inherit, a path, or none)
POST /datasets/{name}/rename — rename it within its pool
DELETE /datasets/{name}   — destroy it
GET  /snapshots           — list snapshots across all datasets
POST /snapshots           — create a snapshot
GET  /devices             — block devices, and what is already on them
POST /pools               — create a pool
GET  /pools/{pool}/removal — what exporting or destroying it would take along
POST /pools/{pool}/export — export it (import it again with /pools/import)
DELETE /pools/{pool}?confirm=… — destroy it
GET  /usb-devices          — removable partitions that can hold a pool's USB key
GET  /pools/importable    — pools attached to this system but not imported
POST /pools/import/inspect — look at one without changing it
POST /pools/import        — import it, with the shares and users it brings
POST /pools/{pool}/devices — attach a cache (L2ARC) or log (SLOG) device
DELETE /pools/{pool}/devices?device=… — detach one again
GET  /properties          — the ZFS properties this edition supports
GET  /datasets/{name}/properties — what a dataset has them set to
PUT  /datasets/{name}/properties — change them
GET  /snapshot-policies   — list the scheduled-snapshot policies
PUT  /snapshot-policies/{dataset} — set how many to keep, per interval
DELETE /snapshot-policies/{dataset} — stop scheduling (keeps existing snapshots)
GET  /pools/{pool}/status — state, scrub/resilver progress and the vdev tree
POST /pools/{pool}/scrub|trim|initialize — start, pause/suspend, stop/cancel
POST /pools/{pool}/clear  — clear error counters, of the pool or one device
POST /pools/{pool}/device — online, offline, expand, replace, attach, detach, remove one device
POST /pools/{pool}/vdevs  — add data, special, dedup or spare vdevs
GET  /pools/{pool}/history — zpool history
GET  /pool-properties     — the pool properties this edition supports
GET  /pools/{pool}/properties — what the pool has them set to
PUT  /pools/{pool}/properties — change them
POST /pools/{pool}/upgrade|checkpoint|reguid — feature upgrade, checkpoint, new GUID
"""

import asyncio
import json
import logging
import posixpath
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import pools as pool_devices
from freedomnas import datasets as zfs_datasets
from freedomnas import pool_import
from freedomnas import pool_removal
from freedomnas import shadow
from freedomnas import keymgmt
from freedomnas.keymgmt import PoolProvisioner, KeyManagementError
from api.deps import authenticate, get_config, get_engine
from api.routes.shares import (
    add_nfs_share, add_smb_share, move_share, nfs_share_problem, remove_share, shares_under,
)
from api.routes.users import create_account, password_problem

logger = logging.getLogger("freedomnas.webui.storage")

router = APIRouter()


@router.get("/pools")
async def list_pools(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "pool_list")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    # What is attached as cache or log is read from the pool itself; what
    # config.yaml recorded is only the fallback for when ZFS cannot say.
    recorded = {p.get("name"): p for p in get_config().get_pools()}
    try:
        rows = zfs_datasets.listing(engine)
    except (CommandExecutionError, CommandTranslationError):
        rows = []
    system_pool = zfs_datasets.root_pool(engine)
    active = shadow.active_dataset(engine)

    pools = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            entry = {"name": parts[0], "size": _bytes(parts[1])}
            # `zpool list -Hp -o name,size,alloc,free,capacity,health`: sizes
            # in bytes, capacity as a bare percentage. A profile that prints
            # fewer columns still gives a name and a size.
            if len(parts) >= 3:
                entry["used"] = _bytes(parts[2])
            if len(parts) >= 4:
                entry["free"] = _bytes(parts[3])
            if len(parts) >= 5:
                entry["used_pct"] = _bytes(parts[4].rstrip("%"))
            if len(parts) >= 6:
                entry["health"] = parts[5]
            entry["protected"] = pool_removal.problem(engine, parts[0], rows, system_pool, active)
            known = recorded.get(parts[0], {})
            for key in ("cache_devices", "log_devices", "vdev_type", "encrypted"):
                if known.get(key):
                    entry[key] = known[key]
            live = pool_devices.attached(engine, parts[0])
            if live is not None:
                for role in ("cache", "log"):
                    entry.pop(f"{role}_devices", None)
                    if live[role]:
                        entry[f"{role}_devices"] = live[role]
            pools.append(entry)
    return {"pools": pools}


def _bytes(value: str):
    """
    `-p` prints exact byte counts, which the page formats itself. A profile
    whose command lacks `-p` prints `3.6T`, and that goes through unchanged
    rather than becoming a wrong number.
    """
    try:
        return int(value)
    except ValueError:
        return value


@router.get("/datasets")
async def list_datasets(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        stdout, _ = engine.execute("zfs", "dataset_list")
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    system_pool = zfs_datasets.root_pool(engine)
    rows = [(p[0], p[4]) for p in (line.split("\t") for line in stdout.splitlines()) if len(p) >= 5]
    datasets = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            datasets.append({
                "name": parts[0],
                "used": _bytes(parts[1]),
                "avail": _bytes(parts[2]),
                "refer": _bytes(parts[3]),
                "mountpoint": parts[4],
                "protected": zfs_datasets.subtree_reason(rows, parts[0], system_pool),
            })
    return {"datasets": datasets}


@router.post("/datasets")
async def create_dataset(body: dict, username: str = Depends(authenticate)):
    parent = str(body.get("parent") or "").strip()
    name = str(body.get("name") or "").strip()
    mode = str(body.get("mountpoint_mode") or "inherit")
    mountpoint = str(body.get("mountpoint") or "").strip()
    wanted = {str(k): str(v).strip() for k, v in (body.get("properties") or {}).items()
              if str(v).strip() != ""}
    catalogue = _catalogue()
    errors = [e for e in (_validate(catalogue, k, v, creating=True) for k, v in wanted.items()) if e]
    if errors:
        return JSONResponse(status_code=422, content={"detail": "; ".join(errors)})
    owner = str(body.get("owner") or "").strip() or username
    engine = get_engine()
    try:
        engine.execute("users", "get_user_info", username=owner)
    except CommandExecutionError:
        return JSONResponse(status_code=422, content={"detail": f"There is no account named {owner}"})
    try:
        created = await asyncio.to_thread(
            zfs_datasets.create, engine, parent, name, mode, mountpoint, wanted)
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    # The dataset exists from here on: a directory that could not be handed
    # over is reported, not a reason to call the creation failed.
    result = {"dataset": created, "owner": None}
    try:
        where, _ = await asyncio.to_thread(zfs_datasets.mountpoint_of, engine, created)
        if where.startswith("/"):
            await asyncio.to_thread(zfs_datasets.give_to, engine, where, owner)
            result["owner"] = owner
    except (zfs_datasets.DatasetProblem, CommandExecutionError, CommandTranslationError) as exc:
        result["warning"] = f"{created} was created, but its directory was not given to {owner}: {exc}"
    return result


def _policies_of(config, name: str):
    return [p for p in config.get_snapshot_policies()
            if p.get("dataset") == name or str(p.get("dataset", "")).startswith(name + "/")]


@router.get("/datasets/{dataset:path}/dependents")
async def dataset_dependents(dataset: str, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    try:
        found = await asyncio.to_thread(zfs_datasets.dependents, engine, dataset)
        mountpoint, source = await asyncio.to_thread(zfs_datasets.mountpoint_of, engine, dataset)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    return {
        "dataset": dataset,
        "mountpoint": mountpoint,
        "mountpoint_source": source,
        "children": found["children"],
        "snapshots": found["snapshots"],
        "shares": shares_under(config, mountpoint),
        "policies": [p["dataset"] for p in _policies_of(config, dataset)],
        "protected": zfs_datasets.subtree_reason(
            zfs_datasets.listing(engine), dataset, zfs_datasets.root_pool(engine)),
    }


def _follow_shares(engine, config, old: str, new: str, update: bool) -> dict:
    """Shares under a moved dataset: pointed at the new place, or reported as left behind."""
    moved, left, warnings = [], [], []
    if old == new:
        return {"moved": moved, "left": left, "warnings": warnings}
    for kind, shares in shares_under(config, old).items():
        for share in shares:
            target = new + share["path"][len(old):] if new.startswith("/") else ""
            if not update or not target or not posixpath.isdir(target):
                left.append(f"{kind}:{share['name']}")
                continue
            try:
                move_share(engine, config, kind, share, target)
                moved.append(f"{kind}:{share['name']}")
            except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
                left.append(f"{kind}:{share['name']}")
                warnings.append(f"{kind.upper()} share {share['name']} not moved: {exc}")
    if left:
        warnings.append("These shares still point at the old place: " + ", ".join(left))
    return {"moved": moved, "left": left, "warnings": warnings}


@router.put("/datasets/{dataset:path}/mountpoint")
async def move_dataset(dataset: str, body: dict, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    try:
        change = await asyncio.to_thread(
            zfs_datasets.set_mountpoint, engine, dataset,
            str(body.get("mode") or ""), str(body.get("mountpoint") or "").strip())
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    shares = await asyncio.to_thread(
        _follow_shares, engine, config, change["old"], change["new"], bool(body.get("update_shares")))
    return {"dataset": dataset, **change, "shares": shares}


@router.post("/datasets/{dataset:path}/rename")
async def rename_dataset(dataset: str, body: dict, username: str = Depends(authenticate)):
    new_name = str(body.get("new_name") or "").strip()
    engine, config = get_engine(), get_config()
    try:
        change = await asyncio.to_thread(zfs_datasets.rename, engine, dataset, new_name)
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    for policy in _policies_of(config, dataset):
        keep = {k: v for k, v in policy.items() if k != "dataset"}
        config.remove_snapshot_policy(policy["dataset"])
        config.set_snapshot_policy(new_name + policy["dataset"][len(dataset):], keep)
    shares = await asyncio.to_thread(
        _follow_shares, engine, config, change["old"], change["new"], bool(body.get("update_shares")))
    return {"dataset": new_name, **change, "shares": shares}


@router.delete("/datasets/{dataset:path}")
async def destroy_dataset(dataset: str, confirm: str = "", recursive: bool = False,
                          remove_shares: bool = False, username: str = Depends(authenticate)):
    if confirm != dataset:
        return JSONResponse(status_code=422, content={
            "detail": "Type the dataset's full name to confirm"})
    engine, config = get_engine(), get_config()
    try:
        mountpoint, _ = await asyncio.to_thread(zfs_datasets.mountpoint_of, engine, dataset)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    found = shares_under(config, mountpoint)
    if (found["smb"] or found["nfs"]) and not remove_shares:
        names = [s["name"] for s in found["smb"] + found["nfs"]]
        return JSONResponse(status_code=422, content={
            "detail": f"Shares use it ({', '.join(names)}): tick that they go too"})
    try:
        await asyncio.to_thread(zfs_datasets.destroy, engine, dataset, recursive)
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    warnings, removed = [], []
    for kind, shares in found.items():
        for share in shares:
            try:
                remove_share(engine, config, kind, share)
                removed.append(f"{kind}:{share['name']}")
            except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
                warnings.append(f"{kind.upper()} share {share['name']} not removed: {exc}")
    for policy in _policies_of(config, dataset):
        config.remove_snapshot_policy(policy["dataset"])
    return {"dataset": dataset, "removed_shares": removed, "warnings": warnings}


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


def _validate(catalogue: dict, name: str, value: str, creating: bool = False):
    """Returns an error string, or None when the value is acceptable."""
    spec = catalogue.get(name)
    if spec is None:
        return f"Unknown property: {name}"
    if spec.get("create_only") and not creating:
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
    problem = layout_problem("", devices)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})

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
VDEV_TYPES = ("", "mirror", "raidz1", "raidz2", "raidz3")


def layout_problem(vdev_type: str, *groups):
    """
    Devices and the vdev type reach zpool as several arguments, split on
    whitespace, and the engine cannot refuse a leading '-' there as it does
    for single values: the installer passes -f that way on purpose. So the
    vdev type is one zpool knows and a device is one /dev path, or `-f`
    among the devices would take a disk that belongs to another pool.
    """
    if vdev_type not in VDEV_TYPES:
        return f"vdev_type must be empty or one of {', '.join(VDEV_TYPES[1:])}"
    for device in (d for group in groups for d in group):
        if (not device.startswith("/dev/")
                or posixpath.normpath(device) != device
                or any(c.isspace() or not c.isprintable() for c in device)):
            return f"{device!r} is not a device path under /dev/"
    return None


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

    # Every edition answers in `lsblk -P` form: FreeBSD's profile builds the
    # same fields from diskinfo, gpart, fstyp, zpool and mount.
    devices = []
    parents = {}
    for line in stdout.splitlines():
        fields = dict(_LSBLK_FIELD.findall(line))
        if not fields.get("NAME"):
            continue
        parents[fields["NAME"]] = fields.get("PKNAME", "")
        reserved = fields.get("PARTTYPE", "").lower() == ZFS_RESERVED_PARTTYPE
        devices.append({
            "name": fields["NAME"],
            "parent": fields.get("PKNAME", ""),
            "size": int(fields.get("SIZE") or 0),
            "type": fields.get("TYPE", ""),
            "fstype": fields.get("FSTYPE", ""),
            "mountpoint": fields.get("MOUNTPOINT", ""),
            "model": fields.get("MODEL", ""),
            "in_use": bool(fields.get("FSTYPE") or fields.get("MOUNTPOINT") or reserved),
            "reserved": reserved,
        })

    # A disk whose partitions hold the running system shows no filesystem of
    # its own: it is in use all the same, and must not be offered for a pool.
    busy = set()
    for device in devices:
        name = device["name"]
        if device["in_use"]:
            while parents.get(name):
                name = parents[name]
                busy.add(name)
    for device in devices:
        if device["name"] in busy:
            device["in_use"] = True
    return {"devices": devices}


# The small partition ZFS makes beside its own when it is given a whole
# disk ("Solaris reserved 1"): it holds no filesystem, so nothing else would
# mark it as taken.
ZFS_RESERVED_PARTTYPE = "6a945a3b-1dd2-11b2-99a6-080020736631"


@router.post("/pools")
async def create_pool(body: dict, username: str = Depends(authenticate)):
    name = (body.get("name") or "").strip()
    vdev_type = (body.get("vdev_type") or "").strip()
    devices = [str(d).strip() for d in (body.get("devices") or []) if str(d).strip()]
    cache = [str(d).strip() for d in (body.get("cache_devices") or []) if str(d).strip()]
    log = [str(d).strip() for d in (body.get("log_devices") or []) if str(d).strip()]
    groups = body.get("vdevs") or []
    if groups:
        try:
            spec = _vdev_words(groups)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        devices = [d for g in groups for d in g.get("devices") or []]
        vdev_type = ""
    encrypt = bool(body.get("encrypted"))
    passphrase = body.get("master_passphrase") or ""

    if not _POOL_NAME.match(name):
        return JSONResponse(status_code=422, content={
            "detail": "A pool name starts with a letter and holds letters, digits, _ . : -"})
    if not devices:
        return JSONResponse(status_code=422, content={"detail": "Select at least one device"})
    if not groups:
        spec = devices
    problem = layout_problem(vdev_type, devices, cache, log)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})

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
    usb = [str(d).strip() for d in (body.get("usb_devices") or []) if str(d).strip()]
    if usb and not encrypt:
        return JSONResponse(status_code=422, content={
            "detail": "A USB key only unlocks an encrypted pool"})
    if usb:
        removable = set(keymgmt.removable_partitions(engine))
        for device in usb:
            if device not in removable:
                return JSONResponse(status_code=422, content={
                    "detail": f"{device} is not a partition of a removable USB device"})
            if device in seen:
                return JSONResponse(status_code=422, content={
                    "detail": f"{device} is listed twice — a device belongs to one role"})
    try:
        if encrypt:
            PoolProvisioner(engine).create_encrypted_pool(
                pool_name=name,
                vdev_type=vdev_type,
                devices=" ".join(spec),
                master_passphrase=passphrase,
                usb_devices=usb,
            )
        else:
            engine.execute(
                "zfs", "pool_create",
                pool_name=name,
                vdev_type=vdev_type,
                devices=" ".join(spec),
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

    record = {
        "name": name,
        "vdev_type": vdev_type,
        "encrypted": encrypt,
        "cache_devices": attached["cache"],
        "log_devices": attached["log"],
    }
    if groups:
        record["vdevs"] = [_vdev_record(g) for g in groups]
    config.add_pool(record)
    return {"pool": name, "attached": attached, "warnings": attached["errors"]}


@router.get("/usb-devices")
async def usb_devices(username: str = Depends(authenticate)):
    engine = get_engine()
    return {"devices": await asyncio.to_thread(keymgmt.removable_partitions, engine)}


@router.get("/pools/importable")
async def importable_pools(username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        pools = await asyncio.to_thread(pool_import.list_importable, engine)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"pools": pools}


def _import_request(body: dict):
    pool_id = str(body.get("pool_id") or "").strip()
    name = (body.get("name") or "").strip() or None
    mountpoint = (body.get("mountpoint") or "").strip() or None
    passphrases = body.get("passphrases") or {}
    if not pool_id.isdigit():
        return None, "Choose a pool to import"
    if name and not _POOL_NAME.match(name):
        return None, "A pool name starts with a letter and holds letters, digits, _ . : -"
    if not isinstance(passphrases, dict):
        return None, "passphrases must map encryption roots to passphrases"
    unlock = body.get("keyslot_unlock") or None
    if unlock is not None:
        if not isinstance(unlock, dict) or unlock.get("method") not in ("usb", "file", "passphrase"):
            return None, "keyslot_unlock.method is usb, file or passphrase"
        if len(str(unlock.get("key") or "")) > 4096:
            return None, "The key file is too large to be a freedomnas.key"
        unlock = {k: str(v) for k, v in unlock.items() if k in ("method", "key", "passphrase")}
    return {
        "pool_id": pool_id,
        "name": name,
        "force": bool(body.get("force")),
        "root_mountpoint": mountpoint,
        "passphrases": {str(k): str(v) for k, v in passphrases.items()},
        "keyslot_unlock": unlock,
    }, None


def _pool_names(engine):
    stdout, _ = engine.execute("zfs", "pool_list", check=False)
    return {line.split("\t")[0] for line in stdout.splitlines() if line.strip()}


def _user_exists(engine, who) -> bool:
    try:
        stdout, _ = engine.execute("users", "get_user_info", username=str(who), check=False)
    except (CommandExecutionError, CommandTranslationError):
        return False
    return bool(stdout.strip())


def _group_exists(engine, group: str) -> bool:
    try:
        stdout, _ = engine.execute("users", "group_exists", group=group, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return False
    return bool(stdout.strip())


def _annotate(engine, config, report: dict) -> dict:
    """What the report means on this system: taken names, existing users and groups."""
    final = report["pool"]["final_name"]
    report["name_taken"] = final in _pool_names(engine)
    smb_here = {s.get("name") for s in config.get_smb_shares()}
    nfs_here = {s.get("name") for s in config.get_nfs_shares()}
    for share in report["shares"]["smb"]:
        share["exists_here"] = share["name"] in smb_here
    for share in report["shares"]["nfs"]:
        share["exists_here"] = share["name"] in nfs_here
    known = {u["username"] for u in report["users"]}
    for share in report["shares"]["smb"]:
        for user in share.get("users", []):
            if user not in known:
                report["users"].append({"username": user, "uid": None, "groups": [],
                                        "shell": "/bin/bash", "admin": False})
                known.add(user)
    for user in report["users"]:
        user["exists_here"] = _user_exists(engine, user["username"])
        user["uid_taken"] = bool(user["uid"]) and not user["exists_here"] and _user_exists(engine, user["uid"])
        user["groups_here"] = [g for g in user["groups"]
                               if g not in pool_import.ADMIN_GROUPS and _group_exists(engine, g)]
    return report


@router.post("/pools/import/inspect")
async def inspect_pool(body: dict, username: str = Depends(authenticate)):
    request, problem = _import_request(body)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    engine, config = get_engine(), get_config()
    try:
        report = await asyncio.to_thread(pool_import.inspect, engine, **request)
        report = await asyncio.to_thread(_annotate, engine, config, report)
    except pool_import.ImportProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return report


def _blocking_problem(report: dict):
    if report["refused"]:
        return ("It cannot be imported: it holds another system's root ("
                + "; ".join(report["refused"]) + ")")
    if report["name_taken"]:
        return f"A pool named {report['pool']['final_name']} already exists here: choose another name"
    if report["conflicts"]:
        c = report["conflicts"][0]
        return (f"{c['dataset'] or report['pool']['final_name']} would mount on {c['mountpoint']}: "
                f"{c['reason']}. Choose another mountpoint for the pool")
    for root in report["encryption_roots"]:
        if root.get("blocking"):
            return f"{root['dataset'] or report['pool']['final_name']}: {root['error']}"
    return None


def _do_import(engine, config, request: dict, body: dict, admin_user: str) -> dict:
    report = _annotate(engine, config, pool_import.inspect(engine, **request))
    problem = _blocking_problem(report)
    if problem:
        raise pool_import.ImportProblem(problem)

    wanted_smb = set(body.get("smb") or [])
    wanted_nfs = set(body.get("nfs") or [])
    smb = [s for s in report["shares"]["smb"] if s["name"] in wanted_smb]
    nfs = [s for s in report["shares"]["nfs"] if s["name"] in wanted_nfs]
    given = {u.get("username"): u for u in body.get("users") or [] if isinstance(u, dict)}
    foreign = {u["username"]: u for u in report["users"]}

    needed = []
    for share in smb:
        for user in share.get("users", []):
            if user not in needed and not foreign.get(user, {}).get("exists_here"):
                needed.append(user)
    for user in needed:
        if user not in given:
            raise pool_import.ImportProblem(
                f"The share needs the user {user}: give it a password to create it")
    to_create = [name for name in given if name in foreign and not foreign[name]["exists_here"]]
    for name in to_create:
        problem = password_problem(name, given[name].get("password") or "")
        if problem:
            raise pool_import.ImportProblem(f"{name}: {problem}")

    name = report["pool"]["final_name"]
    result = pool_import.import_pool(
        engine, request["pool_id"], name, force=request["force"],
        root_mountpoint=request["root_mountpoint"], passphrases=request["passphrases"],
        keyslot_unlock=request["keyslot_unlock"])

    warnings = []
    try:
        shadow.ensure_dataset(engine, name)
    except (CommandExecutionError, CommandTranslationError) as exc:
        warnings.append(f"No {shadow.DATASET_NAME} dataset on {name}: {exc}")
    locked = [r["dataset"] or name for r in result["encryption_roots"] if not r["unlocked"]]
    for dataset in locked:
        warnings.append(f"{dataset} stays locked")
    config.add_pool({
        "name": name,
        "encrypted": bool(result["encryption_roots"]),
        "imported": True,
    })

    sudo_group = engine.get_profile_value("users", "sudo_group")
    created_users = []
    for user_name in to_create:
        info = foreign[user_name]
        groups = list(info["groups_here"])
        if given[user_name].get("admin"):
            groups.append(sudo_group)
        uid = info["uid"] if info["uid"] and not info["uid_taken"] else None
        if info["uid"] and info["uid_taken"]:
            warnings.append(f"{user_name}: uid {info['uid']} is taken here, so its files on the pool "
                            "keep an owner number this system gives to someone else")
        try:
            create_account(engine, config, user_name, given[user_name]["password"],
                           groups=groups, shell=info["shell"], uid=uid)
            created_users.append(user_name)
        except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
            warnings.append(f"User {user_name} not created: {exc}")

    created_shares = []
    for share in smb:
        if share["exists_here"]:
            warnings.append(f"SMB share {share['name']} already exists here: left as it is")
            continue
        if any(u not in created_users and not foreign.get(u, {}).get("exists_here") for u in share.get("users", [])):
            warnings.append(f"SMB share {share['name']} not created: a user it needs is missing")
            continue
        try:
            add_smb_share(engine, config, {
                "name": share["name"],
                "path": share["path"],
                "comment": share.get("comment") or share["name"],
                "acl": share.get("acl") or f"Everyone:R,{admin_user}:F",
                "read_only": bool(share.get("read_only")),
                "valid_users": list(share.get("valid_users") or []),
            })
            created_shares.append(f"smb:{share['name']}")
        except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
            warnings.append(f"SMB share {share['name']} not created: {exc}")
    for share in nfs:
        if share["exists_here"]:
            warnings.append(f"NFS share {share['name']} already exists here: left as it is")
            continue
        entry = {
            "name": share["name"],
            "path": share["path"],
            "allowed_hosts": share.get("allowed_hosts", ""),
            "options": share.get("options") or "",
            "owner": share.get("owner"),
            "map_to_owner": bool(share.get("map_to_owner")),
        }
        problem = nfs_share_problem(entry)
        if problem:
            warnings.append(f"NFS share {share['name']} not created: {problem}")
            continue
        try:
            add_nfs_share(engine, config, entry)
            created_shares.append(f"nfs:{share['name']}")
        except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
            warnings.append(f"NFS share {share['name']} not created: {exc}")

    return {"pool": name, "users": created_users, "shares": created_shares,
            "kept_config": result.get("kept_config"),
            "adopted_keyslots": result.get("adopted_keyslots", False), "warnings": warnings}


@router.post("/pools/import")
async def import_existing_pool(body: dict, username: str = Depends(authenticate)):
    request, problem = _import_request(body)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    engine, config = get_engine(), get_config()
    try:
        return await asyncio.to_thread(_do_import, engine, config, request, body, username)
    except pool_import.ImportProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


def _pool_shares(config, mountpoints) -> dict:
    found = {"smb": [], "nfs": []}
    for mountpoint in mountpoints:
        if pool_import.is_system_path(mountpoint):
            continue
        for kind, shares in shares_under(config, mountpoint).items():
            for share in shares:
                if all(s["name"] != share["name"] for s in found[kind]):
                    found[kind].append(share)
    return found


def _pool_policies(config, pool: str):
    return [p for p in config.get_snapshot_policies()
            if p.get("dataset") == pool or str(p.get("dataset", "")).startswith(pool + "/")]


@router.get("/pools/{pool_name}/removal")
async def pool_removal_report(pool_name: str, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    try:
        found = await asyncio.to_thread(pool_removal.contents, engine, pool_name)
        reason = await asyncio.to_thread(pool_removal.problem, engine, pool_name)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    return {
        "pool": pool_name,
        "protected": reason,
        "datasets": found["datasets"],
        "encrypted": found["encrypted"],
        "keyslots": found["keyslots"],
        "shares": _pool_shares(config, found["mountpoints"]),
        "policies": [p["dataset"] for p in _pool_policies(config, pool_name)],
    }


def _remove_pool(engine, config, pool: str, action: str, remove_shares: bool) -> dict:
    found = pool_removal.contents(engine, pool)
    shares = _pool_shares(config, found["mountpoints"])
    if (shares["smb"] or shares["nfs"]) and not remove_shares:
        names = [s["name"] for s in shares["smb"] + shares["nfs"]]
        raise pool_removal.RemovalProblem(f"Shares use it ({', '.join(names)}): tick that they go too")
    result = pool_removal.remove(engine, pool, action)

    warnings, removed = [], []
    for kind, items in shares.items():
        for share in items:
            try:
                remove_share(engine, config, kind, share)
                removed.append(f"{kind}:{share['name']}")
            except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
                warnings.append(f"{kind.upper()} share {share['name']} not removed: {exc}")
    for policy in _pool_policies(config, pool):
        config.remove_snapshot_policy(policy["dataset"])
    config.remove_pool(pool)
    return {**result, "removed_shares": removed, "warnings": warnings}


@router.post("/pools/{pool_name}/export")
async def export_pool(pool_name: str, body: dict, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    try:
        return await asyncio.to_thread(
            _remove_pool, engine, config, pool_name, "export", bool(body.get("remove_shares")))
    except pool_removal.RemovalProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.delete("/pools/{pool_name}")
async def destroy_pool(pool_name: str, confirm: str = "", remove_shares: bool = False,
                       username: str = Depends(authenticate)):
    if confirm != pool_name:
        return JSONResponse(status_code=422, content={"detail": "Type the pool's name to confirm"})
    engine, config = get_engine(), get_config()
    try:
        return await asyncio.to_thread(_remove_pool, engine, config, pool_name, "destroy", remove_shares)
    except pool_removal.RemovalProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Pool administration: status, maintenance, devices, properties
#
# Everything ZFS can do to a pool after it exists. The WebUI offers these at
# the Advanced and Expert levels only; the refusals that matter (a vdev that
# cannot be removed, a mirror that would lose its last copy) come from ZFS.
# ---------------------------------------------------------------------------

def _vdev_record(group: dict) -> dict:
    return {"class": group.get("class") or "data", "type": group.get("type") or "",
            "devices": [str(d) for d in group.get("devices") or []]}


def _vdev_words(groups) -> list:
    if not isinstance(groups, list):
        raise ValueError("vdevs must be a list")
    clean = [_vdev_record(g) for g in groups if isinstance(g, dict)]
    problem = layout_problem("", *[g["devices"] for g in clean])
    if problem:
        raise ValueError(problem)
    return pool_devices.vdev_spec(clean)


def _device_problem(device: str):
    if not device or any(c.isspace() or not c.isprintable() for c in device) or device.startswith("-"):
        return f"{device!r} is not a device of the pool"
    return None


async def _run(action: str, timeout: int = 60, **kwargs):
    engine = get_engine()
    try:
        stdout, _ = await asyncio.to_thread(engine.execute, "zfs", action, timeout=timeout, **kwargs)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return None, JSONResponse(status_code=500, content={"detail": str(exc)})
    return stdout, None


MAINTENANCE = {
    "scrub": {"start": "pool_scrub", "pause": "pool_scrub_pause", "stop": "pool_scrub_stop"},
    "trim": {"start": "pool_trim", "suspend": "pool_trim_suspend", "cancel": "pool_trim_cancel"},
    "initialize": {"start": "pool_initialize", "cancel": "pool_initialize_cancel"},
}


@router.get("/pools/{pool_name}/status")
async def pool_status(pool_name: str, username: str = Depends(authenticate)):
    engine = get_engine()
    try:
        result = await asyncio.to_thread(pool_devices.status, engine, pool_name)
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    if result is None:
        return JSONResponse(status_code=404, content={"detail": f"No pool called {pool_name}"})
    result["system"] = pool_name == zfs_datasets.root_pool(engine)
    return result


@router.post("/pools/{pool_name}/{task}")
async def pool_maintenance(pool_name: str, task: str, body: dict,
                           username: str = Depends(authenticate)):
    if task in MAINTENANCE:
        action = MAINTENANCE[task].get(body.get("action") or "start")
        if action is None:
            return JSONResponse(status_code=422, content={
                "detail": f"{task} takes one of {', '.join(MAINTENANCE[task])}"})
        _, err = await _run(action, pool_name=pool_name)
        return err or {"pool": pool_name, task: body.get("action") or "start"}
    if task == "clear":
        device = (body.get("device") or "").strip()
        if device:
            problem = _device_problem(device)
            if problem:
                return JSONResponse(status_code=422, content={"detail": problem})
            _, err = await _run("pool_clear_device", pool_name=pool_name, device=device)
        else:
            _, err = await _run("pool_clear", pool_name=pool_name)
        return err or {"pool": pool_name, "cleared": device or pool_name}
    if task == "checkpoint":
        action = "pool_checkpoint_discard" if body.get("discard") else "pool_checkpoint"
        _, err = await _run(action, pool_name=pool_name)
        return err or {"pool": pool_name, "checkpoint": "discarded" if body.get("discard") else "created"}
    if task in ("upgrade", "reguid"):
        if pool_name == zfs_datasets.root_pool(get_engine()):
            return JSONResponse(status_code=422, content={
                "detail": f"Not on the pool this system boots from: after {task} the boot "
                          "loader or the initramfs may no longer find it"})
        _, err = await _run(f"pool_{task}", pool_name=pool_name)
        return err or {"pool": pool_name, task: True}
    if task == "device":
        return await _device_action(pool_name, body)
    if task == "vdevs":
        return await _add_vdevs(pool_name, body)
    return JSONResponse(status_code=404, content={"detail": f"Unknown pool task: {task}"})


DEVICE_ACTIONS = {
    "online": "pool_online", "offline": "pool_offline",
    "offline_temporary": "pool_offline_temporary", "expand": "pool_expand",
    "replace": "pool_replace", "attach": "pool_attach", "detach": "pool_detach",
    "remove": "pool_remove_device", "cancel_remove": "pool_remove_cancel",
}


async def _device_action(pool_name: str, body: dict):
    action = body.get("action") or ""
    device = (body.get("device") or "").strip()
    new_device = (body.get("new_device") or "").strip()
    if action not in DEVICE_ACTIONS:
        return JSONResponse(status_code=422, content={
            "detail": f"action must be one of {', '.join(DEVICE_ACTIONS)}"})
    if action == "cancel_remove":
        _, err = await _run("pool_remove_cancel", pool_name=pool_name)
        return err or {"pool": pool_name, "action": action}
    problem = _device_problem(device)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})
    kwargs = {"pool_name": pool_name, "device": device}
    profile_action = DEVICE_ACTIONS[action]
    if action == "attach" or (action == "replace" and new_device):
        problem = layout_problem("", [new_device])
        if problem:
            return JSONResponse(status_code=422, content={"detail": problem})
        kwargs["new_device"] = new_device
    elif action == "replace":
        profile_action = "pool_replace_in_place"
    _, err = await _run(profile_action, timeout=120, **kwargs)
    if err:
        return err
    if action == "remove":
        config = get_config()
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
    return {"pool": pool_name, "action": action, "device": device, "new_device": new_device}


async def _add_vdevs(pool_name: str, body: dict):
    groups = body.get("vdevs") or []
    try:
        words = _vdev_words(groups)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    _, err = await _run("pool_add_vdevs", timeout=120, pool_name=pool_name, devices=" ".join(words))
    if err:
        return err
    config = get_config()
    for pool in config.get_pools():
        if pool.get("name") == pool_name:
            pool["vdevs"] = pool.get("vdevs", []) + [_vdev_record(g) for g in groups]
            config.add_pool(pool)
            break
    return {"pool": pool_name, "added": [_vdev_record(g) for g in groups]}


@router.get("/pools/{pool_name}/history")
async def pool_history(pool_name: str, internal: bool = False,
                       username: str = Depends(authenticate)):
    stdout, err = await _run("pool_history_internal" if internal else "pool_history",
                             pool_name=pool_name)
    if err:
        return err
    lines = [ln for ln in stdout.splitlines() if ln and not ln.startswith("History for")]
    return {"pool": pool_name, "internal": internal, "lines": lines}


def _pool_catalogue():
    return get_engine().get_profile_section("zpool_properties")


@router.get("/pool-properties")
async def list_pool_properties(username: str = Depends(authenticate)):
    return {"properties": _pool_catalogue()}


@router.get("/pools/{pool_name}/properties")
async def get_pool_properties(pool_name: str, username: str = Depends(authenticate)):
    stdout, err = await _run("pool_get_all", pool_name=pool_name)
    if err:
        return err
    catalogue = _pool_catalogue()
    found = pool_devices.parse_properties(stdout)
    return {"pool": pool_name,
            "properties": {k: v for k, v in found.items() if k in catalogue}}


@router.put("/pools/{pool_name}/properties")
async def set_pool_properties(pool_name: str, body: dict, username: str = Depends(authenticate)):
    catalogue = _pool_catalogue()
    wanted = {k: str(v) for k, v in body.items()}
    if not wanted:
        return JSONResponse(status_code=422, content={"detail": "Nothing to set"})
    errors = []
    for name, value in wanted.items():
        spec = catalogue.get(name)
        if spec is None:
            errors.append(f"Unknown pool property: {name}")
        elif spec["type"] == "enum" and value not in spec["values"]:
            errors.append(f"{name} accepts {', '.join(spec['values'])} — not {value!r}")
        elif spec["type"] == "text" and (len(value) > 256 or not value.isprintable()):
            errors.append(f"{name} must be printable text of at most 256 characters")
    if errors:
        return JSONResponse(status_code=422, content={"detail": "; ".join(errors)})
    applied, failed = [], []
    for name, value in wanted.items():
        _, err = await _run("pool_set", pool_name=pool_name, property=name, value=value)
        if err:
            failed.append({"property": name, "error": json.loads(err.body)["detail"]})
        else:
            applied.append(name)
    return JSONResponse(status_code=200 if not failed else 207,
                        content={"pool": pool_name, "applied": applied, "failed": failed})
