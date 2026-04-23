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
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
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
from backend.app.file_resolution import resolve_files_from_chunks
from backend.app.mode_engine import (
    MODE_STRICT,
    apply_mode_conflict_resolution,  # noqa: F401 — exported for test introspection
    mode_engine_gateway,
)
from backend.app.models import (
    ChatFile,
    Conversation,
    ConversationContext,
    ConversationRepo,
    IngestJob,
    Repo,
    RepoChunk,
    RepoIndexRegistry,
)
from backend.app.query_classifier import QueryType, route_query
from backend.app.query_router import (
    RuntimeViolationError,
    build_execution_trace,
    execute_query,
    verify_execution_trace,
)
from backend.app.repo_retrieval import retrieve_relevant_chunks
from backend.app.structural_handler import handle_structural_query
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
_MIN_RETRIEVAL_CHUNKS = 50
_STRUCTURAL_FILE_PREVIEW_SIZE = 20
_STRUCTURAL_FILE_PAGINATION_THRESHOLD = 100

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
    # Backward-compatible alias accepted from some clients.
    repo_ids: list[str] | None = None

    @model_validator(mode="after")
    def _normalize_repo_fields(self) -> "ChatContext":
        merged: list[str] = []
        for source in (self.repos or [], self.repo_ids or []):
            for repo_id in source:
                text = str(repo_id or "").strip()
                if text and text not in merged:
                    merged.append(text)
        self.repos = merged or None
        return self


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
    # Backward-compatible top-level alias for repo context.
    repo_ids: list[str] | None = None

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
        if self.repo_ids:
            merged = list(self.context.repos or [])
            for repo_id in self.repo_ids:
                text = str(repo_id or "").strip()
                if text and text not in merged:
                    merged.append(text)
            self.context.repos = merged or None
        return self


class ChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    conversation_id: str
    reply: str
    error_code: str | None = None
    retrieved_count: int = 0
    type: str | None = None
    file_count: int | None = None
    files: list[str] | None = None
    preview: list[str] | None = None
    has_more: bool | None = None
    structural: dict[str, Any] | None = None
    semantic: dict[str, Any] | None = None
    structural_source: str | None = None
    semantic_source: str | None = None
    source: str | None = None
    repo_count: int = 0
    total_chunks: int = 0
    retrieved_chunks: int = 0
    tools_available: list[str]
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    execution_trace: dict[str, Any] = Field(
        default_factory=lambda: {
            "classification": "UNKNOWN",
            "execution_path": [],
            "structural_called": False,
            "retrieval_called": False,
            "llm_called": False,
        }
    )
    details: dict[str, Any] | None = None


class CompletenessStatus(str, Enum):
    NO_CONTEXT = "NO_CONTEXT"
    PARTIAL_CONTEXT = "PARTIAL_CONTEXT"
    SUFFICIENT_CONTEXT = "SUFFICIENT_CONTEXT"
    FULL_CONTEXT = "FULL_CONTEXT"


class ContextIntegrityState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    repo_id: str | None
    total_files: int | None
    retrieved_files: int
    retrieved_chunks: int
    completeness_ratio: float
    completeness_status: CompletenessStatus


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


def _reply_references_repo_data(reply: str, repo_chunks: list[Any]) -> bool:
    """Best-effort grounding check: reply should reference retrieved repo data."""
    lower_reply = reply.lower()
    for chunk in repo_chunks:
        repo_id = str(getattr(chunk, "repo_id", "") or "").lower()
        file_path = str(getattr(chunk, "file_path", "") or "").lower()
        if repo_id and repo_id in lower_reply:
            return True
        if repo_id and f"repo_id: {repo_id}" in lower_reply:
            return True
        if file_path and file_path in lower_reply:
            return True
        if file_path and f"file: {file_path}" in lower_reply:
            return True
        file_name = file_path.split("/")[-1] if file_path else ""
        if file_name and file_name in lower_reply:
            return True
    return False


def _run_structural_query(
    *,
    db: Session,
    repo_ids: list[uuid.UUID],
    query_text: str,
) -> dict[str, Any]:
    return handle_structural_query(db=db, repo_ids=repo_ids, query_text=query_text)


def _run_retrieval_query(
    *,
    user_query: str,
    db: Session,
    repo_ids: list[uuid.UUID] | None = None,
    conversation_id: str | None = None,
) -> list[RepoChunk]:
    return retrieve_relevant_chunks(
        user_query=user_query,
        db=db,
        repo_ids=repo_ids,
        conversation_id=conversation_id,
    )


_GLOBAL_QUERY_RE = re.compile(
    r"\b(how many files|list all files|summarize repo|analyze entire repository|"
    r"find all .+ across repo)\b",
    re.IGNORECASE,
)
_ABSOLUTE_CLAIM_RE = re.compile(
    r"\b(all files|entire repo|whole repository|across the repo|every file)\b",
    re.IGNORECASE,
)


def _requires_full_context(message: str) -> bool:
    return bool(_GLOBAL_QUERY_RE.search(message or ""))


def _normalize_retrieval_result(result: Any) -> list[RepoChunk]:
    if not isinstance(result, list):
        raise RuntimeError("RETRIEVAL_INTEGRITY_FAILURE")

    chunks: list[RepoChunk] = []
    for chunk in result:
        if not isinstance(chunk, RepoChunk):
            raise RuntimeError("INVALID_CHUNK_SHAPE")
        if getattr(chunk, "file_id", None) is None:
            raise RuntimeError("INVALID_CHUNK_SHAPE")
        if not str(getattr(chunk, "file_path", "") or "").strip():
            raise RuntimeError("RETRIEVAL_INTEGRITY_FAILURE")
        chunks.append(chunk)
    return chunks


def _enforce_chunk_shape(chunks: list[Any]) -> None:
    for chunk in chunks:
        if not isinstance(chunk, RepoChunk):
            raise RuntimeError("INVALID_CHUNK_SHAPE")
        if getattr(chunk, "file_id", None) is None:
            raise RuntimeError("INVALID_CHUNK_SHAPE")


def _build_context_integrity_state(
    *,
    conversation_id: str,
    repo_id: str | None,
    total_files: int | None,
    retrieved_files: int,
    retrieved_chunks: int,
) -> ContextIntegrityState:
    if repo_id is None:
        status = CompletenessStatus.NO_CONTEXT
        ratio = 0.0
    elif total_files is None:
        status = CompletenessStatus.PARTIAL_CONTEXT
        ratio = 0.0
    elif total_files <= 0:
        status = CompletenessStatus.PARTIAL_CONTEXT
        ratio = 0.0
    else:
        ratio = retrieved_files / total_files
        # Completeness thresholds:
        # - < 0.60: too narrow to trust repository-wide claims
        # - 0.60..0.94: usable for local/scope-limited answers
        # - >= 0.95: treated as effectively full repository coverage
        if ratio < 0.6:
            status = CompletenessStatus.PARTIAL_CONTEXT
        elif ratio < 0.95:
            status = CompletenessStatus.SUFFICIENT_CONTEXT
        else:
            status = CompletenessStatus.FULL_CONTEXT
    return ContextIntegrityState(
        conversation_id=conversation_id,
        repo_id=repo_id,
        total_files=total_files,
        retrieved_files=retrieved_files,
        retrieved_chunks=retrieved_chunks,
        completeness_ratio=ratio,
        completeness_status=status,
    )


def _retrieve_conversation_context(
    db: Session,
    conversation_id: str,
    limit: int = 50,
) -> list[RepoChunk]:
    """
    MQP-CONTRACT: CHAT_CONTEXT_RETRIEVAL_V1 — Step 2

    Retrieve repository context chunks linked to a conversation.
    Since RepoChunk doesn't have a direct conversation_id field,
    we retrieve through IngestJob entities.
    """
    # Get all successful ingest jobs for this conversation
    job_ids = [
        row.id
        for row in db.exec(
            select(IngestJob).where(
                IngestJob.conversation_id == conversation_id,
                IngestJob.status == "success",
            )
        ).all()
    ]

    if not job_ids:
        return []

    # Retrieve chunks from these jobs
    context_chunks = db.exec(
        select(RepoChunk)
        .where(RepoChunk.ingest_job_id.in_(job_ids))  # type: ignore[attr-defined]
        .limit(limit)
    ).all()

    return list(context_chunks)


def _run_chat_llm(
    *,
    message: str,
    api_key: str,
    history: list[Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    return _call_openai_chat(message, api_key, history, system_prompt)


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


def _ensure_conversation(db: Session | None, conversation_id: str) -> Conversation | None:
    if db is None:
        return None
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        conv = Conversation(id=conversation_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    _ensure_conversation_context(db, conversation_id)
    return conv


def _conversation_repo_ids(db: Session | None, conversation_id: str) -> list[uuid.UUID]:
    if db is None:
        return []
    ctx = db.exec(
        select(ConversationContext).where(ConversationContext.conversation_id == conversation_id)
    ).first()
    if ctx is None:
        return []
    if ctx.repo_id is None:
        return []
    return [ctx.repo_id]


def _ensure_conversation_context(
    db: Session | None,
    conversation_id: str,
    *,
    repo_id: uuid.UUID | None = None,
    update_repo: bool = False,
) -> ConversationContext | None:
    if db is None:
        return None
    ctx = db.exec(
        select(ConversationContext).where(ConversationContext.conversation_id == conversation_id)
    ).first()
    if ctx is None:
        ctx = ConversationContext(
            conversation_id=conversation_id,
            repo_id=repo_id if update_repo else None,
        )
        db.add(ctx)
        db.commit()
        db.refresh(ctx)
        return ctx
    if update_repo and ctx.repo_id != repo_id:
        ctx.repo_id = repo_id
        db.add(ctx)
        db.commit()
        db.refresh(ctx)
    return ctx


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
    conversation_id = str(uuid.uuid4())
    db = _db_session()
    if db is not None:
        try:
            db.add(Conversation(id=conversation_id))
            db.add(ConversationContext(conversation_id=conversation_id, repo_id=None))
            db.commit()
        finally:
            db.close()
    return JSONResponse(status_code=200, content={"conversation_id": conversation_id})


@router.get(
    "/chat/conversation/{conversation_id}/context",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def get_conversation_context_binding(conversation_id: str) -> JSONResponse:
    """
    Return the active context binding for a conversation.
    """
    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; context binding is unavailable.",
        )
    try:
        ctx = db.exec(
            select(ConversationContext).where(
                ConversationContext.conversation_id == conversation_id
            )
        ).first()
        if ctx is None:
            return _error(404, "context_binding_not_found", "No active context binding found.")
        return JSONResponse(
            status_code=200,
            content={
                "conversation_id": conversation_id,
                "repo_id": str(ctx.repo_id) if ctx.repo_id is not None else None,
                "created_at": ctx.created_at.isoformat() if ctx.created_at else None,
                "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
            },
        )
    finally:
        db.close()


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

        # Remove repo bindings for the conversation.
        bindings = db.exec(
            select(ConversationRepo).where(ConversationRepo.conversation_id == conversation_id)
        ).all()
        for b in bindings:
            db.delete(b)
        ctx = db.exec(
            select(ConversationContext).where(
                ConversationContext.conversation_id == conversation_id
            )
        ).first()
        if ctx is not None:
            db.delete(ctx)

        # Remove conversation record if present.
        conv = db.get(Conversation, conversation_id)
        if conv is not None:
            db.delete(conv)

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
        retrieved_count = 0
        repo_count = 0
        total_files = 0
        total_chunks = 0
        retrieved_chunks = 0
        retrieved_files = 0
        response_error_code: str | None = None
        repo_chunks_for_grounding: list[Any] = []
        ctx_chunks: list[Any] = []
        has_explicit_repo_context = False
        context_integrity_state: ContextIntegrityState | None = None
        try:
            _ensure_conversation(db, active_conversation_id)
            if db is not None and context.repos:
                if len(context.repos) != 1:
                    return _error(
                        400,
                        "invalid_request",
                        "Exactly one repo id is allowed in context.repos for active binding.",
                    )
                rid_str = context.repos[0]
                try:
                    selected_repo_uuid = uuid.UUID(rid_str)
                except ValueError:
                    return _error(
                        400,
                        "invalid_request",
                        "Invalid UUID format for repo id in context.repos.",
                    )
                if db.get(Repo, selected_repo_uuid) is None:
                    return _error(
                        404,
                        "repo_not_found",
                        "Requested repo binding target not found.",
                    )
                existing = db.exec(
                    select(ConversationRepo).where(
                        ConversationRepo.conversation_id == active_conversation_id,
                        ConversationRepo.repo_id == selected_repo_uuid,
                    )
                ).first()
                if existing is None:
                    db.add(
                        ConversationRepo(
                            conversation_id=active_conversation_id,
                            repo_id=selected_repo_uuid,
                        )
                    )
                db.commit()
                _ensure_conversation_context(
                    db,
                    active_conversation_id,
                    repo_id=selected_repo_uuid,
                    update_repo=True,
                )
            elif db is not None:
                _ensure_conversation_context(db, active_conversation_id)
            conversation_repo_ids = _conversation_repo_ids(db, active_conversation_id)
            has_explicit_repo_context = bool(conversation_repo_ids)
            repo_count = len(conversation_repo_ids)
            if conversation_repo_ids and db is not None:
                for rid in conversation_repo_ids:
                    repo_obj = db.get(Repo, rid)
                    if repo_obj is None:
                        continue
                    total_files += int(repo_obj.total_files or 0)
                    total_chunks += int(repo_obj.total_chunks or 0)
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

            hybrid_structural_result: dict[str, Any] | None = None
            query_type = route_query(message)
            requires_full_context = (
                _requires_full_context(message) and query_type != QueryType.HYBRID
            )
            if requires_full_context and not conversation_repo_ids:
                reply = "No repository bound to this conversation"
                assistant_message = _persist_message(
                    db,
                    "assistant",
                    reply,
                    context,
                    conversation_id=active_conversation_id,
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=reply,
                        error_code="NO_CONTEXT",
                        retrieved_count=0,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=0,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
            if query_type == QueryType.STRUCTURAL and (not conversation_repo_ids or db is None):
                router_result, router_runtime = execute_query(
                    classification=QueryType.STRUCTURAL,
                    query=message,
                    structural_handler=lambda _: {
                        "type": "structural",
                        "file_count": 0,
                        "files": [],
                        "source": "index_registry",
                    },
                    retrieval_handler=lambda _: {"retrieved_chunks": 0},
                    llm_handler=None,
                )
                trace = build_execution_trace(
                    router_runtime,
                    path_prefix=["chat", "route_query"],
                )
                verify_execution_trace(trace)
                reply = "INSUFFICIENT_CONTEXT"
                assistant_message = _persist_message(
                    db,
                    "assistant",
                    reply,
                    context,
                    conversation_id=active_conversation_id,
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=reply,
                        error_code=(
                            "INSUFFICIENT_CONTEXT"
                            if router_result.get("error_code") is None
                            else str(router_result.get("error_code"))
                        ),
                        retrieved_count=0,
                        type="structural",
                        file_count=0,
                        files=[],
                        has_more=False,
                        source="index_registry",
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=0,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                        execution_trace={
                            "classification": trace.classification,
                            "execution_path": trace.execution_path,
                            "structural_called": trace.structural_called,
                            "retrieval_called": trace.retrieval_called,
                            "llm_called": trace.llm_called,
                        },
                    )
                )
            if conversation_repo_ids and db is not None:
                if query_type in (QueryType.STRUCTURAL, QueryType.HYBRID):
                    if query_type == QueryType.STRUCTURAL and not requires_full_context:
                        def _router_structural(q: str) -> dict[str, Any]:
                            structural_payload = _run_structural_query(
                                db=db,
                                repo_ids=conversation_repo_ids,
                                query_text=q,
                            )
                            return {
                                "type": "structural",
                                "file_count": int(structural_payload["data"]["count"]),
                                "files": list(structural_payload["data"]["files"]),
                                "source": "index_registry",
                            }

                        router_result, router_runtime = execute_query(
                            classification=QueryType.STRUCTURAL,
                            query=message,
                            structural_handler=_router_structural,
                            retrieval_handler=lambda _: {"retrieved_chunks": 0},
                            llm_handler=None,
                        )
                        trace = build_execution_trace(
                            router_runtime,
                            path_prefix=["chat", "route_query"],
                        )
                        verify_execution_trace(trace)
                        if router_result.get("error_code") is not None:
                            reply = "INSUFFICIENT_CONTEXT"
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code="INSUFFICIENT_CONTEXT",
                                    retrieved_count=0,
                                    type="structural",
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=0,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                    execution_trace={
                                        "classification": trace.classification,
                                        "execution_path": trace.execution_path,
                                        "structural_called": trace.structural_called,
                                        "retrieval_called": trace.retrieval_called,
                                        "llm_called": trace.llm_called,
                                    },
                                )
                            )

                        all_files = list(router_result.get("files") or [])
                        lower_message = message.lower()
                        force_full_list = "list all files" in lower_message
                        preview_files = all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                        has_more = (
                            len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                            and not force_full_list
                        )
                        reply = "STRUCTURAL_QUERY_RESULT"
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code=None,
                                retrieved_count=0,
                                type="structural",
                                file_count=int(router_result.get("file_count") or 0),
                                files=None if has_more else all_files,
                                preview=preview_files if has_more else None,
                                has_more=has_more,
                                source="index_registry",
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=0,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                                execution_trace={
                                    "classification": trace.classification,
                                    "execution_path": trace.execution_path,
                                    "structural_called": trace.structural_called,
                                    "retrieval_called": trace.retrieval_called,
                                    "llm_called": trace.llm_called,
                                },
                            )
                        )
                    structural_result = _run_structural_query(
                        db=db,
                        repo_ids=conversation_repo_ids,
                        query_text=message,
                    )
                    if (
                        structural_result.get("error_code") is not None
                        and not requires_full_context
                    ):
                        reply = "INSUFFICIENT_CONTEXT"
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code="INSUFFICIENT_CONTEXT",
                                retrieved_count=0,
                                type="structural",
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=0,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                            )
                        )

                    registries = db.exec(
                        select(RepoIndexRegistry).where(
                            RepoIndexRegistry.repo_id.in_(conversation_repo_ids)  # type: ignore[attr-defined]
                        )
                    ).all()
                    index_file_count = sum(int(row.total_files or 0) for row in registries)
                    structural_data = (
                        structural_result.get("data")
                        if isinstance(structural_result, dict)
                        else None
                    )
                    structural_files = (
                        list(structural_data.get("files") or []) if structural_data else []
                    )
                    structural_file_count = int(
                        (structural_data.get("count") if structural_data else None)
                        or total_files
                        or 0
                    )
                    if structural_file_count != index_file_count and not requires_full_context:
                        reply = "INSUFFICIENT_CONTEXT"
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code="INSUFFICIENT_CONTEXT",
                                retrieved_count=0,
                                type="structural",
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=0,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                            )
                        )

                    if query_type == QueryType.STRUCTURAL and not requires_full_context:
                        all_files = structural_files
                        lower_message = message.lower()
                        force_full_list = "list all files" in lower_message
                        preview_files = all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                        has_more = (
                            len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                            and not force_full_list
                        )
                        reply = "STRUCTURAL_QUERY_RESULT"
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code=None,
                                retrieved_count=0,
                                type="structural",
                                file_count=structural_file_count,
                                files=None if has_more else all_files,
                                preview=preview_files if has_more else None,
                                has_more=has_more,
                                source=str(structural_result.get("source") or "index_registry"),
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=0,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                            )
                        )

                    if query_type == QueryType.STRUCTURAL and requires_full_context:
                        context_integrity_state = _build_context_integrity_state(
                            conversation_id=active_conversation_id,
                            repo_id=(
                                str(conversation_repo_ids[0]) if conversation_repo_ids else None
                            ),
                            total_files=total_files if total_files > 0 else None,
                            retrieved_files=structural_file_count,
                            retrieved_chunks=0,
                        )
                        if (
                            context_integrity_state.completeness_status
                            != CompletenessStatus.FULL_CONTEXT
                        ):
                            reply = "DATA_INCOMPLETE"
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code="DATA_INCOMPLETE",
                                    retrieved_count=0,
                                    type="structural",
                                    file_count=structural_file_count,
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=0,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                    details={
                                        "total_files": context_integrity_state.total_files,
                                        "retrieved_files": context_integrity_state.retrieved_files,
                                        "completeness_ratio": (
                                            context_integrity_state.completeness_ratio
                                        ),
                                    },
                                    execution_trace={
                                        "classification": QueryType.STRUCTURAL.value,
                                        "execution_path": [
                                            "chat",
                                            "route_query",
                                            "execute_query",
                                            "structural_handler",
                                        ],
                                        "structural_called": True,
                                        "retrieval_called": False,
                                        "llm_called": False,
                                    },
                                )
                            )
                        all_files = structural_files
                        lower_message = message.lower()
                        force_full_list = "list all files" in lower_message
                        preview_files = all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                        has_more = (
                            len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                            and not force_full_list
                        )
                        reply = "STRUCTURAL_QUERY_RESULT"
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code=None,
                                retrieved_count=0,
                                type="structural",
                                file_count=structural_file_count,
                                files=None if has_more else all_files,
                                preview=preview_files if has_more else None,
                                has_more=has_more,
                                source=str(structural_result.get("source") or "index_registry"),
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=0,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                                execution_trace={
                                    "classification": QueryType.STRUCTURAL.value,
                                    "execution_path": [
                                        "chat",
                                        "route_query",
                                        "execute_query",
                                        "structural_handler",
                                    ],
                                    "structural_called": True,
                                    "retrieval_called": False,
                                    "llm_called": False,
                                },
                            )
                        )

                    if structural_data is None:
                        hybrid_structural_result = {
                            "data": {"count": structural_file_count, "files": structural_files},
                            "source": str(structural_result.get("source") or "index_registry"),
                        }
                    else:
                        hybrid_structural_result = structural_result

            enforce_retrieval_threshold = False
            if conversation_repo_ids and db is not None and query_type != QueryType.STRUCTURAL:
                registries_for_threshold = db.exec(
                    select(RepoIndexRegistry).where(
                        RepoIndexRegistry.repo_id.in_(conversation_repo_ids)  # type: ignore[attr-defined]
                    )
                ).all()
                if registries_for_threshold and all(
                    r.status in {"indexed", "completed"} for r in registries_for_threshold
                ):
                    enforce_retrieval_threshold = (
                        sum(int(r.total_chunks or 0) for r in registries_for_threshold)
                        < _MIN_RETRIEVAL_CHUNKS
                    )

            if enforce_retrieval_threshold:
                structural_payload = None
                if hybrid_structural_result is not None:
                    all_files = list(hybrid_structural_result["data"]["files"])
                    has_more = len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                    structural_payload = {
                        "file_count": int(hybrid_structural_result["data"]["count"]),
                        "files": None if has_more else all_files,
                        "preview": all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE] if has_more else None,
                        "has_more": has_more,
                    }
                reply = "INSUFFICIENT_CONTEXT"
                assistant_message = _persist_message(
                    db,
                    "assistant",
                    reply,
                    context,
                    conversation_id=active_conversation_id,
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=reply,
                        error_code="INSUFFICIENT_CONTEXT",
                        retrieved_count=0,
                        type="hybrid" if query_type == QueryType.HYBRID else None,
                        file_count=(
                            int(hybrid_structural_result["data"]["count"])
                            if hybrid_structural_result is not None
                            else None
                        ),
                        structural=structural_payload,
                        semantic=(
                            {"result": "INSUFFICIENT_CONTEXT", "retrieved_chunks": 0}
                            if query_type == QueryType.HYBRID
                            else None
                        ),
                        structural_source="index" if query_type == QueryType.HYBRID else None,
                        semantic_source="retrieval" if query_type == QueryType.HYBRID else None,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=0,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )

            # Read OPENAI_API_KEY at call time -- never returned or logged.
            openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

            # API_EXCEPTION_BOUNDARY_LOCK_V1: wrap ALL execution in exception boundary
            # to eliminate 502 errors and ensure structured error responses.
            try:
                if not openai_api_key:
                    if conversation_repo_ids and db is not None:
                        try:
                            retrieval_payload = _normalize_retrieval_result(
                                _run_retrieval_query(
                                    user_query=message,
                                    db=db,
                                    repo_ids=conversation_repo_ids,
                                )
                            )
                        except RuntimeError as exc:
                            failure_code = (
                                "INVALID_CHUNK_SHAPE"
                                if str(exc) == "INVALID_CHUNK_SHAPE"
                                else "RETRIEVAL_INTEGRITY_FAILURE"
                            )
                            reply = failure_code
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code=failure_code,
                                    retrieved_count=0,
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=0,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                )
                            )
                        repo_chunks = list(retrieval_payload)
                        ctx_chunks = repo_chunks
                        _enforce_chunk_shape(ctx_chunks)
                        retrieved_count = len(repo_chunks)
                        retrieved_chunks = len(repo_chunks)
                        retrieved_files = len(
                            {
                                str(chunk.file_id)
                                for chunk in repo_chunks
                                if getattr(chunk, "file_id", None) is not None
                            }
                        )
                        repo_chunks_for_grounding = repo_chunks
                        context_integrity_state = _build_context_integrity_state(
                            conversation_id=active_conversation_id,
                            repo_id=(
                                str(conversation_repo_ids[0]) if conversation_repo_ids else None
                            ),
                            total_files=total_files if total_files > 0 else None,
                            retrieved_files=retrieved_files,
                            retrieved_chunks=retrieved_chunks,
                        )
                        if (
                            requires_full_context
                            and context_integrity_state.completeness_status
                            != CompletenessStatus.FULL_CONTEXT
                        ):
                            reply = "DATA_INCOMPLETE"
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code="DATA_INCOMPLETE",
                                    retrieved_count=retrieved_count,
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=retrieved_chunks,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                    details={
                                        "total_files": context_integrity_state.total_files,
                                        "retrieved_files": context_integrity_state.retrieved_files,
                                        "completeness_ratio": (
                                            context_integrity_state.completeness_ratio
                                        ),
                                    },
                                )
                            )
                        if (
                            requires_full_context
                            and context_integrity_state.completeness_status
                            == CompletenessStatus.FULL_CONTEXT
                            and hybrid_structural_result is not None
                        ):
                            all_files = list(hybrid_structural_result["data"]["files"])
                            lower_message = message.lower()
                            force_full_list = "list all files" in lower_message
                            preview_files = all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                            has_more = (
                                len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                                and not force_full_list
                            )
                            reply = "STRUCTURAL_QUERY_RESULT"
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code=None,
                                    retrieved_count=retrieved_count,
                                    type="structural",
                                    file_count=int(hybrid_structural_result["data"]["count"]),
                                    files=None if has_more else all_files,
                                    preview=preview_files if has_more else None,
                                    has_more=has_more,
                                    source=str(
                                        hybrid_structural_result.get("source") or "index_registry"
                                    ),
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=retrieved_chunks,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                )
                            )
                        if not repo_chunks:
                            reply = (
                                "INSUFFICIENT_CONTEXT"
                                if query_type == QueryType.HYBRID
                                else "REPO_CONTEXT_EMPTY"
                            )
                            assistant_message = _persist_message(
                                db,
                                "assistant",
                                reply,
                                context,
                                conversation_id=active_conversation_id,
                            )
                            logger.info(
                                "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                                active_conversation_id,
                                retrieved_count,
                                "INSUFFICIENT_CONTEXT"
                                if query_type == QueryType.HYBRID
                                else "REPO_CONTEXT_EMPTY",
                            )
                            structural_payload = None
                            if hybrid_structural_result is not None:
                                all_files = list(hybrid_structural_result["data"]["files"])
                                has_more = len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                                structural_payload = {
                                    "file_count": int(hybrid_structural_result["data"]["count"]),
                                    "files": None if has_more else all_files,
                                    "preview": all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                                    if has_more
                                    else None,
                                    "has_more": has_more,
                                }
                            return _json_response(
                                ChatPostResponse(
                                    conversation_id=active_conversation_id,
                                    reply=reply,
                                    error_code=(
                                        "INSUFFICIENT_CONTEXT"
                                        if query_type == QueryType.HYBRID
                                        else "REPO_CONTEXT_EMPTY"
                                    ),
                                    retrieved_count=retrieved_count,
                                    type="hybrid" if query_type == QueryType.HYBRID else None,
                                    structural=structural_payload,
                                    semantic=(
                                        {"result": "INSUFFICIENT_CONTEXT", "retrieved_chunks": 0}
                                        if query_type == QueryType.HYBRID
                                        else None
                                    ),
                                    structural_source=(
                                        "index" if query_type == QueryType.HYBRID else None
                                    ),
                                    semantic_source=(
                                        "retrieval" if query_type == QueryType.HYBRID else None
                                    ),
                                    repo_count=repo_count,
                                    total_chunks=total_chunks,
                                    retrieved_chunks=retrieved_chunks,
                                    tools_available=_TOOLS_AVAILABLE,
                                    user_message=user_message,
                                    assistant_message=assistant_message,
                                )
                            )
                        # NO_SILENT_FALLBACKS: with attached repos, never emit generic
                        # fallback content when grounded generation is unavailable.
                        reply = (
                            "INSUFFICIENT_CONTEXT"
                            if query_type == QueryType.HYBRID
                            else "RETRIEVAL_FAILURE"
                        )
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        logger.info(
                            "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                            active_conversation_id,
                            retrieved_count,
                            "INSUFFICIENT_CONTEXT"
                            if query_type == QueryType.HYBRID
                            else "RETRIEVAL_FAILURE",
                        )
                        structural_payload = None
                        if hybrid_structural_result is not None:
                            all_files = list(hybrid_structural_result["data"]["files"])
                            has_more = len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                            structural_payload = {
                                "file_count": int(hybrid_structural_result["data"]["count"]),
                                "files": None if has_more else all_files,
                                "preview": all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                                if has_more
                                else None,
                                "has_more": has_more,
                            }
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code=(
                                    "INSUFFICIENT_CONTEXT"
                                    if query_type == QueryType.HYBRID
                                    else "RETRIEVAL_FAILURE"
                                ),
                                retrieved_count=retrieved_count,
                                type="hybrid" if query_type == QueryType.HYBRID else None,
                                structural=structural_payload,
                                semantic=(
                                    {
                                        "result": "INSUFFICIENT_CONTEXT",
                                        "retrieved_chunks": retrieved_chunks,
                                    }
                                    if query_type == QueryType.HYBRID
                                    else None
                                ),
                                structural_source=(
                                    "index" if query_type == QueryType.HYBRID else None
                                ),
                                semantic_source=(
                                    "retrieval" if query_type == QueryType.HYBRID else None
                                ),
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=retrieved_chunks,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                            )
                        )
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
                    # GLOBAL_REPO_ASSET_SYSTEM_LOCK_V3 — Section 5 & 7:
                    # Strict validation: 409 on any non-ready repo.
                    # Timeout safety: running → failed after threshold (§8).
                    # -------------------------------------------------------
                    repo_chunks = []
                    no_index_data = False
                    if conversation_repo_ids and db is not None:
                        active_repo_ids = conversation_repo_ids
                        print("CTX_REPOS:", active_repo_ids)

                        if active_repo_ids:
                            _RUNNING_TIMEOUT = timedelta(minutes=15)

                            loaded_repos: list[Repo] = []
                            for rid in active_repo_ids:
                                repo_obj = db.get(Repo, rid)
                                if repo_obj:
                                    # Timeout safety: stuck "running" → "failed"
                                    if repo_obj.ingestion_status == "running":
                                        # updated_at may be stored as a naive datetime in SQLite;
                                        # coerce to UTC-aware for safe arithmetic.
                                        updated = repo_obj.updated_at
                                        if updated.tzinfo is None:
                                            updated = updated.replace(tzinfo=timezone.utc)
                                        age = datetime.now(timezone.utc) - updated
                                        if age > _RUNNING_TIMEOUT:
                                            repo_obj.ingestion_status = "failed"
                                            repo_obj.updated_at = datetime.now(timezone.utc)
                                            db.add(repo_obj)
                                            db.commit()
                                            db.refresh(repo_obj)
                                    loaded_repos.append(repo_obj)

                            if not loaded_repos:
                                no_index_data = True

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

                                # Strict enforcement — any non-success status or
                                # zero chunks blocks chat execution (LAW 5 + 8).
                                for repo_obj in loaded_repos:
                                    print("REPO_STATUS:", repo_obj.id, repo_obj.ingestion_status)
                                    if repo_obj.ingestion_status != "success":
                                        no_index_data = True
                                        break
                                    if repo_obj.total_chunks == 0:
                                        no_index_data = True
                                        break

                            # CONTRACT: when repo_ids are attached, retrieval MUST execute.
                            try:
                                retrieval_payload = _normalize_retrieval_result(
                                    _run_retrieval_query(
                                        user_query=message,
                                        db=db,
                                        repo_ids=active_repo_ids,
                                    )
                                )
                            except RuntimeError as exc:
                                failure_code = (
                                    "INVALID_CHUNK_SHAPE"
                                    if str(exc) == "INVALID_CHUNK_SHAPE"
                                    else "RETRIEVAL_INTEGRITY_FAILURE"
                                )
                                reply = failure_code
                                assistant_message = _persist_message(
                                    db,
                                    "assistant",
                                    reply,
                                    context,
                                    conversation_id=active_conversation_id,
                                )
                                return _json_response(
                                    ChatPostResponse(
                                        conversation_id=active_conversation_id,
                                        reply=reply,
                                        error_code=failure_code,
                                        retrieved_count=0,
                                        repo_count=repo_count,
                                        total_chunks=total_chunks,
                                        retrieved_chunks=0,
                                        tools_available=_TOOLS_AVAILABLE,
                                        user_message=user_message,
                                        assistant_message=assistant_message,
                                    )
                                )
                            repo_chunks = list(retrieval_payload)
                            ctx_chunks = repo_chunks
                            _enforce_chunk_shape(ctx_chunks)
                            retrieved_count = len(repo_chunks)
                            retrieved_chunks = len(repo_chunks)
                            retrieved_files = len(
                                {
                                    str(chunk.file_id)
                                    for chunk in repo_chunks
                                    if getattr(chunk, "file_id", None) is not None
                                }
                            )
                            repo_chunks_for_grounding = repo_chunks
                            context_integrity_state = _build_context_integrity_state(
                                conversation_id=active_conversation_id,
                                repo_id=str(active_repo_ids[0]) if active_repo_ids else None,
                                total_files=total_files if total_files > 0 else None,
                                retrieved_files=retrieved_files,
                                retrieved_chunks=retrieved_chunks,
                            )

                            print("REPO_CHUNKS:", len(repo_chunks))

                            if not repo_chunks:
                                no_index_data = True

                            if (
                                requires_full_context
                                and context_integrity_state.completeness_status
                                != CompletenessStatus.FULL_CONTEXT
                            ):
                                reply = "DATA_INCOMPLETE"
                                assistant_message = _persist_message(
                                    db,
                                    "assistant",
                                    reply,
                                    context,
                                    conversation_id=active_conversation_id,
                                )
                                return _json_response(
                                    ChatPostResponse(
                                        conversation_id=active_conversation_id,
                                        reply=reply,
                                        error_code="DATA_INCOMPLETE",
                                        retrieved_count=retrieved_count,
                                        repo_count=repo_count,
                                        total_chunks=total_chunks,
                                        retrieved_chunks=retrieved_chunks,
                                        tools_available=_TOOLS_AVAILABLE,
                                        user_message=user_message,
                                        assistant_message=assistant_message,
                                        details={
                                            "total_files": context_integrity_state.total_files,
                                            "retrieved_files": (
                                                context_integrity_state.retrieved_files
                                            ),
                                            "completeness_ratio": (
                                                context_integrity_state.completeness_ratio
                                            ),
                                        },
                                    )
                                )
                            if (
                                requires_full_context
                                and context_integrity_state.completeness_status
                                == CompletenessStatus.FULL_CONTEXT
                                and hybrid_structural_result is not None
                            ):
                                all_files = list(hybrid_structural_result["data"]["files"])
                                lower_message = message.lower()
                                force_full_list = "list all files" in lower_message
                                preview_files = all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                                has_more = (
                                    len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                                    and not force_full_list
                                )
                                reply = "STRUCTURAL_QUERY_RESULT"
                                assistant_message = _persist_message(
                                    db,
                                    "assistant",
                                    reply,
                                    context,
                                    conversation_id=active_conversation_id,
                                )
                                return _json_response(
                                    ChatPostResponse(
                                        conversation_id=active_conversation_id,
                                        reply=reply,
                                        error_code=None,
                                        retrieved_count=retrieved_count,
                                        type="structural",
                                        file_count=int(hybrid_structural_result["data"]["count"]),
                                        files=None if has_more else all_files,
                                        preview=preview_files if has_more else None,
                                        has_more=has_more,
                                        source=str(
                                            hybrid_structural_result.get("source")
                                            or "index_registry"
                                        ),
                                        repo_count=repo_count,
                                        total_chunks=total_chunks,
                                        retrieved_chunks=retrieved_chunks,
                                        tools_available=_TOOLS_AVAILABLE,
                                        user_message=user_message,
                                        assistant_message=assistant_message,
                                    )
                                )

                            if not no_index_data:
                                has_invalid_context_injection = any(
                                    (chunk.repo_id is None)
                                    or (not isinstance(chunk.file_path, str))
                                    or (not chunk.file_path.strip())
                                    or (not isinstance(chunk.content, str))
                                    or (not chunk.content.strip())
                                    for chunk in repo_chunks
                                )
                                if has_invalid_context_injection:
                                    reply = "RETRIEVAL_FAILURE"
                                    assistant_message = _persist_message(
                                        db,
                                        "assistant",
                                        reply,
                                        context,
                                        conversation_id=active_conversation_id,
                                    )
                                    logger.info(
                                        (
                                            "chat_response conversation_id=%s "
                                            "retrieved_count=%s error_code=%s"
                                        ),
                                        active_conversation_id,
                                        retrieved_count,
                                        "RETRIEVAL_FAILURE",
                                    )
                                    return _json_response(
                                        ChatPostResponse(
                                            conversation_id=active_conversation_id,
                                            reply=reply,
                                            error_code="RETRIEVAL_FAILURE",
                                            retrieved_count=retrieved_count,
                                            repo_count=repo_count,
                                            total_chunks=total_chunks,
                                            retrieved_chunks=retrieved_chunks,
                                            tools_available=_TOOLS_AVAILABLE,
                                            user_message=user_message,
                                            assistant_message=assistant_message,
                                        )
                                    )
                                metadata_block = (
                                    "\n\nCONTEXT METADATA:\n"
                                    f"- Total files in repository: "
                                    f"{context_integrity_state.total_files}\n"
                                    f"- Files retrieved for this query: "
                                    f"{context_integrity_state.retrieved_files}\n"
                                    f"- Total chunks retrieved: "
                                    f"{context_integrity_state.retrieved_chunks}\n"
                                    f"- Context completeness: "
                                    f"{context_integrity_state.completeness_status.value}\n\n"
                                    "INSTRUCTIONS:\n"
                                    "- Do NOT assume access to files beyond retrieved set\n"
                                    "- If question requires full repository knowledge and "
                                    "context is incomplete, state limitation clearly\n"
                                )
                                repo_block = "\n\n---\nREPO CONTEXT:\n"
                                for chunk in repo_chunks:
                                    repo_block += (
                                        f"\nREPO_ID: {chunk.repo_id}\n"
                                        f"FILE: {chunk.file_path}\n\n{chunk.content}\n"
                                    )
                                repo_block += "---\n"
                                base_system_prompt += metadata_block + repo_block

                    if has_explicit_repo_context and no_index_data:
                        reply = (
                            "INSUFFICIENT_CONTEXT"
                            if query_type == QueryType.HYBRID
                            else "REPO_CONTEXT_EMPTY"
                        )
                        assistant_message = _persist_message(
                            db,
                            "assistant",
                            reply,
                            context,
                            conversation_id=active_conversation_id,
                        )
                        logger.info(
                            "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                            active_conversation_id,
                            retrieved_count,
                            "INSUFFICIENT_CONTEXT"
                            if query_type == QueryType.HYBRID
                            else "REPO_CONTEXT_EMPTY",
                        )
                        structural_payload = None
                        if hybrid_structural_result is not None:
                            all_files = list(hybrid_structural_result["data"]["files"])
                            has_more = len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                            structural_payload = {
                                "file_count": int(hybrid_structural_result["data"]["count"]),
                                "files": None if has_more else all_files,
                                "preview": all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                                if has_more
                                else None,
                                "has_more": has_more,
                            }
                        return _json_response(
                            ChatPostResponse(
                                conversation_id=active_conversation_id,
                                reply=reply,
                                error_code=(
                                    "INSUFFICIENT_CONTEXT"
                                    if query_type == QueryType.HYBRID
                                    else "REPO_CONTEXT_EMPTY"
                                ),
                                retrieved_count=retrieved_count,
                                type="hybrid" if query_type == QueryType.HYBRID else None,
                                structural=structural_payload,
                                semantic=(
                                    {
                                        "result": "INSUFFICIENT_CONTEXT",
                                        "retrieved_chunks": retrieved_chunks,
                                    }
                                    if query_type == QueryType.HYBRID
                                    else None
                                ),
                                structural_source=(
                                    "index" if query_type == QueryType.HYBRID else None
                                ),
                                semantic_source=(
                                    "retrieval" if query_type == QueryType.HYBRID else None
                                ),
                                repo_count=repo_count,
                                total_chunks=total_chunks,
                                retrieved_chunks=retrieved_chunks,
                                tools_available=_TOOLS_AVAILABLE,
                                user_message=user_message,
                                assistant_message=assistant_message,
                            )
                        )

                    # -------------------------------------------------------
                    # MQP-CONTRACT: CHAT_CONTEXT_RETRIEVAL_V1
                    # INGEST_JOB_CONTEXT_INJECTION_V1 (Enhanced with contract requirements)
                    # Retrieve chunks from all successfully completed IngestJob
                    # records for the current conversation and inject them into
                    # the system prompt so the AI can answer questions about
                    # ingested files, URLs, and repositories.
                    # -------------------------------------------------------
                    if db is not None and active_conversation_id and not has_explicit_repo_context:
                        try:
                            # Step 2: Direct conversation-based context retrieval
                            context_chunks = _retrieve_conversation_context(
                                db=db,
                                conversation_id=active_conversation_id,
                                limit=50,
                            )

                            # Step 8: Add logging
                            print(f"CHAT_CONTEXT: chunks={len(context_chunks)}")

                            # Step 3: Validate and inject context
                            if context_chunks:
                                # Step 4: Build context string (limit to 3000-6000 chars)
                                context_text_parts = []
                                total_chars = 0
                                max_context_chars = 6000

                                for chunk in context_chunks:
                                    chunk_text = f"\nFILE: {chunk.file_path}\n\n{chunk.content}\n"
                                    if total_chars + len(chunk_text) > max_context_chars:
                                        # Truncate to fit within limit
                                        remaining = max_context_chars - total_chars
                                        if remaining > 0:
                                            context_text_parts.append(chunk_text[:remaining])
                                        break
                                    context_text_parts.append(chunk_text)
                                    total_chars += len(chunk_text)

                                # Step 5: Inject with strong grounding instruction
                                ingest_block = "\n\n---\nREPOSITORY CONTEXT:\n"
                                ingest_block += "".join(context_text_parts)
                                ingest_block += "\n---\n"
                                ingest_block += (
                                    "\nInstructions: Answer questions about the "
                                    "repository using the context above.\n"
                                )
                                ingest_block += (
                                    "If the answer is not in the context, "
                                    "respond with: NOT_FOUND_IN_CONTEXT\n"
                                )
                                base_system_prompt += ingest_block
                            else:
                                # Step 3: No repository context available
                                # Note: Contract suggests returning INSUFFICIENT_CONTEXT,
                                # but for flexibility, we let the LLM handle this gracefully.
                                base_system_prompt += (
                                    "\n\nNote: No repository data is linked to this "
                                    "conversation. Repository-specific questions cannot "
                                    "be answered.\n"
                                )
                        except Exception:
                            logger.warning(
                                "Failed to retrieve ingest job chunks for conversation %s",
                                active_conversation_id,
                                exc_info=True,
                            )

                    files = resolve_files_from_chunks(ctx_chunks, db)
                    ctx_files = [f.path for f in files]

                    if ctx_chunks and not ctx_files:
                        raise Exception("FILE_RESOLUTION_BROKEN")

                    logger.info("CTX_FILES: count=%s sample=%s", len(ctx_files), ctx_files[:3])
                    print("CTX_FILES:", ctx_files[:3])

                    # -------------------------------------------------------
                    # FILE_CONTEXT_INJECTION_V1 (V1 compat path):
                    # inject uploaded file references.
                    # -------------------------------------------------------
                    effective_file_refs: list[dict] = []
                    if context.files:
                        for file_ref in context.files:
                            effective_file_refs.append(file_ref)

                    if effective_file_refs:
                        file_block = "\n\n--- Uploaded Files Available for Reference ---\n"

                        for file_ref in effective_file_refs:
                            file_id_str = file_ref.get("id", "")
                            filename = file_ref.get("filename", "unnamed")
                            category = file_ref.get("category", "other")
                            file_block += f"\n- {filename} (Category: {category})"

                            if category == "github_repo":
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

                        file_block += "\n--- End of Uploaded Files ---\n"
                        base_system_prompt += file_block

                    # Build the ai_call closure; mode constraints are injected by the gateway.
                    # This is the ONLY path through which _call_openai_chat is reached for
                    # POST /api/chat — all AI calls are exclusive to mode_engine_gateway.
                    def _openai_ai_call(system_prompt: str) -> str:
                        return _run_chat_llm(
                            message=message,
                            api_key=openai_api_key,
                            history=history[:-1] if history else [],
                            system_prompt=system_prompt,
                        )

                    reply, _audit = mode_engine_gateway(
                        user_intent=message,
                        modes=active_modes,
                        ai_call=_openai_ai_call,
                        base_system_prompt=base_system_prompt,
                    )

                    if (
                        context_integrity_state is not None
                        and context_integrity_state.completeness_status
                        != CompletenessStatus.FULL_CONTEXT
                        and _ABSOLUTE_CLAIM_RE.search(reply or "")
                    ):
                        response_error_code = "CONTEXT_VIOLATION"
                        reply = (
                            "CONTEXT VIOLATION: The system does not have full "
                            "repository visibility to answer this request."
                        )

                    # Append citations to the reply if retrieval was performed.
                    if search_results:
                        reply += _format_citations(search_results)

                if (
                    has_explicit_repo_context
                    and repo_chunks_for_grounding
                    and response_error_code != "CONTEXT_VIOLATION"
                ):
                    if not _reply_references_repo_data(
                        reply=reply, repo_chunks=repo_chunks_for_grounding
                    ):
                        response_error_code = "RETRIEVAL_FAILURE"
                        reply = "RETRIEVAL_FAILURE"

                assistant_message = _persist_message(
                    db, "assistant", reply, context, conversation_id=active_conversation_id
                )
                logger.info(
                    "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                    active_conversation_id,
                    retrieved_count,
                    response_error_code,
                )
                if query_type == QueryType.HYBRID and hybrid_structural_result is not None:
                    all_files = list(hybrid_structural_result["data"]["files"])
                    has_more = len(all_files) > _STRUCTURAL_FILE_PAGINATION_THRESHOLD
                    return _json_response(
                        ChatPostResponse(
                            conversation_id=active_conversation_id,
                            reply="HYBRID_QUERY_RESULT",
                            error_code=response_error_code,
                            retrieved_count=retrieved_count,
                            type="hybrid",
                            file_count=int(hybrid_structural_result["data"]["count"]),
                            structural={
                                "file_count": int(hybrid_structural_result["data"]["count"]),
                                "files": None if has_more else all_files,
                                "preview": all_files[:_STRUCTURAL_FILE_PREVIEW_SIZE]
                                if has_more
                                else None,
                                "has_more": has_more,
                                "source": str(
                                    hybrid_structural_result.get("source") or "index_registry"
                                ),
                            },
                            semantic={
                                "result": (
                                    "INSUFFICIENT_CONTEXT"
                                    if retrieved_chunks == 0 or reply == "RETRIEVAL_FAILURE"
                                    else reply
                                ),
                                "retrieved_chunks": retrieved_chunks,
                                "source": "retrieval",
                            },
                            structural_source="index",
                            semantic_source="retrieval",
                            repo_count=repo_count,
                            total_chunks=total_chunks,
                            retrieved_chunks=retrieved_chunks,
                            tools_available=_TOOLS_AVAILABLE,
                            user_message=user_message,
                            assistant_message=assistant_message,
                        )
                    )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=reply,
                        error_code=response_error_code,
                        retrieved_count=retrieved_count,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=retrieved_chunks,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
            except RuntimeViolationError as exc:
                error_code = str(exc)
                assistant_message = _persist_message(
                    db, "assistant", error_code, context, conversation_id=active_conversation_id
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=error_code,
                        error_code=error_code,
                        retrieved_count=retrieved_count,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=retrieved_chunks,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                        execution_trace={
                            "classification": query_type.value,
                            "execution_path": ["chat", "route_query", "execute_query", "violation"],
                            "structural_called": False,
                            "retrieval_called": False,
                            "llm_called": False,
                        },
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
                logger.info(
                    "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                    active_conversation_id,
                    retrieved_count,
                    "SYSTEM_FAILURE",
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=error_reply,
                        error_code="SYSTEM_FAILURE",
                        retrieved_count=retrieved_count,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=retrieved_chunks,
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
                logger.info(
                    "chat_response conversation_id=%s retrieved_count=%s error_code=%s",
                    active_conversation_id,
                    retrieved_count,
                    "SYSTEM_FAILURE",
                )
                return _json_response(
                    ChatPostResponse(
                        conversation_id=active_conversation_id,
                        reply=error_reply,
                        error_code="SYSTEM_FAILURE",
                        retrieved_count=retrieved_count,
                        repo_count=repo_count,
                        total_chunks=total_chunks,
                        retrieved_chunks=retrieved_chunks,
                        tools_available=_TOOLS_AVAILABLE,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
        finally:
            if db is not None:
                db.close()
    except HTTPException:
        # GLOBAL_REPO_ASSET_SYSTEM_LOCK_V3: HTTPException (e.g. 409 REPO_NOT_READY)
        # must propagate as a real HTTP response — not be swallowed into SYSTEM_FAILURE.
        raise
    except Exception as e:
        logger.exception("HARD FAIL:", exc_info=True)
        return JSONResponse(
            status_code=200,
            content={
                "error": "SYSTEM_FAILURE",
                "message": str(e),
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
