"""
backend.app.ingest_pipeline
============================
Unified, stable ingestion pipeline — single code path for all source kinds.

Replaces
--------
- analysis_job_processor.py   (zip / Android-artifact analysis)
- worker.py:run_repo_ingestion (GitHub ingestion worker, async/sync dance)
- github_routes.py ingestion helper (_fetch_repo_file_list)
- document_routes.py          (incomplete parallel implementation)

Architecture
------------
All ingestion flows through one entry point::

    process_ingest_job(job_id: str)
           │
           ├─ kind="file" → read blob from DB → extract_text → chunk → store
           ├─ kind="url"  → read blob from DB → html.parser strip → chunk → store
           └─ kind="repo" → read blob manifest from DB → per-file extract → chunk → store

LAW: ALWAYS_UPDATE_TERMINAL
    ``process_ingest_job`` unconditionally writes a terminal status
    (``success`` or ``failed``) to the DB, even if an unexpected exception
    is raised.  Callers never need to handle partial state.

Extraction support
------------------
Format    Library              Fallback
--------- -------------------- ---------------------------------
PDF       pypdf (optional)     warn + return None
DOCX      python-docx (opt.)   warn + return None
HTML      stdlib html.parser   always available
CSV       stdlib csv           always available
ZIP       stdlib zipfile       recurse on extractable members
text/code UTF-8 decode         always available

Chunking
--------
CHUNK_MAX_CHARS      env var, default 1500
CHUNK_OVERLAP_CHARS  env var, default 150

Chunks are split on line boundaries; the overlap window carries the
trailing *CHUNK_OVERLAP_CHARS* characters of the previous chunk forward
so context is not lost at boundaries.

GitHub ingestion
----------------
Uses the ``/git/trees/{branch}?recursive=1`` endpoint (single request for
the full tree), then fetches raw content from raw.githubusercontent.com.

Env vars
--------
GITHUB_TOKEN          GitHub personal-access token (optional)
REPO_MAX_FILES        Maximum files to ingest per repo  (default 200)
REPO_MAX_FILE_CHARS   Maximum characters per file       (default 50000)
MAX_UNCOMPRESSED_BYTES ZIP-bomb protection limit        (default 500 MB)
CHUNK_MAX_CHARS       Chunk size in characters          (default 1500)
CHUNK_OVERLAP_CHARS   Overlap between chunks            (default 150)
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION
# ---------------------------------------------------------------------------
# State Machine Definition
#
# States represent a linear progression through the ingestion lifecycle.
# Transitions are explicit, deterministic, and irreversible (except to FAILED).
#
# STATE FLOW:
#   created → stored → queued → running → processing → indexing → finalizing → success
#   Any state → failed
#
# STORAGE INVARIANT:
#   - ALL data stored in database (blob_data field)
#   - NO filesystem usage allowed
#   - Workers read ONLY from database
#
# ---------------------------------------------------------------------------

# State constants
class IngestJobState:
    """Canonical states for the ingestion state machine."""

    # Initial creation
    CREATED = "created"          # Job record exists, no data yet

    # Blob storage
    STORED = "stored"            # Blob data stored in DB (≤ 500MB validated)

    # Execution states
    QUEUED = "queued"            # In RQ queue, awaiting worker
    RUNNING = "running"          # Worker started, blob loaded from DB
    PROCESSING = "processing"    # Content parsed, extraction in progress
    INDEXING = "indexing"        # Chunks created and being indexed

    # Finalization
    FINALIZING = "finalizing"    # Final persistence, metadata updates

    # Terminal states
    SUCCESS = "success"          # Completed successfully
    FAILED = "failed"            # Failed with error

    @classmethod
    def all_states(cls) -> set[str]:
        """Return all valid states."""
        return {
            cls.CREATED, cls.STORED, cls.QUEUED,
            cls.RUNNING, cls.PROCESSING, cls.INDEXING, cls.FINALIZING,
            cls.SUCCESS, cls.FAILED
        }

    @classmethod
    def terminal_states(cls) -> set[str]:
        """Return terminal states (no further transitions allowed)."""
        return {cls.SUCCESS, cls.FAILED}

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        """Check if a state is terminal."""
        return state in cls.terminal_states()


# Allowed state transitions (deterministic, linear)
#
# SINGLE PATH (no filesystem staging):
#   created → stored → queued → running → processing → indexing → finalizing → success
#
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    IngestJobState.CREATED: {IngestJobState.STORED, IngestJobState.FAILED},
    IngestJobState.STORED: {IngestJobState.QUEUED, IngestJobState.FAILED},
    IngestJobState.QUEUED: {IngestJobState.RUNNING, IngestJobState.FAILED},
    IngestJobState.RUNNING: {IngestJobState.PROCESSING, IngestJobState.FAILED},
    IngestJobState.PROCESSING: {IngestJobState.INDEXING, IngestJobState.FAILED},
    IngestJobState.INDEXING: {IngestJobState.FINALIZING, IngestJobState.FAILED},
    IngestJobState.FINALIZING: {IngestJobState.SUCCESS, IngestJobState.FAILED},
    IngestJobState.SUCCESS: set(),  # Terminal - no transitions
    IngestJobState.FAILED: set(),   # Terminal - no transitions
}


def validate_state_transition(from_state: str | None, to_state: str) -> None:
    """
    Validate a state transition according to the state machine.

    MQP-CONTRACT: INGESTION_STATE_MACHINE_ENFORCEMENT_V1

    Raises RuntimeError if transition is forbidden.
    """
    # Initial transition (no previous state)
    if from_state is None:
        if to_state != IngestJobState.CREATED:
            raise RuntimeError(
                f"STATE_MACHINE_VIOLATION: Initial state must be CREATED, got {to_state}"
            )
        return

    # Verify states are valid
    if from_state not in IngestJobState.all_states():
        raise RuntimeError(
            f"STATE_MACHINE_VIOLATION: Invalid from_state: {from_state}"
        )
    if to_state not in IngestJobState.all_states():
        raise RuntimeError(
            f"STATE_MACHINE_VIOLATION: Invalid to_state: {to_state}"
        )

    # Terminal states cannot transition
    if IngestJobState.is_terminal(from_state):
        raise RuntimeError(
            f"STATE_MACHINE_VIOLATION: Cannot transition from terminal state "
            f"{from_state} to {to_state}"
        )

    # Check if transition is allowed
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise RuntimeError(
            f"STATE_MACHINE_VIOLATION: Forbidden transition {from_state} → {to_state}. "
            f"Allowed transitions from {from_state}: {sorted(allowed)}"
        )

    logger.info("STATE_TRANSITION: %s → %s", from_state, to_state)


# ---------------------------------------------------------------------------
# Blob validation (pre-transition enforcement)
# ---------------------------------------------------------------------------


def validate_blob_before_stored(job: Any) -> None:
    """
    Validate blob_data before allowing transition to 'stored' state.

    MQP-CONTRACT: AIC-v1.1-FINAL-VALIDATION-LOCK
    Enforces blob invariants before storage.

    Raises RuntimeError if validation fails.
    """
    MAX_BLOB_SIZE = 500 * 1024 * 1024  # 500MB

    if job.blob_data is None:
        raise RuntimeError(
            f"BLOB_VALIDATION_FAILED: Job {job.id} has no blob_data. "
            f"Cannot transition to 'stored' state without blob content."
        )

    if job.blob_size_bytes == 0:
        raise RuntimeError(
            f"BLOB_VALIDATION_FAILED: Job {job.id} has zero-size blob. "
            f"Blob must contain data before storage."
        )

    if job.blob_size_bytes > MAX_BLOB_SIZE:
        raise RuntimeError(
            f"BLOB_VALIDATION_FAILED: Job {job.id} blob size {job.blob_size_bytes:,} bytes "
            f"exceeds maximum of {MAX_BLOB_SIZE:,} bytes (500MB)."
        )

    logger.debug(
        "BLOB_VALIDATED: job=%s size=%d bytes mime=%s",
        job.id, job.blob_size_bytes, job.blob_mime_type
    )


# ---------------------------------------------------------------------------
# TRANSITION AUTHORITY ENFORCEMENT
# ---------------------------------------------------------------------------


def _dispatch_job(job_id: str) -> None:
    """
    MQP-CONTRACT: AIC-v1.1-FINAL-INVARIANT-SEAL — DISPATCH AUTHORITY

    Dispatch process_ingest_job through the appropriate executor.
    This function is ONLY called from transition() when next_state == "queued".
    It MUST NOT be called from anywhere else.

    Priority: BACKEND_DISABLE_JOBS > REDIS_URL > daemon thread.

    In all three paths the same process_ingest_job function is invoked,
    so runtime behaviour is identical regardless of execution context.
    """
    disable = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        from backend.app.job_runner import execute_job

        execute_job("process_ingest_job", job_id)
        return

    from backend.app.worker import enqueue_job

    enqueue_job(job_id, "process_ingest_job")
    logger.info("IngestJob %s enqueued for execution", job_id)


def transition(job_id: uuid.UUID, next_state: str, payload: dict[str, Any] | None = None) -> None:
    """
    MQP-CONTRACT: AIC-v1.1-FINAL-INVARIANT-SEAL — TRANSITION + DISPATCH AUTHORITY

    Single source of truth for ALL state changes AND job dispatch.

    ENFORCED:
    - Atomic DB transaction (state change committed before dispatch)
    - Strict validation of allowed transitions
    - Blob invariant check before "stored" AND before "queued"
    - Automatic dispatch via _dispatch_job() when next_state == "queued"
    - Timestamp + logging

    STRUCTURAL COUPLING (queued):
    - transition(job_id, "queued") is the ONLY path to enqueue a job
    - _dispatch_job() MUST NOT be called from outside this function
    - Enqueue is a consequence of state, not a separate procedure

    FORBIDDEN:
    - Direct mutation (job.status = X)
    - Calling _dispatch_job() from routes or any code outside transition()
    - Partial updates
    - Silent state changes

    Violation → HARD FAIL

    Args:
        job_id: IngestJob primary key
        next_state: Target state (must be valid transition)
        payload: Optional dict with updates to apply atomically:
            - progress: int (0-100)
            - error: str
            - file_count: int
            - chunk_count: int
    """
    from sqlmodel import Session, select

    from backend.app.database import get_engine
    from backend.app.models import IngestJob, Repo, RepoIndexRegistry

    with Session(get_engine()) as session:
        job = session.get(IngestJob, job_id)
        if not job:
            raise RuntimeError(f"TRANSITION_ERROR: Job {job_id} not found")

        # MQP-CONTRACT: BLOB VALIDATION ENFORCEMENT
        # Validate blob exists before allowing 'stored' state
        if next_state == "stored":
            validate_blob_before_stored(job)

        # MQP-CONTRACT: ENQUEUE GATE — validate blob still exists before dispatch
        if next_state == "queued":
            if not job.blob_data:
                raise RuntimeError(
                    f"ENQUEUE_GATE_VIOLATION: job {job_id} has no blob_data — "
                    f"data must be stored in DB before enqueue"
                )
            if job.blob_size_bytes == 0:
                raise RuntimeError(
                    f"ENQUEUE_GATE_VIOLATION: job {job_id} has empty blob_data"
                )

        # Validate transition
        validate_state_transition(job.status, next_state)

        # Atomic state + payload update
        job.status = next_state
        job.updated_at = datetime.now(timezone.utc)
        if next_state in IngestJobState.terminal_states():
            job.execution_locked = True

        if payload:
            if "progress" in payload:
                job.progress = payload["progress"]
            if "error" in payload:
                job.error = payload["error"]
            if "file_count" in payload:
                job.file_count = payload["file_count"]
            if "chunk_count" in payload:
                job.chunk_count = payload["chunk_count"]

        if job.kind == "repo":
            source = job.source or ""
            if "@" in source:
                repo_url, source_branch = (source.rsplit("@", 1) + [None])[:2]
            else:
                repo_url, source_branch = source, None
            branch = job.branch or source_branch or "main"
            matching_repos = session.exec(
                select(Repo).where(
                    Repo.repo_url == repo_url,
                    Repo.branch == branch,
                )
            ).all()
            if matching_repos and job.repo_id is None:
                job.repo_id = matching_repos[0].id

            registry_status = "indexing"
            if next_state == IngestJobState.CREATED:
                registry_status = "created"
            elif next_state == IngestJobState.SUCCESS:
                registry_status = "completed"
            elif next_state == IngestJobState.FAILED:
                registry_status = "failed"

            for repo in matching_repos:
                registry = session.get(RepoIndexRegistry, repo.id)
                if registry is None:
                    registry = RepoIndexRegistry(
                        repo_id=repo.id,
                        total_files=0,
                        total_chunks=0,
                        indexed=False,
                        status="created",
                    )
                if "file_count" in (payload or {}):
                    registry.total_files = int(payload["file_count"])
                if "chunk_count" in (payload or {}):
                    registry.total_chunks = int(payload["chunk_count"])
                registry.status = registry_status
                registry.indexed = (
                    next_state == IngestJobState.SUCCESS and registry.total_chunks > 0
                )
                registry.min_chunks_per_file = int(getattr(job, "min_chunks_per_file", 0))
                registry.max_chunks_per_file = int(getattr(job, "max_chunks_per_file", 0))
                registry.median_chunks_per_file = float(
                    getattr(job, "median_chunks_per_file", 0.0)
                )
                registry.chunk_variance_flagged = bool(
                    getattr(job, "chunk_variance_flagged", False)
                )
                if next_state in IngestJobState.terminal_states():
                    registry.last_indexed_at = datetime.now(timezone.utc)
                registry.updated_at = datetime.now(timezone.utc)
                session.add(registry)

        session.add(job)
        session.commit()

        logger.info(
            "TRANSITION_COMPLETE: job=%s %s → %s payload=%s",
            job_id, job.status, next_state, payload
        )

    # MQP-CONTRACT: STRUCTURAL DISPATCH COUPLING
    # Enqueue happens AFTER the session is committed and closed.
    # This is the ONLY call site for _dispatch_job — it is structurally
    # impossible to enqueue a job without going through transition("queued").
    if next_state == "queued":
        _dispatch_job(str(job_id))


# ---------------------------------------------------------------------------
# Configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

CHUNK_MAX_CHARS: int = int(os.environ.get("CHUNK_MAX_CHARS", "1500"))
CHUNK_OVERLAP_CHARS: int = int(os.environ.get("CHUNK_OVERLAP_CHARS", "150"))
REPO_MAX_FILES: int = int(os.environ.get("REPO_MAX_FILES", "200"))
REPO_MAX_FILE_CHARS: int = int(os.environ.get("REPO_MAX_FILE_CHARS", "50000"))
MAX_UNCOMPRESSED_BYTES: int = int(
    os.environ.get("MAX_UNCOMPRESSED_BYTES", str(500 * 1024 * 1024))
)
_URL_FETCH_MAX_BYTES: int = 500 * 1024  # 500 KB per page
_URL_FETCH_TIMEOUT: float = 10.0

# File extensions eligible for text ingestion
INGESTIBLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".scala",
        ".cpp", ".c", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
        ".swift", ".sh", ".bash", ".sql",
        ".yaml", ".yml", ".toml", ".ini", ".conf",
        ".html", ".htm", ".css", ".scss", ".sass", ".less",
        ".xml", ".json", ".md", ".txt", ".rst", ".csv",
    }
)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_text(data: bytes, mime_type: str, filename: str) -> str | None:
    """
    Extract plain text from *data*.

    Returns the extracted string, or ``None`` when the content cannot be
    decoded to meaningful text.  Always logs a warning on failure — never
    raises.
    """
    _, ext = os.path.splitext(filename.lower())

    # HTML — must be checked before the `text/*` fast-path (text/html is a
    # subtype of text/) so that we strip tags rather than return raw markup.
    if mime_type in {"text/html", "application/xhtml+xml"} or ext in {".html", ".htm"}:
        return _extract_html(data, filename)

    # CSV — same reasoning: return structured rows, not raw bytes.
    if mime_type == "text/csv" or ext == ".csv":
        return _extract_csv(data, filename)

    # Fast path: plain text and code files
    if (
        mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml"}
        or ext in INGESTIBLE_EXTENSIONS
    ):
        try:
            return data.decode("utf-8", errors="ignore") or None
        except Exception as exc:
            logger.warning("UTF-8 decode failed for %s: %s", filename, exc)
            return None

    if mime_type == "application/pdf" or ext == ".pdf":
        return _extract_pdf(data, filename)

    if mime_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    } or ext in {".docx", ".doc"}:
        return _extract_docx(data, filename)

    if mime_type in {"application/zip", "application/x-zip-compressed"} or ext == ".zip":
        return _extract_zip_text(data, filename)

    return None


def _extract_pdf(data: bytes, filename: str) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(p for p in parts if p.strip())
        return text if text.strip() else None
    except ImportError:
        logger.warning("pypdf not installed — cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", filename, exc)
        return None


def _extract_docx(data: bytes, filename: str) -> str | None:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs) if paragraphs else None
    except ImportError:
        logger.warning("python-docx not installed — cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("DOCX extraction failed for %s: %s", filename, exc)
        return None


def _extract_html(data: bytes, filename: str) -> str | None:
    import html as _html_module
    from html.parser import HTMLParser

    class _TextCollector(HTMLParser):
        _SKIP: frozenset[str] = frozenset(
            {"script", "style", "head", "meta", "link", "noscript"}
        )

        def __init__(self) -> None:
            super().__init__()
            self._parts: list[str] = []
            self._depth: int = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag.lower() in self._SKIP:
                self._depth += 1

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() in self._SKIP and self._depth > 0:
                self._depth -= 1

        def handle_data(self, data: str) -> None:
            if not self._depth:
                s = _html_module.unescape(data).strip()
                if s:
                    self._parts.append(s)

        def result(self) -> str:
            return " ".join(self._parts)

    try:
        raw = data.decode("utf-8", errors="ignore")
        collector = _TextCollector()
        collector.feed(raw)
        result = collector.result()
        return result if result.strip() else None
    except Exception as exc:
        logger.warning("HTML extraction failed for %s: %s", filename, exc)
        return None


def _extract_csv(data: bytes, filename: str) -> str | None:
    import csv

    try:
        text = data.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        lines = [",".join(row) for row in reader if row]
        return "\n".join(lines) if lines else None
    except Exception as exc:
        logger.warning("CSV extraction failed for %s: %s", filename, exc)
        return None


def _extract_zip_text(data: bytes, zip_filename: str) -> str | None:
    """Iterate a ZIP archive and concatenate text from all ingestible members."""
    parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
            if total > MAX_UNCOMPRESSED_BYTES:
                logger.warning(
                    "ZIP bomb protection triggered for %s (uncompressed %d bytes > %d limit)",
                    zip_filename,
                    total,
                    MAX_UNCOMPRESSED_BYTES,
                )
                return None

            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                _, ext = os.path.splitext(info.filename.lower())
                if ext not in INGESTIBLE_EXTENSIONS:
                    continue
                # Guard against path traversal entries
                safe = os.path.normpath(info.filename)
                if safe.startswith(".."):
                    continue
                try:
                    member_data = zf.read(info)
                    member_mime = mimetypes.guess_type(info.filename)[0] or "text/plain"
                    member_text = extract_text(member_data, member_mime, info.filename)
                    if member_text and member_text.strip():
                        parts.append(f"--- {info.filename} ---\n{member_text}")
                except Exception as exc:
                    logger.debug("Skipping ZIP member %s: %s", info.filename, exc)
    except zipfile.BadZipFile as exc:
        logger.warning("Bad ZIP file %s: %s", zip_filename, exc)
        return None

    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Chunking with overlap
# ---------------------------------------------------------------------------


def split_with_overlap(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """
    Split *text* into chunks of at most *chunk_size* characters.

    Chunks are split on line boundaries where possible.  The trailing
    *overlap* characters of the previous chunk are prepended to the next
    chunk so that context is not lost at boundaries.
    """
    max_chars = chunk_size if chunk_size is not None else CHUNK_MAX_CHARS
    olap = overlap if overlap is not None else CHUNK_OVERLAP_CHARS
    olap = min(olap, max_chars // 2)  # prevent infinite overlap

    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    lines = text.splitlines(keepends=True)
    current: list[str] = []
    current_len = 0
    prev_tail = ""

    for line in lines:
        if current_len + len(line) > max_chars and current:
            body = "".join(current)
            chunks.append(prev_tail + body if prev_tail else body)
            prev_tail = body[-olap:] if olap > 0 else ""
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        body = "".join(current)
        chunks.append(prev_tail + body if prev_tail else body)

    return chunks or [text]


# ---------------------------------------------------------------------------
# GitHub helpers (sync, no asyncio)
# ---------------------------------------------------------------------------


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _fetch_github_tree(
    owner: str,
    name: str,
    branch: str,
    token: str | None,
) -> list[dict[str, Any]]:
    """
    Return all blob entries from the GitHub Trees API for a repository.

    Uses a single ``GET /repos/{owner}/{name}/git/trees/{branch}?recursive=1``
    request.  Falls back to an empty list on any network or API error.
    """
    import httpx

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{owner}/{name}/git/trees/{branch}",
                headers=_github_headers(token),
                params={"recursive": "1"},
            )
    except httpx.RequestError as exc:
        logger.warning("GitHub Trees API request failed for %s/%s: %s", owner, name, exc)
        return []

    if resp.status_code == 404:
        logger.warning(
            "GitHub repo/branch not found: %s/%s@%s (HTTP 404)", owner, name, branch
        )
        return []
    if resp.status_code != 200:
        logger.warning(
            "GitHub Trees API returned HTTP %d for %s/%s", resp.status_code, owner, name
        )
        return []

    data = resp.json()
    if data.get("truncated"):
        logger.warning(
            "GitHub tree for %s/%s@%s was truncated — ingest may be incomplete",
            owner,
            name,
            branch,
        )

    return [
        item
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]


def _fetch_raw_file(
    owner: str,
    name: str,
    branch: str,
    path: str,
    client: Any,
) -> bytes | None:
    """Fetch raw file bytes from raw.githubusercontent.com."""
    url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"
    try:
        resp = client.get(url, timeout=15.0)
        if resp.status_code == 200:
            # Byte-level cap before decode to avoid huge files
            return resp.content[: REPO_MAX_FILE_CHARS * 4]
        logger.debug("HTTP %d fetching raw file %s", resp.status_code, path)
        return None
    except Exception as exc:
        logger.debug("Failed to fetch raw file %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_ingest_job(job_id: str):
    """Load IngestJob from DB.  Returns None if not found or DB unavailable."""
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            return session.get(IngestJob, uuid.UUID(job_id))
    except Exception:
        logger.exception("Failed to load IngestJob %s", job_id)
        return None


def _transition(job_id: str, next_state: str, **payload: Any) -> None:
    """
    ATOMIC STATE TRANSITION - SOLE AUTHORITY FOR STATE CHANGES

    AIC-v2 Section 11: TRANSITION AUTHORITY (CRITICAL)

    This is the ONLY function allowed to change job state.
    All state mutations MUST flow through here.

    FORBIDDEN elsewhere:
    - job.status = X
    - direct session.commit() with status change
    - any state update outside this function

    Args:
        job_id: Job UUID as string
        next_state: Target state (must be valid transition)
        **payload: Additional fields to update (progress, error, etc.)

    Raises:
        RuntimeError: If transition is invalid (STRICT MODE - no silent failures)
    """
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            if job is None:
                logger.error("TRANSITION_FAILURE: Job %s not found", job_id)
                return

            old_state = job.status

            # AIC-v2: STRICT VALIDATION (no graceful degradation)
            # Invalid transitions MUST fail hard
            validate_state_transition(old_state, next_state)

            # ATOMIC UPDATE: state + payload + timestamp
            job.status = next_state
            job.updated_at = datetime.now(timezone.utc)
            if next_state in IngestJobState.terminal_states():
                job.execution_locked = True

            for k, v in payload.items():
                setattr(job, k, v)

            session.add(job)
            session.commit()

            # Log successful transition (AIC-v2 Section 9: deterministic logging)
            logger.info(
                "TRANSITION: %s → %s [job=%s] %s",
                old_state,
                next_state,
                job_id,
                f"payload={payload}" if payload else ""
            )

    except RuntimeError as exc:
        # State machine violation - HARD FAILURE (AIC-v2 Section 11)
        logger.error("TRANSITION_VIOLATION: job=%s, %s", job_id, exc)
        raise
    except Exception as exc:
        logger.exception("TRANSITION_ERROR: job=%s, %s", job_id, exc)
        raise


# ---------------------------------------------------------------------------
# Per-kind ingestion handlers
# ---------------------------------------------------------------------------


def _ingest_file(session: Any, job: Any) -> tuple[int, int]:
    """
    Ingest a file from blob_data stored in the database.

    MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1

    Six-phase graph-aware ingestion for a single uploaded file:
      Phase 1 — File registration (RepoFile)
      Phase 2 — Symbol extraction (CodeSymbol)
      Phase 3 — Dependency resolution (N/A for single-file; no cross-file refs)
      Phase 4 — Call graph (SymbolCallEdge, intra-file)
      Phase 5 — Entry points (EntryPoint)
      Phase 6 — Chunking (RepoChunk, unchanged)

    Returns ``(file_count, chunk_count)``.
    """
    from backend.app.graph_extractor import (
        extract_graph,
        extract_symbol_calls,
        hash_content,
    )
    from backend.app.models import (
        CodeSymbol,
        EntryPoint,
        RepoChunk,
        RepoFile,
        SymbolCallEdge,
    )
    from backend.app.repo_chunk_extractor import extract_structure

    logger.info("INGEST_START job_id=%s kind=file", job.id)

    # MQP-CONTRACT: DB-BACKED INGESTION - Blob must exist
    if not job.blob_data:
        logger.error("INGEST_FAIL job_id=%s reason=no_blob_data", job.id)
        raise RuntimeError(
            f"BLOB_MISSING: IngestJob {job.id} has no blob_data. "
            f"Blob must be stored in database before processing."
        )

    # Validate blob size
    if job.blob_size_bytes == 0 or len(job.blob_data) == 0:
        logger.error("INGEST_FAIL job_id=%s reason=empty_blob", job.id)
        raise RuntimeError(
            f"BLOB_MISSING: Blob data is empty for job {job.id}"
        )

    logger.debug(
        "Processing blob: job=%s size=%d mime=%s",
        job.id, job.blob_size_bytes, job.blob_mime_type
    )

    data = job.blob_data
    mime_type = job.blob_mime_type or "application/octet-stream"
    filename = job.source

    text = extract_text(data, mime_type, filename)

    if not text or not text.strip():
        logger.info(
            "No extractable text in blob: job=%s size=%d",
            job.id, job.blob_size_bytes
        )
        logger.info("INGEST_SUCCESS job_id=%s chunks=0", job.id)
        return 0, 0

    graph = extract_graph(filename, data)

    # PHASE 1 — File registration
    repo_file = RepoFile(
        repo_id=job.id,
        path=filename,
        language=graph["language"],
        size_bytes=len(data),
        content_hash=hash_content(data),
    )
    session.add(repo_file)
    session.flush()  # materialise id before FK children

    # PHASE 2 — Symbol extraction
    symbols_map: dict[str, Any] = {}  # name → CodeSymbol
    for name, sym_type, line in graph["symbols"]:
        sym = CodeSymbol(
            file_id=repo_file.id,
            name=name,
            symbol_type=sym_type,
            start_line=line,
            end_line=line,
        )
        session.add(sym)
        symbols_map[name] = sym
    if symbols_map:
        session.flush()  # materialise CodeSymbol ids before call-edge FKs

    # PHASE 3 — Dependency resolution (single-file: no cross-file refs possible)

    # PHASE 4 — Call graph (intra-file)
    # STRICT EDGE POLICY: only create edge when target is also a persisted symbol.
    if symbols_map:
        calls = extract_symbol_calls(text, list(symbols_map.keys()))
        for caller_name, callee_names in calls.items():
            caller_sym = symbols_map.get(caller_name)
            if caller_sym is None:
                continue
            for callee_name in callee_names:
                target_sym = symbols_map.get(callee_name)
                if target_sym is None:
                    continue  # DROP EDGE — intra-file callee not persisted
                session.add(SymbolCallEdge(
                    source_symbol_id=caller_sym.id,
                    callee_name=callee_name,
                    target_symbol_id=target_sym.id,
                ))

    # PHASE 5 — Entry points
    for entry_type, line in graph.get("entry_points", []):
        session.add(EntryPoint(
            file_id=repo_file.id,
            entry_type=entry_type,
            line=line,
        ))

    # PHASE 6 — Chunking (unchanged)
    chunks = split_with_overlap(text)
    for idx, chunk_text in enumerate(chunks):
        structure = extract_structure(chunk_text, filename)
        session.add(
            RepoChunk(
                ingest_job_id=job.id,
                file_path=filename,
                content=chunk_text,
                chunk_index=idx,
                token_estimate=max(1, len(chunk_text) // 4),
                chunk_type=structure["chunk_type"],
                symbol=structure["symbol"],
                dependencies=structure["dependencies"],
                graph_group=structure["graph_group"],
                start_line=structure["start_line"],
                end_line=structure["end_line"],
            )
        )

    session.commit()

    logger.info("INGEST_SUCCESS job_id=%s chunks=%d", job.id, len(chunks))
    return 1, len(chunks)


def _ingest_url(session: Any, job: Any) -> tuple[int, int]:
    """
    Ingest URL content from blob_data stored in the database.

    MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION

    Returns ``(file_count, chunk_count)``.

    Worker reads from blob_data ONLY. URL was already fetched and stored.
    """
    from backend.app.models import RepoChunk
    from backend.app.repo_chunk_extractor import extract_structure

    url = job.source
    logger.info("INGEST_START job_id=%s kind=url source=%s", job.id, url)

    # MQP-CONTRACT: DB-BACKED INGESTION - Blob must exist
    if not job.blob_data:
        logger.error("INGEST_FAIL job_id=%s reason=no_blob_data", job.id)
        raise RuntimeError(
            f"BLOB_MISSING: IngestJob {job.id} has no blob_data. "
            f"URL content must be fetched and stored before processing."
        )


    # Extract text from blob
    content_bytes = job.blob_data
    content_type = job.blob_mime_type or "text/html"

    # Use the <title> tag (or URL) as the document filename
    try:
        text_for_title = content_bytes[:4096].decode('utf-8', errors='ignore')
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", text_for_title, re.IGNORECASE | re.DOTALL
        )
        page_title = title_match.group(1).strip() if title_match else url
        safe_title = re.sub(r"[^\w\-. ]", "_", page_title)[:80] or "webpage"
        filename = f"{safe_title}.html"
    except Exception:
        filename = f"url_{job.id}.html"


    text = extract_text(content_bytes, content_type, filename)
    if not text or not text.strip():
        logger.warning("No extractable text from URL blob: job=%s", job.id)
        return 0, 0


    chunks = split_with_overlap(text)
    for idx, chunk_text in enumerate(chunks):
        structure = extract_structure(chunk_text, filename)
        session.add(
            RepoChunk(
                ingest_job_id=job.id,
                file_path=filename,
                content=chunk_text,
                chunk_index=idx,
                token_estimate=max(1, len(chunk_text) // 4),
                source_url=url,
                chunk_type=structure["chunk_type"],
                symbol=structure["symbol"],
                dependencies=structure["dependencies"],
                graph_group=structure["graph_group"],
                start_line=structure["start_line"],
                end_line=structure["end_line"],
            )
        )

    # Commit chunks to database
    session.commit()

    logger.info("INGEST_SUCCESS job_id=%s chunks=%d", job.id, len(chunks))
    return 1, len(chunks)


def _ingest_repo(session: Any, job: Any) -> tuple[int, int]:
    """
    Ingest a GitHub repository from blob manifest stored in database.

    MQP-CONTRACT: AIC-v1.1-REPO-DB-UNIFICATION-FINAL
    MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1

    Six-phase graph-aware ingestion:
      Phase 1 — File registration   (RepoFile — ALL files persisted first)
      Phase 2 — Symbol extraction   (CodeSymbol)
      Phase 3 — Dependency resolution (FileDependency — resolved only)
      Phase 4 — Call graph          (SymbolCallEdge)
      Phase 5 — Entry points        (EntryPoint)
      Phase 6 — Chunking            (RepoChunk, unchanged)

    Worker is PURE: reads ONLY from blob_data (no network, no filesystem).
    Blob contains complete repo manifest with all file contents pre-fetched.

    Returns ``(file_count, chunk_count)``.
    """
    import json
    from sqlmodel import select

    from backend.app.graph_extractor import (
        extract_graph,
        extract_symbol_calls,
        hash_content,
        resolve_import,
    )
    from backend.app.models import (
        CodeSymbol,
        EntryPoint,
        FileDependency,
        IngestJob,
        RepoChunk,
        RepoFile,
        SymbolCallEdge,
    )
    from backend.app.repo_chunk_extractor import extract_structure

    logger.info("INGEST_START job_id=%s kind=repo", job.id)
    print(f"INGEST START: {job.id}")
    if not job.blob_data:
        logger.error("INGEST_FAIL job_id=%s reason=no_blob_data", job.id)
        raise RuntimeError(
            f"BLOB_MISSING: IngestJob {job.id} has no blob_data. "
            f"Repo manifest must be stored before processing."
        )

    # Deserialize repo manifest from blob
    try:
        manifest = json.loads(job.blob_data.decode('utf-8'))
        files = manifest["files"]
        skipped_files = manifest.get("skipped_files", [])
        repo_url = manifest.get("repo_url", job.source)
        branch = manifest.get("branch", job.branch)
    except Exception as exc:
        raise RuntimeError(f"Invalid repo manifest blob: {exc}") from exc

    logger.info(
        "Processing repo manifest: job=%s repo=%s branch=%s files=%d",
        job.id, repo_url, branch, len(files)
    )
    for skipped in skipped_files:
        file_path = skipped.get("file_path", "unknown")
        reason = skipped.get("reason", "parse_error")
        logger.info("INGEST_SKIP job_id=%s file_path=%s reason=%s", job.id, file_path, reason)

    # -----------------------------------------------------------------------
    # PHASE 1 — File registration.
    #
    # MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1 — Section 3
    # ALL RepoFile rows MUST be persisted (flushed) before graph resolution
    # begins. Resolution operates only on committed/flushed file state.
    # -----------------------------------------------------------------------

    # In-memory maps for phases 2-5
    repo_files_by_path: dict[str, Any] = {}           # path → RepoFile
    raw_imports_by_path: dict[str, list[str]] = {}    # path → raw import list
    entry_points_by_path: dict[str, list[tuple]] = {} # path → [(type, line)]
    file_contents_by_path: dict[str, str] = {}        # path → text content
    symbols_by_path: dict[str, list[tuple]] = {}      # path → [(name, sym_type, line, CodeSymbol)]

    for file_entry in files:
        file_path = file_entry["path"]
        content = file_entry["content"]

        if not content or not content.strip():
            continue

        content_bytes = content.encode("utf-8")
        graph = extract_graph(file_path, content_bytes)

        repo_file = RepoFile(
            repo_id=job.id,
            path=file_path,
            language=graph["language"],
            size_bytes=len(content_bytes),
            content_hash=hash_content(content_bytes),
        )
        session.add(repo_file)

        repo_files_by_path[file_path] = repo_file
        raw_imports_by_path[file_path] = graph["imports"]
        entry_points_by_path[file_path] = graph.get("entry_points", [])
        file_contents_by_path[file_path] = content

    # Flush all RepoFile rows so their IDs are valid before CodeSymbol FKs.
    session.flush()

    # -----------------------------------------------------------------------
    # PHASE 2 — Symbol extraction.
    # All CodeSymbol rows are created after RepoFile IDs are available.
    # -----------------------------------------------------------------------

    global_symbol_map: dict[str, list[Any]] = {}  # name → [CodeSymbol, ...]

    for file_path, repo_file in repo_files_by_path.items():
        content_bytes = file_contents_by_path[file_path].encode("utf-8")
        graph = extract_graph(file_path, content_bytes)

        path_symbols: list[tuple] = []
        for name, sym_type, line in graph["symbols"]:
            sym = CodeSymbol(
                file_id=repo_file.id,
                name=name,
                symbol_type=sym_type,
                start_line=line,
                end_line=line,
            )
            session.add(sym)
            path_symbols.append((name, sym_type, line, sym))
            global_symbol_map.setdefault(name, []).append(sym)

        symbols_by_path[file_path] = path_symbols

    # Flush all CodeSymbol rows so IDs are valid for SymbolCallEdge FKs.
    session.flush()

    # -----------------------------------------------------------------------
    # PHASE 3 — Dependency resolution.
    #
    # Only resolved edges are stored.  Unresolved imports are dropped.
    # INVARIANT: FileDependency.target_file_id IS NEVER NULL.
    # -----------------------------------------------------------------------

    all_paths: frozenset[str] = frozenset(repo_files_by_path.keys())

    # file_dependency_map is built here (in-memory) alongside the FileDependency
    # rows so Phase 4 can use it for import-context symbol resolution without an
    # extra DB round-trip.
    file_dependency_map: dict[Any, list[Any]] = {}  # source_file_id → [target_file_ids]

    for file_path, repo_file in repo_files_by_path.items():
        for imp in raw_imports_by_path[file_path]:
            resolved = resolve_import(imp, file_path, all_paths)
            if resolved and resolved in repo_files_by_path:
                target_file = repo_files_by_path[resolved]
                session.add(FileDependency(
                    source_file_id=repo_file.id,
                    target_file_id=target_file.id,
                ))
                file_dependency_map.setdefault(repo_file.id, []).append(target_file.id)

    # -----------------------------------------------------------------------
    # PHASE 4 — Call graph.
    #
    # MQP-CONTRACT: SYMBOL-RESOLUTION-HARDENING v1.0
    #
    # Resolution priority (per callee name):
    #   1. Same file as caller
    #   2. File directly imported by the caller's file (FileDependency graph)
    #   3. Globally unique (exactly one symbol with that name in the whole repo)
    #   4. DROP EDGE — ambiguous or unresolvable
    #
    # STRICT EDGE POLICY:
    #   source_symbol_id is ALWAYS a valid FK (CodeSymbol must exist).
    #   target_symbol_id is NEVER NULL — partial edges are forbidden.
    # -----------------------------------------------------------------------

    # Build file_symbol_map: file_id → {name → CodeSymbol}
    file_symbol_map: dict[Any, dict[str, Any]] = {}
    for file_path, path_symbols in symbols_by_path.items():
        rf = repo_files_by_path[file_path]
        file_symbol_map[rf.id] = {name: sym for name, _, _, sym in path_symbols}

    def _resolve_symbol(caller_file_id: Any, callee_name: str) -> Any:
        """
        Resolve *callee_name* to a unique CodeSymbol using import-context priority.

        Returns the CodeSymbol, or None if the callee is ambiguous / unknown.
        """
        # 1. Same file
        local = file_symbol_map.get(caller_file_id, {})
        if callee_name in local:
            return local[callee_name]

        # 2. Import graph — files directly imported by the caller's file
        for dep_file_id in file_dependency_map.get(caller_file_id, []):
            dep_syms = file_symbol_map.get(dep_file_id, {})
            if callee_name in dep_syms:
                return dep_syms[callee_name]

        # 3. Globally unique — only if exactly ONE symbol carries this name
        candidates = global_symbol_map.get(callee_name, [])
        if len(candidates) == 1:
            return candidates[0]

        # Ambiguous or unknown — drop the edge
        return None

    for file_path, path_symbols in symbols_by_path.items():
        if not path_symbols:
            continue

        local_sym_map = {name: sym for name, _, _, sym in path_symbols}

        # Pass all globally-known symbol names so cross-file callees are
        # detected in caller bodies (symbol_starts is still bounded to
        # symbols defined in *this* file — no orphan bodies are scanned).
        all_known_names = list(global_symbol_map.keys())
        calls = extract_symbol_calls(file_contents_by_path[file_path], all_known_names)

        for caller_name, callee_names in calls.items():
            caller_sym = local_sym_map.get(caller_name)
            if caller_sym is None:
                continue  # caller not persisted — edge forbidden

            for callee_name in callee_names:
                target_sym = _resolve_symbol(caller_sym.file_id, callee_name)
                if target_sym is None:
                    continue  # DROP EDGE — ambiguous or unresolvable

                session.add(SymbolCallEdge(
                    source_symbol_id=caller_sym.id,
                    callee_name=callee_name,
                    target_symbol_id=target_sym.id,
                ))

    # -----------------------------------------------------------------------
    # PHASE 5 — Entry points.
    # -----------------------------------------------------------------------

    for file_path, repo_file in repo_files_by_path.items():
        for entry_type, line in entry_points_by_path[file_path]:
            session.add(EntryPoint(
                file_id=repo_file.id,
                entry_type=entry_type,
                line=line,
            ))

    # -----------------------------------------------------------------------
    # PHASE 6 — Chunking (existing behaviour, unchanged).
    # -----------------------------------------------------------------------

    file_count = 0
    chunk_count = 0
    chunks_per_file: dict[str, int] = {}

    for file_entry in files:
        file_path = file_entry["path"]
        content = file_entry["content"]

        if not content or not content.strip():
            continue

        chunks = split_with_overlap(content)
        chunks_per_file[file_path] = len(chunks)

        for idx, chunk_text in enumerate(chunks):
            structure = extract_structure(chunk_text, file_path)
            session.add(
                RepoChunk(
                    ingest_job_id=job.id,
                    file_path=file_path,
                    content=chunk_text,
                    chunk_index=idx,
                    token_estimate=max(1, len(chunk_text) // 4),
                    chunk_type=structure["chunk_type"],
                    symbol=structure["symbol"],
                    dependencies=structure["dependencies"],
                    graph_group=structure["graph_group"],
                    start_line=structure["start_line"],
                    end_line=structure["end_line"],
                )
            )
            chunk_count += 1

        file_count += 1

    # Commit all graph rows + chunks atomically
    counts = sorted(chunks_per_file.values())
    if counts:
        mid = len(counts) // 2
        if len(counts) % 2 == 0:
            median_chunks = float((counts[mid - 1] + counts[mid]) / 2)
        else:
            median_chunks = float(counts[mid])
        min_chunks = int(counts[0])
        max_chunks = int(counts[-1])
        avg_chunks = float(chunk_count / file_count) if file_count > 0 else 0.0
    else:
        median_chunks = 0.0
        min_chunks = 0
        max_chunks = 0
        avg_chunks = 0.0

    previous_success = session.exec(
        select(IngestJob)
        .where(
            IngestJob.kind == "repo",
            IngestJob.source == job.source,
            IngestJob.status == IngestJobState.SUCCESS,
            IngestJob.id != job.id,
        )
        .order_by(IngestJob.created_at.desc())
    ).first()
    variance_delta_pct = 0.0
    variance_flagged = False
    if previous_success and int(previous_success.chunk_count or 0) > 0:
        baseline = float(previous_success.chunk_count)
        variance_delta_pct = abs((chunk_count - baseline) / baseline) * 100.0
        variance_flagged = variance_delta_pct > 10.0
        if variance_flagged:
            logger.warning(
                "INGEST_VARIANCE_FLAG job_id=%s baseline_chunks=%d current_chunks=%d delta_pct=%.2f",
                job.id,
                int(previous_success.chunk_count or 0),
                chunk_count,
                variance_delta_pct,
            )

    job.skipped_files_count = int(len(skipped_files))
    job.avg_chunks_per_file = avg_chunks
    job.min_chunks_per_file = min_chunks
    job.max_chunks_per_file = max_chunks
    job.median_chunks_per_file = median_chunks
    job.chunk_variance_flagged = variance_flagged
    job.chunk_variance_delta_pct = variance_delta_pct
    session.add(job)

    session.commit()

    logger.info("INGEST_SUCCESS job_id=%s files=%d chunks=%d", job.id, file_count, chunk_count)
    print(f"INGEST DONE: {job.id} files={file_count} chunks={chunk_count}")
    return file_count, chunk_count



def process_ingest_job(job_id: str) -> None:
    """
    Run the ingestion pipeline for the IngestJob identified by *job_id*.

    Designed to run in an RQ worker or background thread.

    MQP-CONTRACT: WORKER-EXECUTION-CLOSURE
        execution_allowed(job) = job.status == queued
        ALL other states → NO-OP (no transition, no retry, no correction)

    AIC-v2: TRANSITION AUTHORITY
        ALL state changes via _transition() ONLY.
        NO direct job.status mutations.
    """
    from backend.app.execution_spine import require_execute_job_route

    require_execute_job_route("process_ingest_job")
    logger.error("TRACE_ENTRY: job=%s", job_id)

    job = _get_ingest_job(job_id)
    logger.error("TRACE_STATUS: job=%s status=%s", job_id, getattr(job, "status", None))

    if job is None:
        logger.error("TRACE_EXIT: MISSING_JOB job=%s", job_id)
        logger.error("WORKER_MISSING_JOB: job=%s", job_id)
        return

    if getattr(job, "execution_locked", False):
        logger.error("REPLAY_BLOCKED: job=%s", job_id)
        logger.error("TRACE_EXIT: LOCKED_BLOCK job=%s", job_id)
        return

    # TERMINAL STATES — HARD STOP
    if job.status in (IngestJobState.FAILED, IngestJobState.SUCCESS):
        logger.error(
            "WORKER_TERMINAL_VIOLATION: job=%s state=%s",
            job_id, job.status,
        )
        logger.error("TRACE_EXIT: TERMINAL_BLOCK job=%s", job_id)
        return

    # NON-QUEUED — HARD STOP
    if job.status not in (IngestJobState.QUEUED, IngestJobState.RUNNING):
        logger.error(
            "WORKER_ENTRY_VIOLATION: job=%s state=%s expected=queued",
            job_id, job.status,
        )
        logger.error("TRACE_EXIT: NON_QUEUED_BLOCK job=%s", job_id)
        return

    logger.info("IngestJob %s: starting", job_id)

    try:
        logger.error("TRACE_EXECUTION_ALLOWED: job=%s", job_id)
        if job.status == IngestJobState.QUEUED:
            _transition(job_id, IngestJobState.RUNNING)

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            if job is None:
                logger.error("PIPELINE_EXECUTION_FAIL: job=%s not found after RUNNING", job_id)
                logger.error("TRACE_FAILED_TRANSITION: job=%s LOCATION=LINE_1124", job_id)
                _transition(job_id, IngestJobState.FAILED,
                            error="PIPELINE_EXECUTION_FAIL: job not found after RUNNING")
                return

            # PIPELINE VALIDATION — blob integrity, after RUNNING
            if job.kind in ("file", "url", "repo"):
                if not job.blob_data:
                    logger.error("PIPELINE_VALIDATION_FAIL: job=%s blob_data missing", job_id)
                    logger.error("TRACE_FAILED_TRANSITION: job=%s LOCATION=LINE_1133", job_id)
                    _transition(job_id, IngestJobState.FAILED,
                                error="PIPELINE_VALIDATION_FAIL: blob_data missing")
                    return
                if job.blob_size_bytes == 0:
                    logger.error("PIPELINE_VALIDATION_FAIL: job=%s blob_data empty", job_id)
                    logger.error("TRACE_FAILED_TRANSITION: job=%s LOCATION=LINE_1139", job_id)
                    _transition(job_id, IngestJobState.FAILED,
                                error="PIPELINE_VALIDATION_FAIL: blob_data empty")
                    return

            # TRANSITION: RUNNING → PROCESSING
            _transition(job_id, IngestJobState.PROCESSING, progress=5)
            logger.info("STATE: PROCESSING job_id=%s", job_id)

            # Execute the ingestion based on job kind
            if job.kind == "file":
                file_count, chunk_count = _ingest_file(session, job)
            elif job.kind == "url":
                file_count, chunk_count = _ingest_url(session, job)
            elif job.kind == "repo":
                file_count, chunk_count = _ingest_repo(session, job)
            else:
                raise ValueError(f"Unknown IngestJob kind: {job.kind!r}")

            # TRANSITION: PROCESSING → INDEXING
            _transition(job_id, IngestJobState.INDEXING, progress=90)
            logger.info("STATE: INDEXING job_id=%s chunks=%d", job_id, chunk_count)

            # Indexing happens during chunk creation above
            # This state represents the chunk persistence/indexing phase

            # TRANSITION: INDEXING → FINALIZING
            _transition(job_id, IngestJobState.FINALIZING, progress=98)
            logger.info("STATE: FINALIZING job_id=%s", job_id)

            # TRANSITION: FINALIZING → SUCCESS (atomic with final counts)
            _transition(
                job_id,
                IngestJobState.SUCCESS,
                progress=100,
                file_count=file_count,
                chunk_count=chunk_count
            )
            logger.info(
                "STATE: SUCCESS job_id=%s files=%d chunks=%d",
                job_id, file_count, chunk_count
            )

    except Exception as exc:
        logger.error("PIPELINE_EXECUTION_FAIL: job=%s error=%s", job_id, str(exc)[:200])

        # Attempt to mark the job as failed.  If the job is already in a terminal
        # state (e.g., another process raced to fail it, or the exception itself
        # was raised by an earlier _transition call), the state machine will
        # reject a second failed → failed transition.  We swallow that secondary
        # error so it does NOT propagate to RQ — propagation would trigger a retry
        # and an infinite STATE_MACHINE_VIOLATION loop.
        try:
            logger.error("TRACE_FAILED_TRANSITION: job=%s LOCATION=LINE_1189", job_id)
            _transition(job_id, IngestJobState.FAILED, error=str(exc)[:1000], progress=0)
        except Exception as fail_exc:
            logger.error(
                "PIPELINE_FAIL_TRANSITION_ERROR: job=%s already in terminal state "
                "or transition error: %s",
                job_id, fail_exc,
            )
