from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Optional

from sqlmodel import Session, select

from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
from backend.app.models import ConversationContext, IngestJob

_ACTIVE_CONTEXT_SESSION_REGISTRY: dict[str, Any] = {}
_REGISTRY_LOCK = threading.Lock()


@dataclass
class ContextSessionResult:
    status: str
    session: Any | None = None
    summary: dict[str, Any] | None = None
    details: str | None = None


def compute_intent_hash(message: str, refinement: str | None) -> str:
    base = f"{(message or '').strip()}::{(refinement or '').strip()}"
    return hashlib.sha256(base.encode()).hexdigest()


def _get_cached_session(session_id: str) -> Any | None:
    with _REGISTRY_LOCK:
        return _ACTIVE_CONTEXT_SESSION_REGISTRY.get(session_id)


def _cache_session(active_session: Any) -> None:
    with _REGISTRY_LOCK:
        _ACTIVE_CONTEXT_SESSION_REGISTRY[active_session.session_id] = active_session


def _remove_cached_session(session_id: str) -> None:
    with _REGISTRY_LOCK:
        _ACTIVE_CONTEXT_SESSION_REGISTRY.pop(session_id, None)


def ensure_active_context_session(
    db: Session,
    conversation_id: str,
    message: str,
    alignment_confirmed: bool,
    alignment_refinement: Optional[str],
) -> ContextSessionResult:
    """
    Ensure a deterministic ActiveContextSession exists for this conversation.
    """
    ctx = db.exec(
        select(ConversationContext).where(ConversationContext.conversation_id == conversation_id)
    ).first()
    if ctx is None:
        ctx = ConversationContext(conversation_id=conversation_id, repo_id=None)
        db.add(ctx)
        db.commit()
        db.refresh(ctx)

    job = db.exec(
        select(IngestJob)
        .where(IngestJob.conversation_id == conversation_id)
        .where(IngestJob.status == "success")
        .order_by(IngestJob.created_at.desc())
    ).first()
    if job is None:
        return ContextSessionResult(status="FINALIZE_BLOCKED", details="NO_INGEST_CONTEXT")

    current_hash = compute_intent_hash(message, alignment_refinement)

    if ctx.active_context_session_id:
        existing = _get_cached_session(ctx.active_context_session_id)
        if (
            existing is not None
            and getattr(existing, "intent_hash", None) == current_hash
            and str(getattr(existing, "job_id", "")) == str(job.id)
        ):
            return ContextSessionResult(status="SUCCESS", session=existing)

        _remove_cached_session(ctx.active_context_session_id)
        ctx.active_context_session_id = None
        db.add(ctx)
        db.commit()
        db.refresh(ctx)

    try:
        active_session = run_context_pipeline(
            job_id=str(job.id),
            session=db,
            user_intent=message,
            alignment_confirmed=alignment_confirmed,
            alignment_refinement=alignment_refinement,
        )
    except AlignmentRequiredError as exc:
        return ContextSessionResult(status="ALIGNMENT_REQUIRED", summary=exc.summary)
    except Exception as exc:
        return ContextSessionResult(status="FINALIZE_BLOCKED", details=str(exc))

    active_session.intent_hash = current_hash
    _cache_session(active_session)
    ctx.active_context_session_id = active_session.session_id
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ContextSessionResult(status="SUCCESS", session=active_session)
