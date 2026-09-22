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
from api.deps import authenticate, get_config, get_engine

router = APIRouter()


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
    acl = body.get("acl", f"Everyone:R,{username}:F")
    valid_users = body.get("valid_users", [username])
    read_only = body.get("read_only", False)

    try:
        engine.execute(
            "samba", "create_share",
            share_name=share_name,
            path=path,
            comment=comment,
            acl=acl,
        )
        engine.execute("samba", "reload_config")
        config.add_smb_share({
            "name": share_name,
            "path": path,
            "read_only": read_only,
            "valid_users": valid_users,
            "guest_ok": False,
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"name": share_name, "path": path}


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
        engine.execute("nfs", "export_add", path=path, clients=allowed_hosts, options=options)
        engine.execute("nfs", "exportfs_reload")
        config.add_nfs_share({
            "name": name,
            "path": path,
            "allowed_hosts": allowed_hosts,
            "options": options,
        })
    except (CommandExecutionError, CommandTranslationError, ConfigurationException) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return {"name": name, "path": path}


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
