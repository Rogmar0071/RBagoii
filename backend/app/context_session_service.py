from __future__ import annotations

import threading
import uuid
from typing import Optional

from sqlmodel import Session, select

from backend.app.context_pipeline import ActiveContextSession, run_context_pipeline
from backend.app.models import ConversationContext

_ACTIVE_CONTEXT_SESSION_REGISTRY: dict[str, ActiveContextSession] = {}
_REGISTRY_LOCK = threading.Lock()


def _get_cached_session(session_id: str) -> ActiveContextSession | None:
    with _REGISTRY_LOCK:
        return _ACTIVE_CONTEXT_SESSION_REGISTRY.get(session_id)


def _cache_session(active_session: ActiveContextSession) -> None:
    with _REGISTRY_LOCK:
        _ACTIVE_CONTEXT_SESSION_REGISTRY[active_session.session_id] = active_session


def ensure_active_context_session(
    db: Session,
    conversation_id: str,
    job_id: str,
    user_intent: str,
    alignment_confirmed: bool,
    alignment_refinement: Optional[str],
) -> ActiveContextSession:
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

    if ctx.active_context_session_id:
        existing = _get_cached_session(ctx.active_context_session_id)
        if existing is not None:
            return existing
        # Stale pointer: clear and regenerate deterministically.
        ctx.active_context_session_id = None
        db.add(ctx)
        db.commit()
        db.refresh(ctx)

    if not str(job_id or "").strip():
        raise RuntimeError("FINALIZE_BLOCKED: no repository/job bound to conversation")

    # Validate UUID shape early for deterministic failure semantics.
    try:
        uuid.UUID(job_id)
    except ValueError as exc:
        raise RuntimeError("FINALIZE_BLOCKED: invalid repository/job id") from exc

    active_session = run_context_pipeline(
        job_id=job_id,
        session=db,
        user_intent=user_intent,
        alignment_confirmed=alignment_confirmed,
        alignment_refinement=alignment_refinement,
    )

    _cache_session(active_session)
    ctx.active_context_session_id = active_session.session_id
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return active_session
