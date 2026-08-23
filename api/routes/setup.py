"""
/api/setup/* — first-run wizard (always accessible, spec §7.2).

These routes are exempt from the first-run guard middleware.

GET  /status      — whether the system is initialized
POST /initialize  — set hostname, write smb.conf usershare stanza, optionally
                     create an encrypted ZFS pool, mark initialized
POST /topgrade    — configure topgrade (can also be called post-setup)
"""

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from freedomnas.keymgmt import PoolProvisioner, KeyManagementError
from api.deps import get_config, get_engine

router = APIRouter()

MIN_PASSPHRASE_LENGTH = 12


def _validate_pool_cfg(pool_cfg: dict) -> Optional[str]:
    """Return an error message if pool_cfg is invalid, else None."""
    if not pool_cfg.get("name", "").strip():
        return "pool.name is required"
    if not pool_cfg.get("devices"):
        return "pool.devices must be a non-empty list"
    if len(pool_cfg.get("master_passphrase", "")) < MIN_PASSPHRASE_LENGTH:
        return f"pool.master_passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"
    return None


def _provision_encrypted_pool(engine, config, pool_cfg: dict) -> None:
    """
    Create the encrypted pool described by pool_cfg (already validated by
    _validate_pool_cfg) and record its non-secret metadata in config.yaml.
    Never persists the passphrase, the raw ZFS key, or the USB key bytes --
    those live only in the wrapped keyslot file (see
    freedomnas.keymgmt.KeyslotStore).
    """
    pool_name = pool_cfg["name"].strip()
    vdev_type = pool_cfg.get("vdev_type", "").strip()
    devices = pool_cfg["devices"]
    master_passphrase = pool_cfg["master_passphrase"]
    usb_devices = pool_cfg.get("usb_devices", [])

    PoolProvisioner(engine).create_encrypted_pool(
        pool_name=pool_name,
        vdev_type=vdev_type,
        devices=" ".join(devices),
        master_passphrase=master_passphrase,
        usb_devices=usb_devices,
    )

    config.add_pool({
        "name": pool_name,
        "vdev_type": vdev_type,
        "encrypted": True,
        "usb_keyslot_devices": len(usb_devices),
    })


@router.get("/status")
async def setup_status():
    config = get_config()
    return {
        "initialized": config.is_initialized(),
        "edition": config.get_edition(),
    }


@router.post("/initialize")
async def initialize(body: dict):
    hostname = body.get("hostname", "freedomnas").strip()
    timezone = body.get("timezone", "UTC").strip()
    edition = body.get("edition", "").strip()

    if not edition:
        return JSONResponse(status_code=422, content={"detail": "edition is required (debian/freebsd)"})

    pool_cfg = body.get("pool")
    if pool_cfg:
        error = _validate_pool_cfg(pool_cfg)
        if error:
            return JSONResponse(status_code=422, content={"detail": error})

    engine = get_engine()
    config = get_config()

    if config.is_initialized():
        return JSONResponse(status_code=409, content={"detail": "System already initialized"})

    try:
        # Set hostname
        engine.execute("system", "hostname_set", hostname=hostname)
        config.set_hostname(hostname)

        # Set timezone
        engine.execute("system", "timezone_set", timezone=timezone)
        config.set("system.timezone", timezone)

        # Write usershare stanza in smb.conf so shares can be created later (spec §7.6)
        smb_conf = engine.get_profile_value("samba", "config_file")
        usershare_stanza = (
            "\n[global]\n"
            "   usershare path = /var/lib/samba/usershares\n"
            "   usershare max shares = 100\n"
            "   usershare allow guests = no\n"
        )
        with open(smb_conf, "a") as f:
            f.write(usershare_stanza)
        import os
        os.makedirs("/var/lib/samba/usershares", exist_ok=True)

        # Optional: create an encrypted ZFS pool as part of first-run setup
        if pool_cfg:
            _provision_encrypted_pool(engine, config, pool_cfg)

        # Mark initialized
        config.mark_initialized(edition=edition, initialized_by="webui")

    except (
        CommandExecutionError,
        CommandTranslationError,
        ConfigurationException,
        KeyManagementError,
        OSError,
    ) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return {"detail": "System initialized", "hostname": hostname, "edition": edition}
