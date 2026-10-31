"""
Shared FastAPI dependencies: engine singleton, config singleton, PAM auth.

All route handlers obtain engine and config through these functions — never
by instantiating them directly (spec §7.4).
"""

import base64
import binascii
import logging
import threading
import time
from typing import Optional

import pam
from fastapi import HTTPException, Request, status

from freedomnas.engine import (
    CommandExecutionError,
    CommandTranslationError,
    FreedomNASEngine,
    get_engine as _get_engine,
)
from freedomnas.config import ConfigurationManager
from freedomnas import shadow

logger = logging.getLogger("freedomnas.webui.auth")

# The credentials are Basic, but the challenge is not: a browser that sees
# `WWW-Authenticate: Basic` on a 401 opens its own login dialog, leaves the
# page's fetch waiting behind it, and afterwards sends whatever was typed there
# on every request by itself — cross-site ones included. The page asks for the
# password itself, so the scheme it announces is one no browser acts on.
AUTH_SCHEME = "FreedomNAS"

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
        _config.on_save.append(shadow.mirror_after_save(get_engine()))
    return _config


PAM_NEW_AUTHTOK_REQD = 12
PASSWORD_EXPIRED = "Password expired"


def verify_credentials(username: str, password: str) -> bool:
    """
    Check a username and password against PAM, with a lockout after repeated
    failures. A temporary password opens nothing until it is changed.

    Split out of authenticate() because the updates WebSocket needs the same
    check: a browser cannot set an Authorization header on a WebSocket, so it
    sends the same Basic blob as its first frame instead.
    """
    return signin_state(username, password) == "ok"


def signin_state(username: str, password: str) -> Optional[str]:
    """
    check_password(), with "expired" also for a password the configuration
    still records as temporary: an edition whose PAM does not say so must not
    let it in either.
    """
    state = check_password(username, password)
    if state == "ok" and (get_config().get_user(username) or {}).get("temporary_password"):
        return "expired"
    return state


def check_password(username: str, password: str) -> Optional[str]:
    """
    "ok", "expired" (the right password, which has to be changed before the
    account is used: a temporary one given after a restore), or None, with
    the same lockout as signing in.
    """
    now = time.monotonic()
    with _auth_lock:
        if now < _locked_until.get(username, 0.0):
            logger.warning("Authentication refused: %s is locked out", username)
            return None

    check = pam.pam()
    if check.authenticate(username, password):
        state = "ok"
    elif getattr(check, "code", None) == PAM_NEW_AUTHTOK_REQD:
        state = "expired"
    else:
        state = None
    if state:
        with _auth_lock:
            _failures.pop(username, None)
            _locked_until.pop(username, None)
        return state

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
    return None


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


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": AUTH_SCHEME},
    )


def _basic_credentials(request: Request) -> tuple:
    """
    Read the Basic credentials here rather than through fastapi's HTTPBasic,
    whose 401 carries `WWW-Authenticate: Basic` in every FastAPI version the
    editions ship.
    """
    scheme, _, blob = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        raise _unauthorized("Not authenticated")
    try:
        decoded = base64.b64decode(blob.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise _unauthorized("Not authenticated")
    username, separator, password = decoded.partition(":")
    if not separator:
        raise _unauthorized("Not authenticated")
    return username, password


# Samba keeps passwords of its own, and an account that existed before
# FreedomNAS (an install onto a running system, a restored one) has none
# there: no password opens a share for it. An account that signs in here
# gives it, once per account and process, the password PAM just accepted.
_samba_checked: set = set()
_samba_lock = threading.Lock()


def _samba_account(username: str, password: str) -> None:
    with _samba_lock:
        if username in _samba_checked:
            return
        _samba_checked.add(username)
    if "\n" in password or "\r" in password:
        return
    engine = get_engine()
    try:
        engine.execute("samba", "check_installed")
        stdout, _ = engine.execute("samba", "user_exists", username=username, check=False)
        if stdout.strip():
            return
        payload = engine.get_profile_value("samba", "password_stdin_format")
        engine.execute_with_stdin("samba", "set_user_password",
                                  payload.format(username=username, password=password), username=username)
        logger.info("%s had no Samba password: given the one used to sign in", username)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Could not check the Samba password of %s: %s", username, exc)


def _signed_in(request: Request) -> tuple:
    """
    The credentials, once PAM accepts them: 401 when they are missing or
    wrong, and 401 with PASSWORD_EXPIRED for a temporary password, which the
    sign-in page then has its owner change (/api/account/password).
    """
    username, password = _basic_credentials(request)
    state = signin_state(username, password)
    if state == "expired":
        raise _unauthorized(PASSWORD_EXPIRED)
    if state != "ok":
        raise _unauthorized("Invalid credentials")
    return username, password


def authenticate_account(request: Request) -> str:
    """
    Verify credentials via PAM, for what every account may see and do for
    itself.
    """
    username, password = _signed_in(request)
    _samba_account(username, password)
    return username


def authenticate(request: Request) -> str:
    """
    Verify credentials via PAM and require an administrator.

    401 as in _signed_in(), 403 when the credentials are right but the
    account is not in the edition's admin group.
    """
    username, password = _signed_in(request)
    if not is_administrator(username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not an administrator",
        )
    _samba_account(username, password)
    return username
