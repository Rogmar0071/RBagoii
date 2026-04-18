"""
backend.app.chat_routes
========================
FastAPI router for the global AI chat endpoints.

Endpoints
---------
GET  /api/chat                               list persisted chat history (newest-first)
POST /api/chat                               send a message and persist it
POST /api/chat/{message_id}/edit             create an edited user message (supersedes original)
POST /api/chat/intent                        INTERACTION_LAYER_V2 — parse raw human input into
                                             a deterministic structured intent JSON (never executes)

All AI generation on POST /api/chat is routed through MODE_ENGINE_EXECUTION_V2
(see backend.app.mode_engine) which enforces mode-driven constraints,
post-generation validation, retry control, and mandatory audit logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlmodel import Session, select

from backend.app.artifact_utils import (
    ArtifactItem,
    build_artifact_context_block,
    resolve_context_origin,
    resolve_context_surface,
)
from backend.app.auth import require_auth
from backend.app.mode_engine import (
    MODE_STRICT,
    apply_mode_conflict_resolution,  # noqa: F401 — exported for test introspection
    mode_engine_gateway,
)
from backend.app.models import ChatFile, Repo  # Import ChatFile and Repo models
from backend.app.repo_retrieval import retrieve_relevant_chunks
from ui_blueprint.domain.ir import SCHEMA_VERSION
from ui_blueprint.domain.openai_provider import _build_completions_url
from ui_blueprint.prompt_security import (
    append_prompt_injection_defense,
    format_untrusted_json,
    format_untrusted_text,
)

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_CHAT = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 30.0
_GLOBAL_CHAT_HISTORY_LIMIT = 10

_TOOLS_AVAILABLE = [
    "domains.derive",
    "domains.confirm",
    "blueprints.compile",
    "sessions.create",
    "sessions.status",
    "web_search",
]

_CHAT_SYSTEM_PROMPT = (
    "You are UI Blueprint Assistant — a high-discipline AI that reasons about system "
    "architecture and structural behavior.\n\n"
    "You operate ONLY on data explicitly provided in this conversation.\n\n"
    "When reasoning about any codebase, media, or domain, "
    "you apply a three-pass internal model:\n\n"
    "PASS 1 — TOPOLOGY RECONSTRUCTION\n"
    "1. Identify system components and their relationships.\n"
    "2. Map data flow and control flow.\n"
    "3. Detect structural boundaries and dependencies.\n\n"
    "PASS 2 — INVARIANT DETECTION\n"
    "1. Identify constraints that must not be violated.\n"
    "2. Detect coupling, ownership, and responsibility boundaries.\n"
    "3. Define what must remain stable under change.\n\n"
    "PASS 3 — CONTROLLED MODIFICATION\n"
    "1. Propose only changes that preserve invariants.\n"
    "2. Avoid surface-level fixes that break deeper structure.\n"
    "3. Ensure changes align with system integrity.\n\n"
    "If required data is missing, explicitly state what is missing and request it.\n"
    "Default stance: "
    "'I only operate on data explicitly provided — please supply the relevant artifact.'\n\n"
    "Be concise and practical. Focus on structural causes, not surface symptoms."
)

_OPS_CONTEXT_HEADER = (
    "\n\n--- Optional user-provided context ---\n{snippet}\n--- End of user-provided context ---"
)

# ---------------------------------------------------------------------------
# INTERACTION_LAYER_V2 — system prompt and schema version
# ---------------------------------------------------------------------------

INTERACTION_LAYER_V2_SCHEMA_VERSION = "2"

_INTERACTION_LAYER_V2_SYSTEM_PROMPT = """\
You are INTERACTION_LAYER_V2 — a strict, deterministic intent parser.

ROLE: Convert raw human input into a structured JSON specification.
AUTHORITY: Analysis and specification ONLY. You NEVER execute code changes.
           You NEVER treat your output as execution authority.

OUTPUT RULES:
- Respond with valid JSON only. No markdown. No prose. No explanation outside JSON.
- The JSON must conform exactly to the schema below.
- Do NOT invent file names, component names, or system structure that is not
  explicitly provided in the repo context. If unknown, use null or empty arrays.

OPERATING MODES:
  Mode A — No repo context provided:
    - Set "mode": "A"
    - Set "repoContextProvided": false
    - Set "impactAnalysis.requiresRepoContext": true
    - Set "changePlan.canExecuteDeterministically": false
    - Set "changePlan.requiresStructuralMapping": true
    - Set "changePlan.steps": []
    - Set "changePlan.blockedReason": "Repo context required for deterministic execution"

  Mode B — Repo/system context is provided:
    - Set "mode": "B"
    - Set "repoContextProvided": true
    - Populate "structuralIntent" using only the known context (no hallucination)
    - Populate "changePlan.steps" only when the target files/components are explicitly known
    - Set "changePlan.canExecuteDeterministically": true ONLY when ALL of the following hold:
        * repo context is present
        * uncertainty level is low
        * all dependencies are explicitly defined
        * no structural mapping is required
      Otherwise keep it false.

DETERMINISM GATE (MANDATORY):
  "changePlan.canExecuteDeterministically" MUST be false if ANY of:
    - repoContextProvided is false
    - uncertainties list is non-empty
    - changePlan.requiresStructuralMapping is true
    - affected components are unknown

NO HALLUCINATION RULE:
  If files or components are not explicitly known from the provided context:
    - Do NOT invent paths or component names
    - Set "changePlan.requiresStructuralMapping": true

REQUIRED JSON SCHEMA:
{
  "schemaVersion": "2",
  "intentId": "<uuid-v4>",
  "mode": "A" | "B",
  "repoContextProvided": true | false,
  "intent": {
    "objective": "<one-sentence summary of what the user wants>",
    "interpretedMeaning": "<deeper interpretation including implicit goals>"
  },
  "structuralIntent": {
    "operationType": "create" | "modify" | "delete" | "query" | "unknown",
    "targetLayer": "ui" | "backend" | "domain" | "system" | "unknown",
    "scope": "<brief description of the structural scope>"
  },
  "impactAnalysis": {
    "affectedComponents": ["<component or file name>"],
    "riskLevel": "low" | "medium" | "high" | "unknown",
    "requiresRepoContext": true | false,
    "uncertainties": ["<uncertainty description>"]
  },
  "changePlan": {
    "canExecuteDeterministically": true | false,
    "requiresStructuralMapping": true | false,
    "steps": [
      {
        "stepId": "<short identifier>",
        "description": "<what this step does>",
        "targetFile": "<file path or null if unknown>"
      }
    ],
    "blockedReason": "<reason execution is blocked, or null if not blocked>"
  }
}
"""

# Keywords that indicate the user wants up-to-date / current information.
_RECENCY_PATTERN = re.compile(
    r"\b(latest|current|today|now|recent|news|price|release|just|trending|"
    r"this week|this month|right now|up.?to.?date|happening)\b",
    re.IGNORECASE,
)
_SEARCH_PREFIX = "search:"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    domain_profile_id: str | None = None
    files: list[dict[str, Any]] | None = None  # File references for AI context
    # REPO_CONTEXT_FINALIZATION_V1 — Phase 9:
    # First-class Repo IDs.  When present, context is built from Repo entities
    # rather than from the context.files list (which is the V1 compat path).
    repos: list[str] | None = None


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    # CONVERSATION_LIFECYCLE_V1: every message belongs to a conversation.
    conversation_id: str = "legacy_default"
    context: ChatContext = Field(default_factory=ChatContext)
    superseded: bool = False


class ChatHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    messages: list[ChatMessageResponse]
    tools_available: list[str]


class ChatPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    context: ChatContext = Field(default_factory=ChatContext)
    agent_mode: bool = False
    # MODE_ENGINE_EXECUTION_V2: retained for request compatibility only.
    # POST /api/chat resolves active modes exclusively from agent_mode.
    modes: list[str] | None = None
    # ARTIFACT_INGESTION_PIPELINE_V1: user-provided artifacts for this request.
    # Artifacts are passed verbatim into the system prompt; no preprocessing.
    artifacts: list[ArtifactItem] | None = None
    # CONTEXT_ASSEMBLY_ALIGNMENT_V2: UI must declare execution context.
    # Defaults to "global" for backward compatibility with existing callers.
    context_scope: Literal["global", "project"] = "global"
    # Required when context_scope == "project"; ignored when context_scope == "global".
    project_id: str | None = None
    # CONVERSATION_BOUNDARY_CONTROL_V1: when True, bypass all historical messages for
    # this request.  None/False preserves legacy behavior (history is loaded normally).
    force_new_session: bool | None = None
    # CONVERSATION_LIFECYCLE_ENFORCEMENT_LOCK: conversation_id is a hard isolation
    # boundary — every request MUST belong to an explicit conversation.
    # Use POST /api/chat/conversation/new to obtain a conversation_id before sending
    # the first message.  No fallback, no implicit assignment.
    conversation_id: str

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text

    @model_validator(mode="after")
    def _validate_context_scope(self) -> "ChatPostRequest":
        # CONTEXT_ASSEMBLY_ALIGNMENT_V2: project scope requires project_id.
        if self.context_scope == "project" and not self.project_id:
            raise ValueError("project_id is required when context_scope is 'project'.")
        return self


class ChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    reply: str
    tools_available: list[str]
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse


class ChatEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("content is required and must not be empty.")
        return text


class ChatEditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    original_message: ChatMessageResponse
    new_message: ChatMessageResponse


# ---------------------------------------------------------------------------
# INTERACTION_LAYER_V2 — Schemas
# ---------------------------------------------------------------------------


class IntentV2RepoContext(BaseModel):
    """Optional repo/system context provided by the caller for Mode B."""

    model_config = ConfigDict(extra="allow")

    files: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    description: str | None = None


class IntentV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    repo_context: IntentV2RepoContext | None = None

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class _IntentField(BaseModel):
    model_config = ConfigDict(extra="allow")

    objective: str
    interpretedMeaning: str


class _StructuralIntent(BaseModel):
    model_config = ConfigDict(extra="allow")

    operationType: Literal["create", "modify", "delete", "query", "unknown"]
    targetLayer: Literal["ui", "backend", "domain", "system", "unknown"]
    scope: str


class _ImpactAnalysis(BaseModel):
    model_config = ConfigDict(extra="allow")

    affectedComponents: list[str] = Field(default_factory=list)
    riskLevel: Literal["low", "medium", "high", "unknown"]
    requiresRepoContext: bool
    uncertainties: list[str] = Field(default_factory=list)


class _ChangePlanStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    stepId: str
    description: str
    targetFile: str | None = None


class _ChangePlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    canExecuteDeterministically: bool
    requiresStructuralMapping: bool
    steps: list[_ChangePlanStep] = Field(default_factory=list)
    blockedReason: str | None = None


class IntentV2Response(BaseModel):
    """Validated INTERACTION_LAYER_V2 response. Never execution authority."""

    model_config = ConfigDict(extra="allow")

    schemaVersion: str = INTERACTION_LAYER_V2_SCHEMA_VERSION
    intentId: str
    mode: Literal["A", "B"]
    repoContextProvided: bool
    intent: _IntentField
    structuralIntent: _StructuralIntent
    impactAnalysis: _ImpactAnalysis
    changePlan: _ChangePlan


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
        conversation_id=getattr(message, "conversation_id", "legacy_default"),
        context=ChatContext(
            session_id=getattr(message, "session_id", None),
            domain_profile_id=getattr(message, "domain_profile_id", None),
        ),
        superseded=getattr(message, "superseded_by_id", None) is not None,
    )


def _new_ephemeral_message(
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
    conversation_id: str = "legacy_default",
) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=str(uuid.uuid4()),
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
        conversation_id=conversation_id,
        context=context,
    )


def _load_recent_history(db: Session | None, conversation_id: str = "legacy_default") -> list[Any]:
    """Load recent non-superseded messages for the given conversation only.

    CONVERSATION_LIFECYCLE_V1: hard isolation — no cross-conversation reads,
    no merging, no fallback to global history.
    """
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    history = db.exec(
        select(GlobalChatMessage)
        .where(GlobalChatMessage.conversation_id == conversation_id)
        .where(GlobalChatMessage.superseded_by_id.is_(None))
        .order_by(GlobalChatMessage.created_at.desc())
        .limit(_GLOBAL_CHAT_HISTORY_LIMIT)
    ).all()
    return list(reversed(history))


def _list_persisted_messages(
    db: Session | None, conversation_id: str = "legacy_default"
) -> list[Any]:
    """Return all messages for the given conversation (newest-first).

    CONVERSATION_LIFECYCLE_V1: hard isolation — no cross-conversation reads.
    """
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    return db.exec(
        select(GlobalChatMessage)
        .where(GlobalChatMessage.conversation_id == conversation_id)
        .order_by(GlobalChatMessage.created_at.desc())
    ).all()


def _persist_message(
    db: Session | None,
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
    conversation_id: str = "legacy_default",
) -> ChatMessageResponse:
    if db is None:
        return _new_ephemeral_message(role, content, context, conversation_id=conversation_id)

    from backend.app.models import GlobalChatMessage

    message = GlobalChatMessage(
        role=role,
        content=content,
        conversation_id=conversation_id,
        session_id=context.session_id,
        domain_profile_id=context.domain_profile_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return _message_to_response(message)


def _call_openai_chat(
    message: str,
    api_key: str,
    history: list[Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    """Call OpenAI Chat Completions and return the assistant reply text."""
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    url = _build_completions_url(base_url)

    effective_prompt = append_prompt_injection_defense(
        system_prompt if system_prompt is not None else _CHAT_SYSTEM_PROMPT
    )
    prompt_messages: list[dict[str, str]] = [{"role": "system", "content": effective_prompt}]
    total_history = len(history or [])
    for idx, item in enumerate(history or [], start=1):
        if item.role in ("user", "assistant", "system"):
            content = (
                format_untrusted_text(
                    f"Quoted prior user message ({idx} of {total_history})",
                    item.content,
                )
                if item.role == "user"
                else item.content
            )
            prompt_messages.append({"role": item.role, "content": content})
    prompt_messages.append(
        {"role": "user", "content": format_untrusted_text("Latest user message", message)}
    )

    payload = {
        "model": model,
        "messages": prompt_messages,
        "max_tokens": 350,
        "temperature": 0.3,
    }

    response = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _build_chat_system_prompt(db) -> str:
    """Build the global chat system prompt with a bounded ops context window."""
    if db is None:
        return _CHAT_SYSTEM_PROMPT
    try:
        from backend.app.ops_routes import build_ops_context_snippet

        snippet = build_ops_context_snippet(db)
        if not snippet:
            return _CHAT_SYSTEM_PROMPT
        n = snippet.count("\n") + 1
        ops_section = _OPS_CONTEXT_HEADER.format(n=n, snippet=snippet)
        return _CHAT_SYSTEM_PROMPT + ops_section
    except Exception:
        return _CHAT_SYSTEM_PROMPT


def _needs_web_search(message: str) -> bool:
    """Return True when the message appears to request current/live information."""
    stripped = message.strip()
    if stripped.lower().startswith(_SEARCH_PREFIX):
        return True
    return bool(_RECENCY_PATTERN.search(stripped))


def _build_search_query(message: str) -> str:
    """Strip the 'search:' prefix if present and return the query."""
    stripped = message.strip()
    if stripped.lower().startswith(_SEARCH_PREFIX):
        # Strip prefix using its length to handle case-insensitive match.
        return stripped[len(_SEARCH_PREFIX) :].strip()
    return stripped


def _format_citations(results: list[dict[str, Any]]) -> str:
    """Format web search results as a Sources section appended to the reply."""
    if not results:
        return ""
    lines = ["\n\nSources:"]
    for i, r in enumerate(results, 1):
        title = r.get("title") or r.get("url", "")
        url = r.get("url", "")
        published = r.get("published_at")
        date_str = f" ({published})" if published else ""
        lines.append(f"{i}. [{title}]({url}){date_str}")
    return "\n".join(lines)


def _build_retrieval_system_prompt(db, search_results: list[dict[str, Any]]) -> str:
    """Build a system prompt that injects retrieved web snippets."""
    base = _build_chat_system_prompt(db)
    if not search_results:
        return base
    snippets = []
    for r in search_results:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        published = r.get("published_at", "")
        date_str = f" (published: {published})" if published else ""
        snippets.append(f"- {title}{date_str}\n  URL: {url}\n  {snippet}")
    retrieval_section = (
        "\n\n--- Web search results (use to answer the user's question) ---\n"
        + "\n".join(snippets)
        + "\n--- End of web search results ---\n"
        "Cite sources by their URL when referencing retrieved facts."
    )
    return base + retrieval_section


# ---------------------------------------------------------------------------
# INTERACTION_LAYER_V2 helpers
# ---------------------------------------------------------------------------


def _build_intent_v2_mode_a_default(message: str) -> dict[str, Any]:
    """
    Return a Mode A (no repo context) IntentV2 dict without calling OpenAI.

    Used when OPENAI_API_KEY is not set.  The intent fields are derived from
    the raw message text — no structural translation is possible.
    """
    return {
        "schemaVersion": INTERACTION_LAYER_V2_SCHEMA_VERSION,
        "intentId": str(uuid.uuid4()),
        "mode": "A",
        "repoContextProvided": False,
        "intent": {
            "objective": message[:200],
            "interpretedMeaning": f"User wants to: {message[:180]}",
        },
        "structuralIntent": {
            "operationType": "unknown",
            "targetLayer": "unknown",
            "scope": "unknown — repo context required",
        },
        "impactAnalysis": {
            "affectedComponents": [],
            "riskLevel": "unknown",
            "requiresRepoContext": True,
            "uncertainties": ["No repo context provided; cannot determine impact"],
        },
        "changePlan": {
            "canExecuteDeterministically": False,
            "requiresStructuralMapping": True,
            "steps": [],
            "blockedReason": "Repo context required for deterministic execution",
        },
    }


def _call_openai_intent_v2(
    message: str,
    repo_context: IntentV2RepoContext | None,
    api_key: str,
) -> dict[str, Any]:
    """
    Call OpenAI with the INTERACTION_LAYER_V2 system prompt and return the
    parsed JSON dict.  Never raises — on any error returns a Mode A fallback.

    This function is analysis + specification only.  It NEVER executes changes.
    """
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT)))
    url = _build_completions_url(base_url)

    # Build user message — include serialised repo context when present.
    if repo_context is not None:
        context_json = repo_context.model_dump(mode="json", exclude_none=True)
        user_content = format_untrusted_json(
            "Intent request",
            {
                "message": message,
                "repo_context": context_json,
                "mode": "B",
            },
        )
    else:
        user_content = format_untrusted_json(
            "Intent request",
            {
                "message": message,
                "repo_context": None,
                "mode": "A",
            },
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": append_prompt_injection_defense(_INTERACTION_LAYER_V2_SYSTEM_PROMPT),
            },
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
    }

    try:
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
        raw_text = data["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences if the model added them despite instructions.
        # Only strip the opening and closing fence lines (```json / ```), not any
        # internal content that might happen to start with backticks.
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            # Remove the first line (opening fence) and the last line if it is a
            # closing fence; leave all other lines untouched.
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()

        parsed: dict[str, Any] = json.loads(raw_text)
        # Enforce schemaVersion — always authoritative from our constant.
        parsed["schemaVersion"] = INTERACTION_LAYER_V2_SCHEMA_VERSION
        return parsed

    except Exception as exc:
        logger.warning("INTERACTION_LAYER_V2 OpenAI call failed: %s", exc)
        fallback = _build_intent_v2_mode_a_default(message)
        fallback["_error"] = str(exc)[:200]
        return fallback


def _validate_intent_v2(raw: dict[str, Any]) -> IntentV2Response:
    """
    Validate raw parsed dict against IntentV2Response schema.

    Applies determinism gate: forces canExecuteDeterministically=false when
    the mode, context flags, or uncertainties require it.
    """
    # Determinism gate — enforce the rules regardless of what the LLM said.
    change_plan = raw.get("changePlan", {})
    impact = raw.get("impactAnalysis", {})

    repo_context_provided = raw.get("repoContextProvided", False)
    uncertainties = impact.get("uncertainties", [])
    requires_structural_mapping = change_plan.get("requiresStructuralMapping", False)

    must_block = (
        not repo_context_provided
        or bool(uncertainties)
        or requires_structural_mapping
        or not impact.get("affectedComponents")
    )

    if must_block:
        change_plan["canExecuteDeterministically"] = False
        raw["changePlan"] = change_plan

    return IntentV2Response.model_validate(raw)


# ---------------------------------------------------------------------------
# GET /api/chat
# ---------------------------------------------------------------------------


@router.get("/chat", status_code=200, dependencies=[Depends(require_auth)])
def list_chat_messages(
    conversation_id: str = Query(default="legacy_default"),
) -> JSONResponse:
    """Return persisted chat history for a conversation (newest-first).

    CONVERSATION_LIFECYCLE_V1: results are hard-isolated to the requested
    conversation_id.  Defaults to the "legacy_default" containment zone so
    that existing callers continue to see their prior messages.
    """
    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        messages = _list_persisted_messages(db, conversation_id=conversation_id)
        return _json_response(
            ChatHistoryResponse(
                messages=[_message_to_response(message) for message in messages],
                tools_available=_TOOLS_AVAILABLE,
            )
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/chat/conversation/new  — CONVERSATION_LIFECYCLE_V1
# ---------------------------------------------------------------------------


@router.post("/chat/conversation/new", status_code=200, dependencies=[Depends(require_auth)])
def create_conversation() -> JSONResponse:
    """
    Create a new conversation and return its UUID4 identifier.

    CONVERSATION_LIFECYCLE_V1: every conversation_id is a UUID4.
    No reuse, no inference, no fallback.

    Response::

        { "conversation_id": "<uuid4>" }
    """
    return JSONResponse(
        status_code=200,
        content={"conversation_id": str(uuid.uuid4())},
    )


# ---------------------------------------------------------------------------
# DELETE /api/chat/conversation/{conversation_id}  — CONVERSATION_LIFECYCLE_V1
# ---------------------------------------------------------------------------


@router.delete(
    "/chat/conversation/{conversation_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def delete_conversation(
    conversation_id: str,
    confirm: bool = Query(default=False),
) -> JSONResponse:
    """
    Delete all messages and files belonging to the given conversation_id.

    CONVERSATION_LIFECYCLE_V1:
    - Deletes ALL messages for the specified conversation.
    - Deletes ALL files uploaded to the conversation (CASCADE).
    - Deletes file objects from R2/S3 storage.
    - Does NOT affect any other conversation.

    RULE 4 — DELETE SAFETY:
    Deleting ``"legacy_default"`` requires ``?confirm=true`` to prevent silent
    deletion of the backward-compatibility containment zone.

    Response::

        { "deleted_messages": <count>, "deleted_files": <count> }
    """
    # RULE 4: legacy_default is the backward-compatibility containment zone.
    # Silent deletion is not allowed — caller must pass ?confirm=true.
    if conversation_id == "legacy_default" and not confirm:
        return _error(
            400,
            "confirmation_required",
            "Deleting 'legacy_default' requires explicit confirmation. Retry with ?confirm=true.",
        )

    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        from backend.app.models import GlobalChatMessage
        from backend.app.storage import delete_object

        # Delete all messages
        messages = db.exec(
            select(GlobalChatMessage).where(GlobalChatMessage.conversation_id == conversation_id)
        ).all()
        message_count = len(messages)
        for msg in messages:
            db.delete(msg)

        # CASCADE DELETE: Delete all files associated with this conversation
        files = db.exec(select(ChatFile).where(ChatFile.conversation_id == conversation_id)).all()
        file_count = len(files)

        # Delete from object storage first, then from database
        for file in files:
            try:
                # Delete from R2/S3
                delete_object(file.object_key)
                logger.info(f"Deleted file from storage: {file.object_key}")
            except Exception as e:
                # Log but continue - don't fail entire deletion if storage cleanup fails
                logger.warning(f"Failed to delete file from storage: {file.object_key}, error: {e}")

            # Delete from database
            db.delete(file)

        db.commit()
        # Return counts - maintain backward compatibility with "deleted" field
        return JSONResponse(
            status_code=200,
            content={
                "deleted": message_count,  # Backward compatibility
                "deleted_messages": message_count,
                "deleted_files": file_count,
            },
        )
    except Exception as e:
        logger.exception("Error deleting conversation")
        db.rollback()
        return _error(
            500,
            "delete_failed",
            f"Failed to delete conversation: {str(e)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------


@router.post("/chat", status_code=200, dependencies=[Depends(require_auth)])
async def chat(http_request: FastAPIRequest, body: dict[str, Any]) -> JSONResponse:
    """
    Send a message to the UI Blueprint assistant.

    STRICT_MODE_EXECUTION_SPINE_LOCK_V1 — PHASE 3: API STABILITY LOCK
    ALL execution paths MUST return HTTP 200
    Wrap ENTIRE handler in try/except to prevent 502 errors

    When the message contains recency keywords (latest, today, current, ...) or
    starts with "search:", a Tavily web search is performed and results are
    injected into the system prompt so the assistant can answer with up-to-date
    information.  Retrieved source URLs are appended to the reply.

    Agent mode can be enabled via:
    - Body field: ``agent_mode: true``

    When enabled, the assistant is instructed to respond using typed strict-mode
    JSON with explicit confidence/verifiability metadata.
    """
    # PHASE 3 — API STABILITY LOCK: Wrap ENTIRE execution in exception boundary
    # TRY: result = pipeline(...)  RETURN 200 + result
    # EXCEPT Exception e: RETURN 200 with structured error
    try:
        # CONTEXT_ORIGIN_ENFORCEMENT_V1: capture raw context_scope from body before
        # Pydantic validation so we can distinguish explicit vs implicit_legacy intent.
        raw_context_scope: str | None = (body or {}).get("context_scope")

        try:
            request = ChatPostRequest.model_validate(body or {})
        except ValidationError as exc:
            if any(error["loc"] == ("message",) for error in exc.errors()):
                return _error(
                    400,
                    "invalid_request",
                    "message is required and must not be empty.",
                )
            if any(error["loc"] == ("conversation_id",) for error in exc.errors()):
                return _error(
                    400,
                    "invalid_request",
                    "conversation_id is required. Call POST /api/chat/conversation/new "
                    "to obtain one before sending messages.",
                )
            return _error(
                422,
                "invalid_request",
                "Request body failed validation.",
                {"errors": exc.errors()},
            )

        message = request.message
        context = request.context
        agent_mode = request.agent_mode
        active_modes = [MODE_STRICT] if agent_mode else []
        # ARTIFACT_INGESTION_PIPELINE_V1: normalize artifact list (never None downstream).
        active_artifacts = request.artifacts or []
        # CONTEXT_ORIGIN_ENFORCEMENT_V1: classify whether context was explicitly declared
        # by the UI or implicitly defaulted by the backend.  Internal-only — never exposed
        # to prompts, AI output, or API responses.
        context_scope, context_origin = resolve_context_origin(raw_context_scope=raw_context_scope)
        logger.debug(
            "context_origin resolution: scope=%s origin=%s",
            context_scope,
            context_origin,
        )
        # CONTEXT_ASSEMBLY_ALIGNMENT_V2: resolve execution context surface.
        # Uses the origin-resolved context_scope (not raw request field) so that
        # validation operates on the resolved value, not raw input.
        # No external I/O — deterministic pass-through only.
        context_surface = resolve_context_surface(
            context_scope=context_scope,
            project_id=request.project_id,
            artifacts=active_artifacts,
        )
        resolved_artifacts = context_surface["resolved_artifacts"]

        # CONVERSATION_BOUNDARY_CONTROL_V1: stateless flag determines read isolation.
        is_stateless = request.force_new_session is True

        # CONVERSATION_LIFECYCLE_ENFORCEMENT_LOCK (RULE 4): single source of truth.
        # conversation_id is now required — no fallback, no branching.
        active_conversation_id = request.conversation_id

        db = _db_session()
        try:
            # RULE 5: always persist messages, regardless of force_new_session.
            # force_new_session=True skips history READ only (debug/testing override).
            user_message = _persist_message(
                db, "user", message, context, conversation_id=active_conversation_id
            )
            if is_stateless:
                # Stateless: skip history read — context window is empty.
                logger.debug(
                    "force_new_session=True: skipping history read; "
                    "messages are still persisted under conversation_id=%s",
                    active_conversation_id,
                )
                history = []
            else:
                history = _load_recent_history(db, conversation_id=active_conversation_id)

            # Read OPENAI_API_KEY at call time -- never returned or logged.
            openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

            # API_EXCEPTION_BOUNDARY_LOCK_V1: wrap ALL execution in exception boundary
            # to eliminate 502 errors and ensure structured error responses.
            try:
                if not openai_api_key:
                    # No OpenAI key — the stub reply still flows through mode_engine_gateway
                    # so that pre-generation constraints, all four validation stages, and
                    # mandatory audit logging run.  The stub path is NOT a bypass.
                    def _stub_ai_call(system_prompt: str) -> str:  # noqa: ARG001
                        stub_text = _stub_reply(message)
                        # In strict mode (agent_mode), return JSON-formatted output
                        # so it can pass through validation pipeline
                        if MODE_STRICT in active_modes:
                            import json

                            return json.dumps(
                                {
                                    "reply": stub_text,
                                    "claims": [],
                                    "uncertainties": [],
                                    "generation_mode": "stub",
                                    "mode_label": "STUB_NO_OPENAI_KEY",
                                }
                            )
                        return stub_text

                    reply, _audit = mode_engine_gateway(
                        user_intent=message,
                        modes=active_modes,
                        ai_call=_stub_ai_call,
                        base_system_prompt="",
                    )
                else:
                    # Optionally retrieve web results for recency-sensitive queries.
                    search_results: list[dict[str, Any]] = []
                    if _needs_web_search(message):
                        try:
                            from backend.app.web_search import TavilyKeyMissing, web_search

                            query = _build_search_query(message)
                            raw = web_search(query, recency_days=7, max_results=5)
                            search_results = raw.get("results", [])
                        except TavilyKeyMissing:
                            logger.info("web_search: TAVILY_API_KEY not set; skipping retrieval.")
                        except Exception:
                            logger.warning("web_search call failed; continuing without retrieval.")

                    # Build base system prompt with ops context + optional retrieval results.
                    if search_results:
                        base_system_prompt = _build_retrieval_system_prompt(db, search_results)
                    else:
                        base_system_prompt = _build_chat_system_prompt(db)

                    # When agent_mode is enabled, append strict typed-output requirements.
                    if agent_mode:
                        base_system_prompt += (
                            "\n\nAgent mode strict output contract:\n"
                            "- Return JSON object only.\n"
                            "- Include fields: claims, uncertainties, generation_mode, "
                            "mode_label.\n"
                            "- claims[] fields: statement, confidence (0..1), "
                            "source_type, verifiability.\n"
                            "- mode_label must be RETRIEVED, INFERRED, or GENERATED.\n"
                            "- If requirements cannot be met, return exactly: "
                            "INSUFFICIENT GROUNDED KNOWLEDGE"
                        )

                    # ARTIFACT_INGESTION_PIPELINE_V1 / CONTEXT_ASSEMBLY_ALIGNMENT_V2:
                    # inject user-provided artifacts (order: after ops/retrieval,
                    # before mode injection).
                    # Artifacts are passed verbatim — no preprocessing, no summarization.
                    artifact_block = build_artifact_context_block(resolved_artifacts)
                    if artifact_block:
                        base_system_prompt += "\n\n" + artifact_block

                    # -------------------------------------------------------
                    # REPO_CONTEXT_FINALIZATION_V1 — Phase 9:
                    # When context.repos (first-class Repo IDs) are present,
                    # build the repo context block from Repo entities directly.
                    # Phase 6: always inject REPO STATUS for AI awareness.
                    # -------------------------------------------------------
                    if context.repos and db is not None:
                        active_repo_ids: list[uuid.UUID] = []
                        for rid_str in context.repos:
                            try:
                                active_repo_ids.append(uuid.UUID(rid_str))
                            except ValueError:
                                pass

                        if active_repo_ids:
                            loaded_repos: list[Repo] = []
                            for rid in active_repo_ids:
                                repo_obj = db.get(Repo, rid)
                                if repo_obj:
                                    loaded_repos.append(repo_obj)

                            if loaded_repos:
                                # Phase 6 — REPO STATUS block
                                status_block = "\n\nREPO STATUS:\n"
                                for r in loaded_repos:
                                    status_block += (
                                        f"- repo: {r.owner}/{r.name}"
                                        f" (branch: {r.branch})"
                                        f" | status: {r.ingestion_status}"
                                        f" | files: {r.total_files}"
                                        f" | chunks: {r.total_chunks}\n"
                                    )
                                base_system_prompt += status_block

                                # Retrieve chunks scoped to these repo IDs
                                success_repo_ids = [
                                    r.id for r in loaded_repos if r.ingestion_status == "success"
                                ]
                                failed_repos = [
                                    r
                                    for r in loaded_repos
                                    if r.ingestion_status in ("failed", "pending", "running")
                                ]

                                if success_repo_ids:
                                    relevant_chunks = retrieve_relevant_chunks(
                                        user_query=message,
                                        db=db,
                                        repo_ids=success_repo_ids,
                                    )
                                    if relevant_chunks:
                                        repo_block = "\n\n---\nREPO CONTEXT:\n"
                                        for chunk in relevant_chunks:
                                            repo_block += (
                                                f"\nFILE: {chunk.file_path}\n\n{chunk.content}\n"
                                            )
                                        repo_block += "---\n"
                                        base_system_prompt += repo_block
                                    else:
                                        base_system_prompt += (
                                            "\n\n[NO_REPO_CONTENT_AVAILABLE]: "
                                            "Repository files were referenced but no content "
                                            "could be retrieved from storage.\n"
                                        )

                                for r in failed_repos:
                                    base_system_prompt += (
                                        f"\n[REPO_PRESENT_BUT_EMPTY]: "
                                        f"Repository '{r.owner}/{r.name}' was added but "
                                        f"ingestion status is '{r.ingestion_status}' — "
                                        f"no usable content available. "
                                        f"Respond: 'Repository ingestion incomplete'.\n"
                                    )

                    # -------------------------------------------------------
                    # FILE_CONTEXT_INJECTION_V1 (V1 compat path):
                    # inject uploaded file references.
                    # PHASE 8 — CONTEXT_FALLBACK: when frontend omits context.files,
                    # auto-load all included github_repo files for the conversation.
                    # -------------------------------------------------------
                    effective_file_refs: list[dict] = []
                    if context.files:
                        for file_ref in context.files:
                            effective_file_refs.append(file_ref)
                    elif not context.repos and db is not None:
                        # Backend fallback (V1 path): query DB for included repo files
                        stmt = (
                            select(ChatFile)
                            .where(ChatFile.conversation_id == active_conversation_id)
                            .where(ChatFile.category == "github_repo")
                            .where(ChatFile.included_in_context.is_(True))  # type: ignore[attr-defined]
                        )
                        fallback_files = db.exec(stmt).all()
                        for fb in fallback_files:
                            effective_file_refs.append(
                                {
                                    "id": str(fb.id),
                                    "filename": fb.filename,
                                    "category": fb.category,
                                    "mime_type": fb.mime_type,
                                }
                            )

                    if effective_file_refs:
                        file_block = "\n\n--- Uploaded Files Available for Reference ---\n"
                        repo_chat_file_ids: list[uuid.UUID] = []

                        for file_ref in effective_file_refs:
                            file_id_str = file_ref.get("id", "")
                            filename = file_ref.get("filename", "unnamed")
                            category = file_ref.get("category", "other")
                            file_block += f"\n- {filename} (Category: {category})"

                            if category == "github_repo":
                                # Phase 5: collect explicit file IDs for isolated retrieval
                                try:
                                    repo_chat_file_ids.append(uuid.UUID(file_id_str))
                                except ValueError:
                                    pass
                                continue

                            # Fetch file content from database if text was extracted
                            try:
                                file_uuid = uuid.UUID(file_id_str)
                                stmt = select(ChatFile).where(ChatFile.id == file_uuid)
                                chat_file = db.exec(stmt).first()
                                if chat_file and chat_file.extracted_text:
                                    # Truncate to reasonable size for context
                                    content = chat_file.extracted_text[:5000]
                                    if len(chat_file.extracted_text) > 5000:
                                        content += "\n...[truncated]"
                                    file_block += f"\n  Content preview:\n{content}\n"
                            except (ValueError, AttributeError):
                                pass  # Invalid UUID or missing data, skip content

                        # REPO_CONTEXT_INTELLIGENCE_LAYER_V2 — Phase 5 + 6:
                        # Retrieve chunks scoped to the explicit context.files IDs only.
                        # Inject with standardised REPO CONTEXT block format.
                        # PHASE 9 + 10: surface failure marker when ids present but no chunks.
                        if repo_chat_file_ids:
                            # PHASE 9: check for repos with failed/empty ingestion status
                            failed_repo_names: list[str] = []
                            if db is not None:
                                for rid in repo_chat_file_ids:
                                    stmt = select(ChatFile).where(ChatFile.id == rid)
                                    repo_file = db.exec(stmt).first()
                                    if repo_file:
                                        has_bad_status = repo_file.ingestion_status in (
                                            "failed",
                                            None,
                                        )
                                        if has_bad_status:
                                            failed_repo_names.append(repo_file.filename)

                            relevant_chunks = retrieve_relevant_chunks(
                                user_query=message,
                                db=db,
                                chat_file_ids=repo_chat_file_ids,
                            )
                            if relevant_chunks:
                                file_block += "\n\n---\nREPO CONTEXT:\n"
                                for chunk in relevant_chunks:
                                    file_block += f"\nFILE: {chunk.file_path}\n\n{chunk.content}\n"
                                file_block += "---\n"
                            elif failed_repo_names:
                                # PHASE 9: per-repo failure markers explain the empty result;
                                # skip the generic NO_REPO_CONTENT_AVAILABLE to avoid duplication.
                                for name in failed_repo_names:
                                    file_block += (
                                        f"\n[REPO_PRESENT_BUT_EMPTY]: "
                                        f"Repository '{name}' was added but ingestion "
                                        f"produced no usable content.\n"
                                    )
                            else:
                                # PHASE 10: ids present, no known failure, but no chunks —
                                # emit the generic retrieval-failure marker.
                                file_block += (
                                    "\n\n[NO_REPO_CONTENT_AVAILABLE]: "
                                    "Repository files were referenced but no content "
                                    "could be retrieved from storage.\n"
                                )

                        file_block += "\n--- End of Uploaded Files ---\n"
                        base_system_prompt += file_block

                    # Build the ai_call closure; mode constraints are injected by the gateway.
                    # This is the ONLY path through which _call_openai_chat is reached for
                    # POST /api/chat — all AI calls are exclusive to mode_engine_gateway.
                    def _openai_ai_call(system_prompt: str) -> str:
                        return _call_openai_chat(
                            message,
                            openai_api_key,
                            history[:-1] if history else [],
                            system_prompt=system_prompt,
                        )

                    reply, _audit = mode_engine_gateway(
                        user_intent=message,
                        modes=active_modes,
                        ai_call=_openai_ai_call,
                        base_system_prompt=base_system_prompt,
                    )

                    # Append citations to the reply if retrieval was performed.
                    if search_results:
                        reply += _format_citations(search_results)

                assistant_message = _persist_message(
                    db, "assistant", reply, context, conversation_id=active_conversation_id
                )
                return _json_response(
                    ChatPostResponse(
                        reply=reply,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
            except (
                httpx.TimeoutException,
                httpx.RequestError,
                httpx.HTTPStatusError,
                httpx.ConnectError,
            ):
                # API_EXCEPTION_BOUNDARY_LOCK_V1: Catch httpx exceptions to prevent 502 errors.
                # Return status 200 with structured error in reply field
                # (matching mode_engine pattern).
                import json as _json

                logger.exception("HTTP exception in chat endpoint")
                error_reply = _json.dumps(
                    {
                        "error": "SYSTEM_FAILURE",
                        "message": "AI provider connection failed",
                        "type": "HTTPError",
                    }
                )
                assistant_message = _persist_message(
                    db, "assistant", error_reply, context, conversation_id=active_conversation_id
                )
                return _json_response(
                    ChatPostResponse(
                        reply=error_reply,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
            except (KeyError, IndexError, ValueError) as e:
                # API_EXCEPTION_BOUNDARY_LOCK_V1: Catch parsing errors to prevent 502 errors.
                # Return status 200 with structured error in reply field.
                import json as _json

                logger.exception("Parsing exception in chat endpoint: %s", e)
                error_reply = _json.dumps(
                    {
                        "error": "SYSTEM_FAILURE",
                        "message": "Invalid AI response format",
                        "type": type(e).__name__,
                    }
                )
                assistant_message = _persist_message(
                    db, "assistant", error_reply, context, conversation_id=active_conversation_id
                )
                return _json_response(
                    ChatPostResponse(
                        reply=error_reply,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
        finally:
            if db is not None:
                db.close()
    # PHASE 3 — API STABILITY LOCK: Final catch-all exception handler
    # This should never be reached due to nested try/except, but provides ultimate safety
    except Exception as e:
        # PROHIBITIONS: NO uncaught exceptions, NO 5xx responses
        import json as _json

        logger.exception("Unexpected exception in chat endpoint: %s", e)
        error_reply = _json.dumps(
            {
                "error": "SYSTEM_FAILURE",
                "detail": str(e),
                "retry_count": 0,
            }
        )
        # Try to persist error message if db is available
        try:
            db = _db_session()
            active_conversation_id = (body or {}).get("conversation_id", "unknown")
            context = ChatContext()
            assistant_message = _persist_message(
                db, "assistant", error_reply, context, conversation_id=active_conversation_id
            )
            user_message = _persist_message(
                db,
                "user",
                (body or {}).get("message", ""),
                context,
                conversation_id=active_conversation_id,
            )
            if db is not None:
                db.close()
            return _json_response(
                ChatPostResponse(
                    reply=error_reply,
                    tools_available=_TOOLS_AVAILABLE,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
            )
        except Exception:
            # If even persistence fails, return minimal error response
            # Don't expose internal exception details to clients
            return JSONResponse(
                status_code=200,
                content={
                    "error": "SYSTEM_FAILURE",
                    "detail": "An unexpected error occurred. Please try again later.",
                    "retry_count": 0,
                },
            )


# ---------------------------------------------------------------------------
# POST /api/chat/{message_id}/edit
# ---------------------------------------------------------------------------


@router.post(
    "/chat/{message_id}/edit",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def edit_chat_message(message_id: str, body: dict[str, Any]) -> JSONResponse:
    """
    Edit a user message by creating a new message and marking the original as
    superseded.

    The original message is preserved (not deleted) but its
    ``superseded_by_id`` field is set to the new message's id, so clients can
    hide it from the active conversation while retaining the audit trail.

    Only ``role="user"`` messages may be edited.

    Request body::

        { "content": "corrected message text" }

    Response (HTTP 201)::

        {
            "schema_version": "...",
            "original_message": { ... superseded=true ... },
            "new_message":      { ... superseded=false ... }
        }
    """
    try:
        request = ChatEditRequest.model_validate(body or {})
    except ValidationError:
        return _error(422, "invalid_request", "Request body failed validation.")

    # Validate message_id is a UUID.
    try:
        msg_uuid = uuid.UUID(message_id)
    except ValueError:
        return _error(400, "invalid_request", "message_id must be a valid UUID.")

    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        from backend.app.models import GlobalChatMessage

        original = db.get(GlobalChatMessage, msg_uuid)
        if original is None:
            return _error(404, "not_found", "Message not found.")
        if original.role != "user":
            return _error(400, "invalid_request", "Only user messages may be edited.")

        # Create the new (replacement) message, inheriting the original's
        # conversation_id so it stays within the same conversation boundary.
        new_msg = GlobalChatMessage(
            role="user",
            content=request.content,
            conversation_id=original.conversation_id,
            session_id=original.session_id,
            domain_profile_id=original.domain_profile_id,
        )
        db.add(new_msg)
        db.flush()  # assign new_msg.id

        # Mark the original as superseded.
        original.superseded_by_id = new_msg.id
        db.add(original)
        db.commit()
        db.refresh(original)
        db.refresh(new_msg)

        return JSONResponse(
            status_code=201,
            content=ChatEditResponse(
                original_message=_message_to_response(original),
                new_message=_message_to_response(new_msg),
            ).model_dump(mode="json"),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/chat/intent  — INTERACTION_LAYER_V2
# ---------------------------------------------------------------------------


@router.post("/chat/intent", status_code=200, dependencies=[Depends(require_auth)])
async def parse_intent_v2(body: dict[str, Any]) -> JSONResponse:
    """
    INTERACTION_LAYER_V2 — Convert raw human input into a deterministic
    structured intent specification (JSON only, never executes).

    Operating modes
    ---------------
    Mode A (no ``repo_context``):
        Returns intent with ``canExecuteDeterministically=false`` and marks
        repo context as required.  Structural translation is skipped.

    Mode B (``repo_context`` provided):
        Attempts structural translation and change planning using only the
        explicitly supplied context.  Files/components are never hallucinated.

    The response is SPECIFICATION ONLY.  The system does not execute, mutate,
    or treat this output as execution authority.

    Request body::

        {
          "message": "Add a dark-mode toggle to the settings screen",
          "repo_context": {           // optional — omit for Mode A
            "files": ["src/Settings.tsx", "src/theme.ts"],
            "components": ["SettingsScreen", "ThemeProvider"],
            "description": "React Native app with styled-components"
          }
        }

    Response (HTTP 200)::

        {
          "schemaVersion": "2",
          "intentId": "<uuid-v4>",
          "mode": "A" | "B",
          "repoContextProvided": true | false,
          "intent": { "objective": "...", "interpretedMeaning": "..." },
          "structuralIntent": { "operationType": "...", "targetLayer": "...", "scope": "..." },
          "impactAnalysis": { "affectedComponents": [], "riskLevel": "...",
                              "requiresRepoContext": true|false, "uncertainties": [] },
          "changePlan": { "canExecuteDeterministically": false, "requiresStructuralMapping": true,
                          "steps": [], "blockedReason": "..." }
        }
    """
    try:
        request = IntentV2Request.model_validate(body or {})
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
    repo_context = request.repo_context

    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not openai_api_key:
        # Mode A — no OpenAI key; return deterministic defaults.
        raw = _build_intent_v2_mode_a_default(message)
    else:
        raw = _call_openai_intent_v2(message, repo_context, openai_api_key)

    try:
        validated = _validate_intent_v2(raw)
        return JSONResponse(status_code=200, content=validated.model_dump(mode="json"))
    except Exception as exc:
        logger.warning("IntentV2 validation failed: %s — returning raw fallback", exc)
        # Fallback: return the raw dict with a validation_error note.
        raw["_validation_error"] = str(exc)[:200]
        return JSONResponse(status_code=200, content=raw)
