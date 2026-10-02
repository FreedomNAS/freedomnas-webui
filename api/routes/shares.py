"""
/api/shares/* — SMB and NFS share management.

SMB:
  GET    /smb           — list SMB shares
  POST   /smb           — create SMB share (pre-flight check, spec §7.6)
  DELETE /smb/{name}    — delete SMB share

NFS:
  GET    /nfs           — list NFS shares
  POST   /nfs           — create NFS share
  DELETE /nfs/{name}    — delete NFS share
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas import datasets as zfs_datasets
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


# ---------------------------------------------------------------------------
# Owner
#
# A share is as writable as its directory. When that directory is where a
# dataset of a data pool mounts, it goes to the share's owner (mode 700, see
# freedomnas.datasets.give_to). With no owner asked for, a directory that
# already belongs to someone keeps them — a share made by the administrator
# for a dataset given to another account must not take it back — and one
# still root's goes to whoever creates the share.
# ---------------------------------------------------------------------------

def _share_owner(engine, path: str, requested: str, username: str):
    """(owner, warning) for a new share of `path`."""
    if not zfs_datasets.dataset_mounted_at(engine, path):
        return requested or username, (
            f"{path} is not where a dataset of a data pool is mounted, so its "
            "permissions were left as they are")
    owner = requested
    if not owner:
        current = zfs_datasets.owner_of(engine, path)
        owner = current if current and current != "root" else username
    zfs_datasets.give_to(engine, path, owner)
    return owner, None


# ---------------------------------------------------------------------------
# SMB
# ---------------------------------------------------------------------------

def _smb_preflight(engine) -> bool:
    """
    Verify that the global smb.conf contains usershare path and max shares.
    Returns True if the setup wizard has written those lines (spec §7.6).
    """
    try:
        config_file = engine.get_profile_value("samba", "config_file")
        with open(config_file, "r") as f:
            content = f.read()
        return "usershare path" in content and "usershare max shares" in content
    except (CommandTranslationError, OSError):
        return False


@router.get("/smb")
async def list_smb(username: str = Depends(authenticate)):
    config = get_config()
    return {"shares": config.get_smb_shares()}


@router.post("/smb")
async def create_smb(body: dict, username: str = Depends(authenticate)):
    share_name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    if not share_name or not path:
        return JSONResponse(status_code=422, content={"detail": "name and path are required"})

    engine = get_engine()
    config = get_config()

    # Pre-flight check (spec §7.6) — setup wizard must have run first
    if not _smb_preflight(engine):
        return JSONResponse(
            status_code=409,
            content={"detail": "Samba usershare not configured — run the setup wizard first."},
        )

    comment = body.get("comment", share_name)
    read_only = bool(body.get("read_only", False))
    try:
        owner, warning = _share_owner(
            engine, path, str(body.get("owner") or "").strip(), username)
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    valid_users = body.get("valid_users", [owner])
    # A bare name in a usershare ACL is not looked up as the account, and a
    # NetBIOS-qualified one breaks when the hostname changes: Samba's own
    # "Unix User" domain always names the account itself.
    acl = body.get("acl") or f"Unix User\\{owner}:{'R' if read_only else 'F'}"

    try:
        add_smb_share(engine, config, {
            "name": share_name,
            "path": path,
            "comment": comment,
            "acl": acl,
            "read_only": read_only,
            "valid_users": valid_users,
            "owner": owner,
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    result = {"name": share_name, "path": path, "owner": owner}
    if warning:
        result["warning"] = warning
    return result


def add_smb_share(engine, config, share: dict) -> None:
    engine.execute(
        "samba", "create_share",
        share_name=share["name"],
        path=share["path"],
        comment=share["comment"],
        acl=share["acl"],
    )
    engine.execute("samba", "reload_config")
    config.add_smb_share({**share, "guest_ok": False})


@router.delete("/smb/{share_name}")
async def delete_smb(share_name: str, username: str = Depends(authenticate)):
    engine = get_engine()
    config = get_config()
    try:
        engine.execute("samba", "delete_share", share_name=share_name)
        engine.execute("samba", "reload_config")
        # Remove from config (use generic set to filter the list)
        shares = [s for s in config.get_smb_shares() if s.get("name") != share_name]
        config.set("shares.smb", shares)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"Share {share_name} deleted"}


# ---------------------------------------------------------------------------
# NFS
#
# An export is one line of /etc/exports, and its fields are separated by
# whitespace: a value holding a space or a line break would not be one field
# any more, it would be another field or another export. Those are refused
# here; writing the line is the profile's business (nfs.export_add), since its
# syntax is not the same on every edition.
# ---------------------------------------------------------------------------

def _export_field_problem(name: str, value: str):
    if not value or any(c.isspace() or not c.isprintable() for c in value):
        return f"{name} must be a single word with no spaces or control characters"
    return None

@router.get("/nfs")
async def list_nfs(username: str = Depends(authenticate)):
    config = get_config()
    return {"shares": config.get_nfs_shares()}


@router.post("/nfs")
async def create_nfs(body: dict, username: str = Depends(authenticate)):
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    allowed_hosts = body.get("allowed_hosts", "").strip()
    if not name or not path or not allowed_hosts:
        return JSONResponse(
            status_code=422,
            content={"detail": "name, path and allowed_hosts are required"},
        )

    options = body.get("options", "rw,sync,no_subtree_check")
    for field, value in (("path", path), ("allowed_hosts", allowed_hosts), ("options", options)):
        problem = _export_field_problem(field, value)
        if problem:
            return JSONResponse(status_code=422, content={"detail": problem})

    engine = get_engine()
    config = get_config()
    try:
        owner, warning = _share_owner(
            engine, path, str(body.get("owner") or "").strip(), username)
    except zfs_datasets.DatasetProblem as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except (CommandExecutionError, CommandTranslationError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    try:
        add_nfs_share(engine, config, {
            "name": name,
            "path": path,
            "allowed_hosts": allowed_hosts,
            "options": options,
            "owner": owner,
            # The directory is the owner's alone only when it was given to them.
            "map_to_owner": warning is None,
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    result = {"name": name, "path": path, "owner": owner}
    if warning:
        result["warning"] = warning
    return result


def nfs_share_problem(share: dict):
    for field in ("path", "allowed_hosts", "options"):
        problem = _export_field_problem(field, str(share.get(field, "")))
        if problem:
            return problem
    return None


def add_nfs_share(engine, config, share: dict) -> None:
    engine.execute("nfs", "export_add", path=share["path"],
                   clients=share["allowed_hosts"],
                   options=zfs_datasets.nfs_export_options(engine, share))
    engine.execute("nfs", "exportfs_reload")
    config.add_nfs_share(share)


@router.delete("/nfs/{share_name}")
async def delete_nfs(share_name: str, username: str = Depends(authenticate)):
    config = get_config()
    engine = get_engine()

    share = config.get_nfs_shares()
    entry = next((s for s in share if s.get("name") == share_name), None)
    if not entry:
        return JSONResponse(status_code=404, content={"detail": f"NFS share '{share_name}' not found"})

    try:
        engine.execute("nfs", "export_remove", path=entry["path"])
        engine.execute("nfs", "exportfs_reload")
        config.remove_nfs_share(share_name)
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"detail": f"NFS share {share_name} deleted"}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def shares_under(config, path: str) -> dict:
    """The SMB and NFS shares whose directory is `path` or below it."""
    if not path.startswith("/"):
        return {"smb": [], "nfs": []}
    return {
        "smb": [s for s in config.get_smb_shares() if _under(str(s.get("path", "")), path)],
        "nfs": [s for s in config.get_nfs_shares() if _under(str(s.get("path", "")), path)],
    }


def move_share(engine, config, kind: str, share: dict, new_path: str) -> None:
    """
    Point a share at the directory its dataset moved to. Samba drops a
    usershare whose directory is gone on its own, so the old one may
    already have vanished when this runs.
    """
    if kind == "smb":
        engine.execute("samba", "delete_share", share_name=share["name"], check=False)
        add_smb_share(engine, config, {**share, "path": new_path})
    else:
        engine.execute("nfs", "export_remove", path=share["path"])
        add_nfs_share(engine, config, {**share, "path": new_path})


def remove_share(engine, config, kind: str, share: dict) -> None:
    if kind == "smb":
        engine.execute("samba", "delete_share", share_name=share["name"], check=False)
        engine.execute("samba", "reload_config")
        config.remove_smb_share(share["name"])
    else:
        engine.execute("nfs", "export_remove", path=share["path"])
        engine.execute("nfs", "exportfs_reload")
        config.remove_nfs_share(share["name"])
