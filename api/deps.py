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


def authenticate(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    """
    Verify credentials via PAM.  Returns the authenticated username.
    Raises HTTP 401 on failure.
    """
    p = pam.pam()
    if not p.authenticate(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
