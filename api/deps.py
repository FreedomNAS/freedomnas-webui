"""
Shared FastAPI dependencies: engine singleton, config singleton, PAM auth.

All route handlers obtain engine and config through these functions — never
by instantiating them directly (spec §7.4).
"""

import pam
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from freedomnas.engine import FreedomNASEngine, get_engine as _get_engine
from freedomnas.config import ConfigurationManager

security = HTTPBasic()

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
    Check a username and password against PAM.

    Split out of authenticate() because the updates WebSocket needs the same
    check: a browser cannot set an Authorization header on a WebSocket, so it
    sends the same Basic blob as its first frame instead.
    """
    return bool(pam.pam().authenticate(username, password))


def authenticate(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    """
    Verify credentials via PAM.  Returns the authenticated username.
    Raises HTTP 401 on failure.
    """
    if not verify_credentials(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
