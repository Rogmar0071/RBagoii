"""
backend.app.auth
================
Shared bearer-token authentication dependency for FastAPI routers.

Reads API_KEY from the environment at *call time* (not import time) so that
environment-variable overrides in tests take effect without module reloading.

Security note
-------------
API_KEY MUST be set before deployment.  If API_KEY is not configured the
application raises ``RuntimeError("AUTHENTICATION_NOT_CONFIGURED")``
enforcing the ``no_open_mode`` invariant — unauthenticated access is never
silently permitted.
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
    be protected.

    Raises
    ------
    RuntimeError
        If API_KEY env var is not set (``no_open_mode`` invariant — the system
        must not silently allow all requests when authentication is unconfigured).
    HTTPException 401
        If the Authorization header is missing or malformed.
    HTTPException 403
        If the token does not match API_KEY.
    """
    api_key: str = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "AUTHENTICATION_NOT_CONFIGURED: API_KEY environment variable is not set. "
            "Set API_KEY to a strong secret before deployment."
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or invalid Authorization header"
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
