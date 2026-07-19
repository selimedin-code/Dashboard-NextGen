"""HTTP Basic Auth guarding the entire app.

Two internal users share one credential pair from the environment. This is
deliberately simple — no user table, no roles (explicitly out of scope in the
roadmap). Constant-time comparison to avoid leaking the secret via timing.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    settings = get_settings()
    user_ok = secrets.compare_digest(credentials.username, settings.basic_auth_user)
    pass_ok = secrets.compare_digest(credentials.password, settings.basic_auth_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
