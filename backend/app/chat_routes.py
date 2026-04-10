"""
backend.app.chat_routes
========================
FastAPI router for the global AI chat endpoints.

Endpoints
---------
GET  /api/chat   [requires Authorization: Bearer <API_KEY>]   list persisted chat history
POST /api/chat   [requires Authorization: Bearer <API_KEY>]   send a message and persist it
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session, select

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
_GLOBAL_CHAT_HISTORY_LIMIT = 20

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
# Schemas
# ---------------------------------------------------------------------------


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    domain_profile_id: str | None = None


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    context: ChatContext = Field(default_factory=ChatContext)


class ChatHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    messages: list[ChatMessageResponse]
    tools_available: list[str]


class ChatPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    context: ChatContext = Field(default_factory=ChatContext)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class ChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    reply: str
    tools_available: list[str]
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(model: BaseModel, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))


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


def _db_session() -> Session | None:
    try:
        from backend.app.database import get_engine
    except RuntimeError:
        return None

    try:
        return Session(get_engine())
    except RuntimeError:
        return None


def _message_to_response(message: Any) -> ChatMessageResponse:
    created_at = message.created_at
    if isinstance(created_at, datetime):
        created_at_str = created_at.isoformat()
    else:
        created_at_str = str(created_at)
    return ChatMessageResponse(
        id=str(message.id),
        role=message.role,
        content=message.content,
        created_at=created_at_str,
        context=ChatContext(
            session_id=getattr(message, "session_id", None),
            domain_profile_id=getattr(message, "domain_profile_id", None),
        ),
    )


def _new_ephemeral_message(
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=str(uuid.uuid4()),
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
        context=context,
    )


def _load_recent_history(db: Session | None) -> list[Any]:
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    history = db.exec(
        select(GlobalChatMessage)
        .order_by(GlobalChatMessage.created_at.desc())
        .limit(_GLOBAL_CHAT_HISTORY_LIMIT)
    ).all()
    return list(reversed(history))


def _list_persisted_messages(db: Session | None) -> list[Any]:
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    return db.exec(select(GlobalChatMessage).order_by(GlobalChatMessage.created_at.asc())).all()


def _persist_message(
    db: Session | None,
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
) -> ChatMessageResponse:
    if db is None:
        return _new_ephemeral_message(role, content, context)

    from backend.app.models import GlobalChatMessage

    message = GlobalChatMessage(
        role=role,
        content=content,
        session_id=context.session_id,
        domain_profile_id=context.domain_profile_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return _message_to_response(message)


def _call_openai_chat(message: str, api_key: str, history: list[Any] | None = None) -> str:
    """Call OpenAI Chat Completions and return the assistant reply text."""
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    url = _build_completions_url(base_url)

    prompt_messages: list[dict[str, str]] = [{"role": "system", "content": _CHAT_SYSTEM_PROMPT}]
    for item in history or []:
        if item.role in ("user", "assistant", "system"):
            prompt_messages.append({"role": item.role, "content": item.content})
    prompt_messages.append({"role": "user", "content": message})

    payload = {
        "model": model,
        "messages": prompt_messages,
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
# GET /api/chat
# ---------------------------------------------------------------------------


@router.get("/chat", status_code=200, dependencies=[Depends(require_auth)])
def list_chat_messages() -> JSONResponse:
    """Return persisted global chat history."""
    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        messages = _list_persisted_messages(db)
        return _json_response(
            ChatHistoryResponse(
                messages=[_message_to_response(message) for message in messages],
                tools_available=_TOOLS_AVAILABLE,
            )
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------


@router.post("/chat", status_code=200, dependencies=[Depends(require_auth)])
async def chat(body: dict[str, Any]) -> JSONResponse:
    """
    Send a message to the UI Blueprint assistant.

    Returns the legacy ``reply`` field while also exposing persisted message
    objects for deterministic clients.
    """
    try:
        request = ChatPostRequest.model_validate(body or {})
    except ValidationError as exc:
        if any(error["loc"] == ("message",) for error in exc.errors()):
            return _error(
                400,
                "invalid_request",
                "message is required and must not be empty.",
            )
        return _error(
            422,
            "invalid_request",
            "Request body failed validation.",
            {"errors": exc.errors()},
        )

    message = request.message
    context = request.context

    db = _db_session()
    try:
        user_message = _persist_message(db, "user", message, context)
        history = _load_recent_history(db)

        # Read OPENAI_API_KEY at call time — never returned or logged.
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if not openai_api_key:
            reply = _stub_reply(message)
        else:
            try:
                reply = _call_openai_chat(message, openai_api_key, history[:-1] if history else [])
            except httpx.TimeoutException:
                return _error(
                    502,
                    "ai_provider_error",
                    "Chat request timed out.",
                    {"hint": "timeout"},
                )
            except httpx.RequestError:
                return _error(
                    502,
                    "ai_provider_error",
                    "Network error contacting AI.",
                    {"hint": "network_error"},
                )
            except (httpx.HTTPStatusError, KeyError, IndexError, ValueError):
                return _error(
                    502,
                    "ai_provider_error",
                    "Invalid response from AI.",
                    {"hint": "invalid_response"},
                )

        assistant_message = _persist_message(db, "assistant", reply, context)
        return _json_response(
            ChatPostResponse(
                reply=reply,
                tools_available=_TOOLS_AVAILABLE,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        )
    finally:
        if db is not None:
            db.close()
