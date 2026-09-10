"""
Shared FastAPI dependencies: engine singleton, config singleton, PAM auth.

All route handlers obtain engine and config through these functions — never
by instantiating them directly (spec §7.4).
"""

import logging
import threading
import time

import pam
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from freedomnas.engine import (
    CommandExecutionError,
    CommandTranslationError,
    FreedomNASEngine,
    get_engine as _get_engine,
)
from freedomnas.config import ConfigurationManager

logger = logging.getLogger("freedomnas.webui.auth")

security = HTTPBasic()

# Password guessing over the network is otherwise limited only by how fast PAM
# answers. Five wrong answers inside a minute lock that username out for five.
# The trade-off is deliberate and worth knowing: someone who can reach the port
# can keep an administrator locked out by guessing wrong on purpose.
_MAX_FAILURES = 5
_WINDOW_SECONDS = 60.0
_LOCKOUT_SECONDS = 300.0

_auth_lock = threading.Lock()
_failures: dict = {}
_locked_until: dict = {}

_config: ConfigurationManager | None = None


def get_engine() -> FreedomNASEngine:
    return _get_engine()


def get_config() -> ConfigurationManager:
    global _config
    if _config is None:
        _config = ConfigurationManager()
    return _config


def verify_credentials(username: str, password: str) -> bool:
    """
    Check a username and password against PAM, with a lockout after repeated
    failures.

    Split out of authenticate() because the updates WebSocket needs the same
    check: a browser cannot set an Authorization header on a WebSocket, so it
    sends the same Basic blob as its first frame instead.
    """
    now = time.monotonic()
    with _auth_lock:
        if now < _locked_until.get(username, 0.0):
            logger.warning("Authentication refused: %s is locked out", username)
            return False

    if pam.pam().authenticate(username, password):
        with _auth_lock:
            _failures.pop(username, None)
            _locked_until.pop(username, None)
        return True

    with _auth_lock:
        recent = [t for t in _failures.get(username, []) if now - t < _WINDOW_SECONDS]
        recent.append(now)
        _failures[username] = recent
        if len(recent) >= _MAX_FAILURES:
            _locked_until[username] = now + _LOCKOUT_SECONDS
            logger.warning(
                "%s locked out for %.0fs after %d failed attempts",
                username, _LOCKOUT_SECONDS, len(recent),
            )
    return False


def is_administrator(username: str) -> bool:
    """
    PAM says who you are; the profile says who may administer.

    Without this, any account that can authenticate — including one created
    only to mount a share — could delete users, change the network or reboot
    the appliance. The admin group is not the same name everywhere, which is
    why it comes from `users.sudo_group` and never from a literal here.
    """
    if username == "root":
        return True
    engine = get_engine()
    try:
        admin_group = engine.get_profile_value("users", "sudo_group")
        groups, _ = engine.execute("users", "list_groups", username=username)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Cannot read the groups of %s: %s", username, exc)
        return False
    return bool(admin_group) and admin_group in groups.split()


def authenticate(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    """
    Verify credentials via PAM and require an administrator.

    401 when the credentials are wrong, 403 when they are right but the
    account is not in the edition's admin group.
    """
    if not verify_credentials(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not is_administrator(credentials.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not an administrator",
        )
    return credentials.username
