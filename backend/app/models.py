"""
backend.app.models
==================
SQLModel data models for folder-based clip bundles.

Tables
------
- global_chat_messages : persisted global chat history for /api/chat
- folders          : top-level container for a recorded/picked clip and all derived data
- folder_messages  : per-folder chat history
- jobs             : background processing jobs (analyze / blueprint)
- artifacts        : object-storage references for files produced by jobs
- ops_events       : server-side operations log (backend + worker activity)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel, UniqueConstraint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# conversations / global_chat_messages
# ---------------------------------------------------------------------------


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(sa_column=Column(sa.Text, primary_key=True, nullable=False))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


class GlobalChatMessage(SQLModel, table=True):
    __tablename__ = "global_chat_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # user / assistant / system
    role: str
    content: str = Field(sa_column=Column(sa.Text))
    # CONVERSATION_LIFECYCLE_V1: every message belongs to a conversation.
    # "legacy_default" is the containment zone for requests without an explicit
    # conversation_id — it is NOT normal system behavior.
    conversation_id: str = Field(default="legacy_default", index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    domain_profile_id: Optional[str] = Field(default=None, index=True)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    # When a user message is edited, the original is preserved but marked
    # superseded by the new message's id.
    superseded_by_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# Backward-compatible message model alias for conversation chat history.
Message = GlobalChatMessage


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


class Folder(SQLModel, table=True):
    __tablename__ = "folders"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )
    # pending / uploading / queued / running / done / failed / audio_ready
    status: str = Field(default="pending")
    clip_object_key: Optional[str] = Field(default=None)
    audio_object_key: Optional[str] = Field(default=None)

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# folder_messages
# ---------------------------------------------------------------------------


class FolderMessage(SQLModel, table=True):
    __tablename__ = "folder_messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # user / assistant / system
    role: str
    content: str = Field(sa_column=Column(sa.Text))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # analyze / blueprint
    type: str
    # queued / running / succeeded / failed
    status: str = Field(default="queued")
    progress: int = Field(default=0)
    error: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    execution_locked: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
    )
    execution_attempts: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default="0"),
    )
    last_execution_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )
    rq_job_id: Optional[str] = Field(default=None)
    source_artifact_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )

    # ---------------------------------------------------------------------------
    # Pipeline v1 checkpoint fields (analyze stage)
    # ---------------------------------------------------------------------------
    # Current pipeline stage: 'prepare' | 'frames' | 'summarize'
    analyze_stage: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    # How many frames have been extracted and uploaded so far.
    analyze_cursor_frame_index: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True)
    )
    # Estimated total frames to extract (set during prepare; may be None).
    analyze_total_frames: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True)
    )
    # Clip object key cached in the checkpoint for robustness.
    analyze_clip_object_key: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True)
    )
    # User-selected per-job options (JSON).  Persisted at enqueue time and
    # read by the pipeline to decide which optional stages to run.
    # Schema: {"additional_analysis": {"enabled": bool, "keyframes": bool, ...}}
    analyze_options: Optional[Any] = Field(default=None, sa_column=Column(sa.JSON, nullable=True))
    # Segment cursor – number of segments processed so far.
    # Used by baseline_segments stage (analyze) and segments stage (analyze_optional).
    analyze_cursor_segment_index: Optional[int] = Field(
        default=None, sa_column=Column(sa.Integer, nullable=True)
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


class Artifact(SQLModel, table=True):
    __tablename__ = "artifacts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    folder_id: uuid.UUID = Field(
        foreign_key="folders.id",
        index=True,
    )
    # The job that produced this artifact.  NULL for artifacts created directly
    # by the API (e.g. the 'clip' artifact uploaded via /clip).
    job_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    # clip / analysis_json / analysis_md / blueprint_json / blueprint_md / transcript
    type: str
    object_key: str
    display_name: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# ops_events
# ---------------------------------------------------------------------------


class OpsEvent(SQLModel, table=True):
    __tablename__ = "ops_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, index=True),
    )
    # backend / worker / storage / rq / db / auth
    source: str = Field(sa_column=Column(sa.Text, index=True))
    # debug / info / warning / error
    level: str = Field(sa_column=Column(sa.Text, index=True))
    # e.g. "folders.create", "clip.upload.started", "jobs.enqueue"
    event_type: str = Field(sa_column=Column(sa.Text, index=True))
    message: str = Field(sa_column=Column(sa.Text))
    folder_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    job_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    artifact_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True, index=True),
    )
    rq_job_id: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    request_id: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    http_method: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    http_path: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    http_status: Optional[int] = Field(default=None, sa_column=Column(sa.Integer, nullable=True))
    duration_ms: Optional[int] = Field(default=None, sa_column=Column(sa.Integer, nullable=True))
    error_type: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    error_detail: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    details_json: Optional[Any] = Field(
        default=None,
        sa_column=Column(sa.JSON, nullable=True),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        # Truncate error_detail to 2000 chars.
        if data.get("error_detail") and len(data["error_detail"]) > 2000:
            data["error_detail"] = data["error_detail"][:2000]
        super().__init__(**data)


# ---------------------------------------------------------------------------
# analysis_jobs
# ---------------------------------------------------------------------------


class AnalysisJob(SQLModel, table=True):
    """Standalone analysis job for uploaded zips / clips (session-based pipeline)."""

    __tablename__ = "analysis_jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Path of the uploaded file (absolute, under /tmp/uploads/).
    file_path: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    # queued / running / succeeded / failed
    status: str = Field(default="queued")
    execution_locked: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
    )
    execution_attempts: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default="0"),
    )
    last_execution_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    # Full analysis result JSON (populated on success).
    results_json: Optional[Any] = Field(default=None, sa_column=Column(sa.JSON, nullable=True))
    # List of error dicts recorded during processing.
    errors_json: Optional[Any] = Field(default=None, sa_column=Column(sa.JSON, nullable=True))
    # List of warning dicts recorded during processing.
    warnings_json: Optional[Any] = Field(default=None, sa_column=Column(sa.JSON, nullable=True))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# chat_files
# ---------------------------------------------------------------------------

# NOTE: RepoChunk (repo_chunks table) is defined AFTER ChatFile because it
# carries a foreign key to chat_files.id.


class ChatFile(SQLModel, table=True):
    """Files uploaded to a chat conversation for AI reference."""

    __tablename__ = "chat_files"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Conversation this file belongs to
    conversation_id: str = Field(index=True)
    # Optional workspace for multi-workspace isolation
    workspace_id: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True, index=True)
    )
    # Original filename
    filename: str = Field(sa_column=Column(sa.Text))
    # MIME type
    mime_type: str = Field(sa_column=Column(sa.Text))
    # File size in bytes
    size_bytes: int
    # Object storage key
    object_key: str = Field(sa_column=Column(sa.Text))
    # Category for grouping (document, image, code, data, etc.)
    category: str = Field(default="other")
    # Whether file is included in AI context
    included_in_context: bool = Field(default=True)
    # AI-friendly extracted text content (for searchable files)
    extracted_text: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    # Ingestion status for github_repo files: "success" | "failed" | "partial"
    ingestion_status: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# repo_chunks
# ---------------------------------------------------------------------------


class RepoChunk(SQLModel, table=True):
    """A content chunk from an ingested file, URL, or GitHub repository."""

    __tablename__ = "repo_chunks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # REPO_CONTEXT_FINALIZATION_V1 — Phase 1:
    # repo_id is the primary FK (Repo first-class entity).
    # chat_file_id is kept nullable for backward compatibility with V1 ingestion path.
    repo_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("repos.id"), nullable=True, index=True),
    )
    chat_file_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("chat_files.id"), nullable=True, index=True),
    )
    # New unified ingestion pipeline FK (ingest_jobs.id).
    # NULL for chunks created by the legacy ingestion paths.
    ingest_job_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("ingest_jobs.id"), nullable=True, index=True),
    )
    # Canonical source-file identity for this chunk (RepoFile.id or equivalent source file id)
    file_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, nullable=False, index=True),
    )
    # Path of the source file within the repository (or filename for uploads)
    file_path: str = Field(sa_column=Column(sa.Text))
    # Chunk text content
    content: str = Field(sa_column=Column(sa.Text))
    # Zero-based ordinal of this chunk within its source file (lower = earlier)
    chunk_index: int = Field(default=0)
    # Approximate token count (characters / 4)
    token_estimate: int = Field(default=0)
    # Source URL for URL-ingested chunks (used for citations)
    source_url: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True)
    )
    # ------------------------------------------------------------------
    # GRAPH_RECONSTRUCTION_LAYER_V1
    # Structural metadata for deterministic code-graph reconstruction.
    # All fields are nullable — existing chunks are unaffected.
    # ------------------------------------------------------------------

    # High-level structural category: CLASS | FUNCTION | IMPORT | CONFIG | DATA | DOC
    chunk_type: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    # Logical grouping key — file path or module path (mirrors file_path for most chunks)
    graph_group: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    # Primary symbol defined in this chunk (class name, function name, etc.)
    symbol: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    # Extracted dependency references (imports, calls, links) — JSON list of strings
    dependencies: Optional[Any] = Field(
        default=None,
        sa_column=Column(sa.JSON, nullable=True),
    )
    # Line range within the source chunk for precise reconstruction
    start_line: Optional[int] = Field(default=None)
    end_line: Optional[int] = Field(default=None)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        from backend.app.identity_authority import assert_constructor_authority

        assert_constructor_authority()
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


class Repo(SQLModel, table=True):
    """
    GLOBAL_REPO_ASSET_INGESTION_AND_CONTEXT_BINDING_V1.

    Global repository asset.  Identity = (repo_url, branch) — unique across
    the entire system.  conversation_id is retained as nullable for backward
    compatibility with the legacy per-conversation ingestion path.
    Conversation-level bindings are expressed via ConversationRepo.
    """

    __tablename__ = "repos"
    __table_args__ = (
        sa.UniqueConstraint("repo_url", "branch", name="uq_repos_url_branch"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Legacy field: set by the old per-conversation endpoint; NULL for repos
    # created via the new global POST /api/repos/add endpoint.
    conversation_id: Optional[str] = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, index=True),
    )
    # Full GitHub URL, e.g. https://github.com/owner/repo
    repo_url: str = Field(sa_column=Column(sa.Text))
    # GitHub owner login
    owner: str = Field(sa_column=Column(sa.Text))
    # Repository name (without owner prefix)
    name: str = Field(sa_column=Column(sa.Text))
    # Target branch
    branch: str = Field(default="main")
    # pending / running / success / failed
    ingestion_status: str = Field(default="pending")
    # Counts populated by the ingestion worker
    total_files: int = Field(default=0)
    total_chunks: int = Field(default=0)
    # --- REPO_VALIDATION_LAYER_V1 ---
    # pending / validated / failed
    validation_status: str = Field(default="pending")
    validation_score: int = Field(default=0)
    # TRUTH | REFERENCE | WIP | UNKNOWN
    trust_class: str = Field(default="UNKNOWN")
    # JSON blob produced by the validation engine
    validation_signals: Optional[Any] = Field(
        default=None,
        sa_column=Column(sa.JSON, nullable=True),
    )
    validated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        from backend.app.identity_authority import assert_constructor_authority

        assert_constructor_authority()
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# repo_index_registry
# ---------------------------------------------------------------------------


class RepoIndexRegistry(SQLModel, table=True):
    """
    Deterministic index visibility registry per Repo.

    Tracks ingestion-backed index truth used by /repos/{repo_id}/structure and
    retrieval enforcement.
    """

    __tablename__ = "repo_index_registry"

    repo_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repos.id"), primary_key=True, nullable=False)
    )
    total_files: int = Field(default=0)
    total_chunks: int = Field(default=0)
    indexed: bool = Field(default=False)
    last_indexed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    status: str = Field(default="created")
    # Backend-observed retrieval count from the latest /repos/{repo_id}/retrieve call.
    # Kept server-side so UI can show "Retrieved (last query)" without client caching.
    last_retrieved_count: int = Field(default=0)
    min_chunks_per_file: int = Field(default=0)
    max_chunks_per_file: int = Field(default=0)
    median_chunks_per_file: float = Field(default=0.0)
    chunk_variance_flagged: bool = Field(default=False)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# conversation_contexts
# ---------------------------------------------------------------------------


class ConversationContext(SQLModel, table=True):
    """
    CONVERSATION_CONTEXT_BINDING_V1.1.

    Deterministic active context binding for a conversation.
    Exactly one row per conversation_id; repo_id is nullable for non-repo scopes.
    """

    __tablename__ = "conversation_contexts"
    __table_args__ = (
        sa.UniqueConstraint("conversation_id", name="uq_conversation_contexts_conversation_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: str = Field(
        sa_column=Column(
            sa.Text,
            sa.ForeignKey("conversations.id"),
            nullable=False,
            index=True,
        )
    )
    repo_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("repos.id"), nullable=True, index=True),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        from backend.app.identity_authority import assert_constructor_authority

        assert_constructor_authority()
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# conversation_repos
# ---------------------------------------------------------------------------


class ConversationRepo(SQLModel, table=True):
    """
    GLOBAL_REPO_ASSET_INGESTION_AND_CONTEXT_BINDING_V1.

    Pure binding layer between a conversation and a global Repo asset.
    No ingestion logic lives here — this is a pointer only.
    """

    __tablename__ = "conversation_repos"
    __table_args__ = (
        sa.UniqueConstraint(
            "conversation_id", "repo_id", name="uq_conversation_repos"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: str = Field(sa_column=Column(sa.Text, nullable=False, index=True))
    repo_id: uuid.UUID = Field(
        sa_column=Column(
            sa.Uuid, sa.ForeignKey("repos.id"), nullable=False, index=True
        )
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        from backend.app.identity_authority import assert_constructor_authority

        assert_constructor_authority()
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


# ---------------------------------------------------------------------------
# repo_validation_snapshots
# ---------------------------------------------------------------------------


class RepoValidationSnapshot(SQLModel, table=True):
    """
    REPO_VALIDATION_SNAPSHOT_V1.

    Immutable audit record written each time a Repo is validated.
    Captures the score, trust class, and raw signals at the moment of
    validation so the full history is preserved even when the Repo row
    is overwritten by a later validation run.
    """

    __tablename__ = "repo_validation_snapshots"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    repo_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repos.id"), nullable=False, index=True)
    )

    validation_score: int
    trust_class: str
    validation_signals: Optional[Any] = Field(
        default=None,
        sa_column=Column(sa.JSON, nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(sa.DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# ingest_jobs   (unified ingestion pipeline)
# ---------------------------------------------------------------------------


class IngestJob(SQLModel, table=True):
    """
    Tracks a single ingestion request: file upload, URL scrape, or GitHub repo.

    This is the central record for the new unified ingestion pipeline
    (backend.app.ingest_pipeline).  Legacy ingestion paths (Repo / ChatFile)
    remain for backward compatibility but all new ingestion goes through here.

    MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION

    kind values
    -----------
    "file"  — a local file uploaded via POST /v1/ingest/file
    "url"   — a web page fetched via POST /v1/ingest/url
    "repo"  — a GitHub repository ingested via POST /v1/ingest/repo

    status values (MQP-CONTRACT: AIC-v1.1 STATE MACHINE)
    ---------------------------------------------------------------------
    Linear progression (strict state machine):

    created → stored → queued → running → processing → indexing → finalizing → success

    Any state can transition to: failed

    State definitions:
    - created: Job record exists, no data yet
    - stored: Blob data stored in database (≤ 500MB validated)
    - queued: Job in RQ queue, awaiting worker
    - running: Worker started, blob loaded from DB
    - processing: Content parsed, extraction in progress
    - indexing: Chunks created and being indexed
    - finalizing: Final persistence, metadata updates
    - success: Completed successfully (terminal)
    - failed: Failed with error (terminal)

    Storage:
    - ALL data stored as BLOB in database (blob_data field)
    - NO filesystem dependencies
    - Workers read ONLY from database
    """

    __tablename__ = "ingest_jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # "file" | "url" | "repo"
    kind: str = Field(sa_column=Column(sa.Text, index=True))

    # Human-readable source identifier:
    #   file → original filename
    #   url  → the URL
    #   repo → "{repo_url}@{branch}"
    source: str = Field(sa_column=Column(sa.Text))

    # For kind="repo": target branch (also embedded in source for uniqueness)
    branch: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    # MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE
    # Binary blob storage for all ingestion data (replaces filesystem dependency)
    # All data (file uploads, fetched URLs, repo content) stored here
    blob_data: Optional[bytes] = Field(
        default=None, sa_column=Column(sa.LargeBinary, nullable=True)
    )

    # MIME type of the stored blob
    blob_mime_type: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    # Size of stored blob in bytes (for validation: must be ≤ 500MB)
    blob_size_bytes: int = Field(default=0)

    # created / stored / queued / running / processing / indexing / finalizing / success / failed
    status: str = Field(default="created", sa_column=Column(sa.Text, index=True))
    execution_locked: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
    )
    execution_attempts: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default="0"),
    )
    last_execution_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )

    # Progress percentage (0-100)
    progress: int = Field(default=0)

    # Human-readable error message (set on failure)
    error: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    # Conversation / workspace scoping (nullable — global ingestion is allowed)
    conversation_id: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True, index=True)
    )
    workspace_id: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True, index=True)
    )

    # Counts populated as the pipeline runs
    file_count: int = Field(default=0)
    chunk_count: int = Field(default=0)
    repo_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("repos.id"), nullable=True, index=True),
    )
    avg_chunks_per_file: float = Field(default=0.0)
    skipped_files_count: int = Field(default=0)
    min_chunks_per_file: int = Field(default=0)
    max_chunks_per_file: int = Field(default=0)
    median_chunks_per_file: float = Field(default=0.0)
    chunk_variance_flagged: bool = Field(default=False)
    chunk_variance_delta_pct: float = Field(default=0.0)

    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        if "updated_at" not in data or data["updated_at"] is None:
            data["updated_at"] = _utcnow()
        super().__init__(**data)



# ---------------------------------------------------------------------------
# GRAPH-LAYER-CORRECTION v1.1
# repo_files / code_symbols / file_dependencies / symbol_call_edges / entry_points
#
# MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1
#
# SINGLE GRAPH AUTHORITY.  ONE ENTITY = ONE TABLE.
# FileNode, SymbolNode, FileEdge have been DELETED and replaced by:
#   RepoFile     — canonical file identity
#   CodeSymbol   — canonical symbol identity
#   FileDependency   — resolved file→file edges (NEVER NULL target)
#   SymbolCallEdge   — symbol→symbol call edges (source always valid FK)
#   EntryPoint       — detected execution entry points
# ---------------------------------------------------------------------------


class RepoFile(SQLModel, table=True):
    """
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 2

    Canonical file identity within an ingestion job.
    ONE ENTITY = ONE TABLE — SOLE authority for file identity.

    Replaces the deleted FileNode / file_nodes table.
    """

    __tablename__ = "repo_files"
    __table_args__ = (
        UniqueConstraint("repo_id", "path", name="uq_repo_files_path"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # repo_id maps to IngestJob.id for unified-pipeline jobs
    repo_id: uuid.UUID = Field(sa_column=Column(sa.Uuid, nullable=False, index=True))
    path: str = Field(sa_column=Column(sa.Text, nullable=False, index=True))
    language: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    size_bytes: int = Field(default=0)
    content_hash: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    def __init__(self, **data):
        from backend.app.identity_authority import assert_constructor_authority

        assert_constructor_authority()
        super().__init__(**data)


class CodeSymbol(SQLModel, table=True):
    """
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 5

    Canonical symbol (function / class) extracted from a RepoFile.
    file_id is NEVER NULL — orphan symbols are forbidden.

    Replaces the deleted SymbolNode / symbol_nodes table.
    """

    __tablename__ = "code_symbols"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repo_files.id"), nullable=False, index=True)
    )
    name: str = Field(sa_column=Column(sa.Text, nullable=False))
    # function | class
    symbol_type: str = Field(sa_column=Column(sa.Text, nullable=False))
    start_line: int
    end_line: int


class FileDependency(SQLModel, table=True):
    """
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 4

    A RESOLVED file-to-file dependency edge.  Both FKs are non-nullable.
    Rows are ONLY inserted when the target path resolves to a known RepoFile.
    Unresolved imports are silently dropped — never stored.

    INVARIANT: target_file_id IS NEVER NULL.
    """

    __tablename__ = "file_dependencies"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_file_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repo_files.id"), nullable=False, index=True)
    )
    target_file_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repo_files.id"), nullable=False, index=True)
    )


class SymbolCallEdge(SQLModel, table=True):
    """
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 6

    A directed call edge FROM a known CodeSymbol to another.
    source_symbol_id is always a valid FK (NOT NULL) — orphan call edges are
    forbidden.  target_symbol_id is nullable: the callee may be external.
    """

    __tablename__ = "symbol_call_edges"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Caller — must be a persisted CodeSymbol
    source_symbol_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("code_symbols.id"), nullable=False, index=True)
    )
    # Raw callee name as it appears in the source
    callee_name: str = Field(sa_column=Column(sa.Text, nullable=False))
    # Resolved target (nullable — callee may be an external symbol)
    target_symbol_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            sa.Uuid, sa.ForeignKey("code_symbols.id"), nullable=True, index=True
        ),
    )


class EntryPoint(SQLModel, table=True):
    """
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 7

    Detected execution entry point within a RepoFile.
    entry_type is one of: "main" | "server" | "framework".
    file_id is NEVER NULL.
    """

    __tablename__ = "entry_points"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, sa.ForeignKey("repo_files.id"), nullable=False, index=True)
    )
    # main | server | framework
    entry_type: str = Field(sa_column=Column(sa.Text, nullable=False))
    line: int


# ---------------------------------------------------------------------------
# PHASE 3 — Context Pipeline persistence
#
# MQP-CONTRACT: RBOII-PHASE1-SEAL + PHASE3-PIPELINE-SPINE v1.0
#
# Three tables persist the Phase 3 pipeline state:
#   context_pipeline_runs   — one record per run_context_pipeline invocation
#   context_gap_records     — one row per ContextGap detected
#   context_alignment_records — one row per confirmed AlignedIntentContract
# ---------------------------------------------------------------------------


class ContextPipelineRun(SQLModel, table=True):
    """
    Tracks a single run_context_pipeline execution.

    status values
    -------------
    pending_alignment  — pipeline halted at Stage 6 (AlignmentRequiredError raised)
    aligned            — user confirmed; pipeline reached Stage 7 (FinalContext built)
    active             — Stage 8 completed; ActiveContextSession live
    failed             — pipeline terminated with RuntimeError
    """

    __tablename__ = "context_pipeline_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Phase 1 IngestJob this run is anchored to
    ingest_job_id: uuid.UUID = Field(
        sa_column=Column(sa.Uuid, nullable=False, index=True)
    )

    # Free-form intent text provided by the user
    user_intent: str = Field(default="", sa_column=Column(sa.Text))

    # pending_alignment | aligned | active | failed
    status: str = Field(default="pending_alignment", sa_column=Column(sa.Text, index=True))

    # UUID of the ActiveContextSession produced at Stage 8 (NULL until activated)
    active_session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column(sa.Uuid, nullable=True)
    )

    # Error message (set on failure)
    error: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )
    activated_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


class ContextGapRecord(SQLModel, table=True):
    """
    Persisted record of a ContextGap detected during Stage 5 gap detection.

    gap_type values: no_entry_points | unresolved_calls |
                     ambiguous_intent | missing_execution_path
    severity values: critical | warning
    """

    __tablename__ = "context_gap_records"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    pipeline_run_id: uuid.UUID = Field(
        sa_column=Column(
            sa.Uuid, sa.ForeignKey("context_pipeline_runs.id"), nullable=False, index=True
        )
    )

    gap_type: str = Field(sa_column=Column(sa.Text, nullable=False, index=True))
    severity: str = Field(sa_column=Column(sa.Text, nullable=False, index=True))
    description: str = Field(sa_column=Column(sa.Text, nullable=False))

    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)


class ContextAlignmentRecord(SQLModel, table=True):
    """
    Persisted record of a confirmed AlignedIntentContract (Stage 6 output).

    One record per run where alignment_confirmed=True was provided.
    """

    __tablename__ = "context_alignment_records"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    pipeline_run_id: uuid.UUID = Field(
        sa_column=Column(
            sa.Uuid, sa.ForeignKey("context_pipeline_runs.id"), nullable=False, index=True
        )
    )

    user_intent: str = Field(sa_column=Column(sa.Text, nullable=False))
    refinement: Optional[str] = Field(
        default=None, sa_column=Column(sa.Text, nullable=True)
    )
    # JSON blob of the full system_summary returned to the user
    system_summary: Optional[Any] = Field(
        default=None, sa_column=Column(sa.JSON, nullable=True)
    )

    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), default=_utcnow),
    )

    def __init__(self, **data):
        if "created_at" not in data or data["created_at"] is None:
            data["created_at"] = _utcnow()
        super().__init__(**data)
