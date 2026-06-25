"""
/api/setup/* — first-run wizard (always accessible, spec §7.2).

These routes are exempt from the first-run guard middleware.

GET  /status      — whether the system is initialized
POST /initialize  — set hostname, write smb.conf usershare stanza, mark initialized
POST /topgrade    — configure topgrade (can also be called post-setup)
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from freedomnas.engine import CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationException
from api.deps import get_config, get_engine

router = APIRouter()


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

        # Mark initialized
        config.mark_initialized(edition=edition, initialized_by="webui")

    except (CommandExecutionError, CommandTranslationError, ConfigurationException, OSError) as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return {"detail": "System initialized", "hostname": hostname, "edition": edition}
