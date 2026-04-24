"""
Shared test utilities for backend tests.
"""

from __future__ import annotations

import uuid


def _seed_ingest_context(conversation_id: str) -> None:
    from sqlmodel import Session

    import backend.app.database as db_module
    from backend.app.models import CodeSymbol, EntryPoint, IngestJob, RepoChunk, RepoFile

    with Session(db_module.get_engine()) as db:
        exists = db.get(IngestJob, uuid.UUID(conversation_id))
        # IngestJob.id is independent from conversation_id; this lookup is only a
        # cheap fast-path and may miss existing rows.
        if exists is not None:
            return

        job_id = uuid.uuid4()
        file_id = uuid.uuid4()
        db.add(
            IngestJob(
                id=job_id,
                kind="repo",
                source="https://github.com/acme/test-context-spine",
                branch="main",
                status="success",
                conversation_id=conversation_id,
                file_count=1,
                chunk_count=1,
            )
        )
        db.add(
            RepoFile(
                id=file_id,
                repo_id=job_id,
                path="app.py",
                language="python",
                size_bytes=100,
            )
        )
        db.add(
            CodeSymbol(
                file_id=file_id,
                name="main",
                symbol_type="function",
                start_line=1,
                end_line=1,
            )
        )
        db.add(EntryPoint(file_id=file_id, entry_type="main", line=1))
        db.add(
            RepoChunk(
                ingest_job_id=job_id,
                file_id=file_id,
                file_path="app.py",
                content="def main():\n    return 'ok'\n",
                chunk_index=0,
                token_estimate=6,
            )
        )
        db.commit()


def _chat_payload(message: str = "test", **overrides) -> dict:
    """Build a valid POST /api/chat request body.

    CONVERSATION_LIFECYCLE_ENFORCEMENT_LOCK: conversation_id is required on
    every request.  If not supplied via ``overrides``, a fresh UUID4 is
    generated so tests that don't care about conversation identity still pass
    validation without needing to wire up a real conversation first.

    Usage::

        # Minimal — auto-generates conversation_id
        body = _chat_payload("Hello")

        # Specific conversation
        body = _chat_payload("Hello", conversation_id=cid)

        # With extra fields
        body = _chat_payload("Hello", conversation_id=cid, force_new_session=True)
    """
    cid = overrides.pop("conversation_id", None) or str(uuid.uuid4())
    if overrides.pop("seed_ingest_context", True):
        _seed_ingest_context(cid)
    return {
        "message": message,
        "conversation_id": cid,
        "alignment_confirmed": overrides.pop("alignment_confirmed", True),
        **overrides,
    }
