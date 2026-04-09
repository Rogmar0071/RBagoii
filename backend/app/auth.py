"""
backend.app.auth
================
Shared bearer-token authentication dependency for FastAPI routers.

Reads API_KEY from the environment at *call time* (not import time) so that
environment-variable overrides in tests take effect without module reloading.

Security note
-------------
If API_KEY is not set the application runs in dev/open mode — all requests are
allowed.  Set API_KEY to a strong secret before any public deployment.
API_KEY is a service-level access token and is entirely separate from
OPENAI_API_KEY (the server-side OpenAI credential that is never sent to
clients).
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency — validates the ``Authorization: Bearer <token>`` header.

    Pass as ``dependencies=[Depends(require_auth)]`` on any route that should
    be protected.  When API_KEY env var is empty the dependency is a no-op
    (dev / open mode).
    """
    api_key: str = os.environ.get("API_KEY", "").strip()
    if not api_key:
        # No key configured — allow all requests (dev mode).
        return
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or invalid Authorization header"
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
