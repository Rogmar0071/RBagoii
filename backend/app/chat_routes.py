"""
backend.app.chat_routes
========================
FastAPI router for the AI chat endpoint.

Endpoint
--------
POST /api/chat  [requires Authorization: Bearer <API_KEY>]

Request body::

    {
      "message": "...",
      "context": {
        "session_id": "...",          // optional
        "domain_profile_id": "..."    // optional
      }
    }

Response::

    {
      "schema_version": "v1.1.0",
      "reply": "...",
      "tools_available": ["domains.derive", ...]
    }

Behaviour
---------
- If OPENAI_API_KEY is not set: deterministic stub reply (no external calls).
- If OPENAI_API_KEY is set: calls OpenAI Chat Completions and returns the
  assistant text.  No tool execution yet; ``tools_available`` is informational.

Environment variables
---------------------
OPENAI_API_KEY          Server-side OpenAI credential (never sent to clients).
OPENAI_MODEL_CHAT       Chat model (default: gpt-4.1-mini).
OPENAI_BASE_URL         Base URL (default: https://api.openai.com).
OPENAI_TIMEOUT_SECONDS  Request timeout in seconds (default: 30).

Security notes
--------------
- Endpoint requires API_KEY bearer auth (same as /v1/sessions).
- OPENAI_API_KEY is read from env at call time and never returned to clients
  or written to logs.
- API_KEY (service access) and OPENAI_API_KEY (OpenAI credential) are
  entirely separate secrets.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.app.auth import require_auth
from ui_blueprint.domain.ir import SCHEMA_VERSION
from ui_blueprint.domain.openai_provider import _build_completions_url

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_CHAT = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 30.0

_TOOLS_AVAILABLE = [
    "domains.derive",
    "domains.confirm",
    "blueprints.compile",
    "sessions.create",
    "sessions.status",
]

_CHAT_SYSTEM_PROMPT = (
    "You are UI Blueprint Assistant, a helpful AI that guides users through "
    "the ui-blueprint pipeline: recording screen clips, deriving domain profiles, "
    "confirming them, and compiling blueprints. "
    "Be concise and practical."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"schema_version": SCHEMA_VERSION, **content},
    )


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def _stub_reply(message: str) -> str:
    return (
        f"[Stub] You said: {message!r}. "
        "AI features are not enabled — set OPENAI_API_KEY on the server to activate them."
    )


def _call_openai_chat(message: str, api_key: str) -> str:
    """Call OpenAI Chat Completions and return the assistant reply text."""
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    url = _build_completions_url(base_url)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "max_tokens": 512,
        "temperature": 0.7,
    }

    with httpx.Client(timeout=timeout) as http:
        response = http.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------


@router.post("/chat", status_code=200, dependencies=[Depends(require_auth)])
async def chat(body: dict[str, Any]) -> JSONResponse:
    """
    Send a message to the UI Blueprint assistant.

    Requires ``Authorization: Bearer <API_KEY>`` header.
    Returns a stub reply when ``OPENAI_API_KEY`` is not configured on the server.
    """
    message: str = str(body.get("message", "")).strip()
    if not message:
        return _error(400, "invalid_request", "message is required and must not be empty.")

    # Read OPENAI_API_KEY at call time — never returned or logged.
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not openai_api_key:
        return _ok(
            {
                "reply": _stub_reply(message),
                "tools_available": _TOOLS_AVAILABLE,
            }
        )

    try:
        reply = _call_openai_chat(message, openai_api_key)
    except httpx.TimeoutException:
        return _error(502, "ai_provider_error", "Chat request timed out.", {"hint": "timeout"})
    except httpx.RequestError:
        return _error(
            502, "ai_provider_error", "Network error contacting AI.", {"hint": "network_error"}
        )
    except (httpx.HTTPStatusError, KeyError, IndexError, ValueError):
        return _error(
            502, "ai_provider_error", "Invalid response from AI.", {"hint": "invalid_response"}
        )

    return _ok(
        {
            "reply": reply,
            "tools_available": _TOOLS_AVAILABLE,
        }
    )
