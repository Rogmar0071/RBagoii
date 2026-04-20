"""
backend.app.ingest_routes
==========================
Clean, unified ingestion API backed by ``ingest_pipeline.process_ingest_job``.

Endpoints
---------
POST   /v1/ingest/file         Upload a file and queue ingestion
POST   /v1/ingest/url          Ingest a web page by URL
POST   /v1/ingest/repo         Ingest a GitHub repository
GET    /v1/ingest/jobs         List ingestion jobs (filterable)
GET    /v1/ingest/{job_id}     Get job status and statistics
DELETE /v1/ingest/{job_id}     Delete job + all associated chunks

All endpoints require ``Authorization: Bearer <API_KEY>``.

Enqueue behaviour
-----------------
``BACKEND_DISABLE_JOBS=1``  — run inline in a thread-pool (test mode).
``REDIS_URL`` set            — enqueue via RQ (production mode).
Neither                      — run in a daemon background thread.

All three paths call the same ``process_ingest_job`` function, so
behaviour is identical regardless of execution context.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.app.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))

# Staging directory: uploaded files are saved here before the background
# worker picks them up.  Cleaned up by the main-process cleanup daemon.
_STAGING_DIR = Path(os.environ.get("INGEST_STAGING_DIR", "/tmp/ingest_staging"))


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------


def _db_session():
    try:
        from backend.app.database import get_session

        yield from get_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Job enqueue helper
# ---------------------------------------------------------------------------


def _enqueue(job_id: str) -> None:
    """
    Dispatch *process_ingest_job(job_id)* through the appropriate executor.

    Priority: BACKEND_DISABLE_JOBS > REDIS_URL > daemon thread.

    MQP-CONTRACT: INGESTION_EXECUTION_ALIGNMENT_V1 §A
        RULE A1 — SINGLE QUEUE: All jobs enqueued to Queue("default")
        RULE A2 — DIRECT ENQUEUE ONLY: Using q.enqueue() directly
        RULE A3 — VALIDATION CHECK: Assert queue name is "rq:queue:default"
    
    MQP-CONTRACT:QUEUE_SINGLE_PATH_ENFORCEMENT_V1 §2
        Updated to use enqueue_job() single entry point instead of direct q.enqueue()
    """
    disable = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        # Tests: run synchronously in the calling thread so DB is fully
        # updated before the test assertion, and to avoid SQLite concurrency
        # issues with ThreadPoolExecutor.
        from backend.app.ingest_pipeline import process_ingest_job

        process_ingest_job(job_id)
        return

    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        try:
            from redis import Redis

            # MQP-CONTRACT:QUEUE_SINGLE_PATH_ENFORCEMENT_V1 §2 — Use single entry point
            from backend.app.worker import enqueue_job
            
            enqueue_job(job_id, "process_ingest_job")
            logger.info("IngestJob %s enqueued via RQ", job_id)

            # MQP-CONTRACT: INGESTION_EXECUTION_ALIGNMENT_V1 §A3 — Validation
            # Verify no intermediate queue exists
            conn = Redis.from_url(redis_url)
            keys = conn.keys("rq:queue:*:intermediate")
            if keys:
                logger.error(
                    "QUEUE_VIOLATION: Intermediate queue detected after enqueue: %s",
                    keys,
                )
                raise RuntimeError(
                    f"QUEUE_VIOLATION: Intermediate queue prohibited. Found: {keys}"
                )
            return
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning("RQ unavailable (%s) — falling back to thread", exc)

    from backend.app.ingest_pipeline import process_ingest_job

    t = threading.Thread(
        target=process_ingest_job,
        args=(job_id,),
        daemon=True,
        name=f"ingest-{job_id}",
    )
    t.start()
    logger.info("IngestJob %s started in background thread", job_id)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class IngestJobResponse(BaseModel):
    job_id: str
    kind: str
    source: str
    status: str
    progress: int = 0
    file_count: int
    chunk_count: int
    error: Optional[str] = None
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_at: str
    updated_at: str


def _to_response(job: object) -> IngestJobResponse:
    return IngestJobResponse(
        job_id=str(job.id),  # type: ignore[attr-defined]
        kind=job.kind,  # type: ignore[attr-defined]
        source=job.source,  # type: ignore[attr-defined]
        status=job.status,  # type: ignore[attr-defined]
        progress=getattr(job, "progress", 0),  # type: ignore[attr-defined]
        file_count=job.file_count,  # type: ignore[attr-defined]
        chunk_count=job.chunk_count,  # type: ignore[attr-defined]
        error=job.error,  # type: ignore[attr-defined]
        conversation_id=job.conversation_id,  # type: ignore[attr-defined]
        workspace_id=job.workspace_id,  # type: ignore[attr-defined]
        created_at=job.created_at.isoformat() if job.created_at else "",  # type: ignore[attr-defined]
        updated_at=job.updated_at.isoformat() if job.updated_at else "",  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# POST /v1/ingest/file
# ---------------------------------------------------------------------------


@router.post("/file", status_code=202, dependencies=[Depends(require_auth)])
async def ingest_file(
    file: UploadFile = File(...),
    conversation_id: Optional[str] = Form(default=None),
    workspace_id: Optional[str] = Form(default=None),
    session=Depends(_db_session),
) -> IngestJobResponse:
    """
    Upload a file and queue it for text extraction and chunking.

    Accepted formats: PDF, DOCX, HTML, CSV, JSON, XML, plain text,
    any source-code file with a recognised extension, ZIP archives.

    The endpoint returns immediately with ``status: "queued"``.
    Poll ``GET /v1/ingest/{job_id}`` to track progress.
    """
    from backend.app.models import IngestJob

    filename = file.filename or "upload"
    _, ext = os.path.splitext(filename.lower())

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size {len(data):,} bytes exceeds the maximum allowed "
                f"upload size of {MAX_UPLOAD_BYTES:,} bytes."
            ),
        )

    # Save to staging area; the worker reads it from here
    _STAGING_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4()
    staging_path = _STAGING_DIR / f"{job_id}{ext or '.bin'}"
    
    # MQP-CONTRACT:FILE_STAGING_INVARIANT_ENFORCEMENT_V1 §1
    # Write file and force flush to disk atomically for guaranteed handoff
    try:
        # Open, write, and fsync in one operation to avoid reopening
        with open(staging_path, "wb") as f:
            f.write(data)
            f.flush()  # Flush Python buffer
            os.fsync(f.fileno())  # Force OS to write to disk
        
        # Verify file was written correctly
        file_stat = staging_path.stat()
        if file_stat.st_size != len(data):
            staging_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"STAGING_INVARIANT_VIOLATION: File size mismatch. "
                f"Expected {len(data)}, got {file_stat.st_size}"
            )
    except FileNotFoundError:
        raise RuntimeError(f"STAGING_INVARIANT_VIOLATION: File write failed: {staging_path}")
    except Exception as exc:
        staging_path.unlink(missing_ok=True)
        raise RuntimeError(f"STAGING_INVARIANT_VIOLATION: Cannot write file: {exc}") from exc

    job = IngestJob(
        id=job_id,
        kind="file",
        source=filename,
        source_path=str(staging_path),
        status="queued",
        conversation_id=conversation_id,
        workspace_id=workspace_id,
    )
    session.add(job)
    session.commit()

    _enqueue(str(job_id))
    # After commit + synchronous enqueue, lazy-load on _to_response(job) reads
    # the terminal status committed by process_ingest_job.
    return _to_response(job)


# ---------------------------------------------------------------------------
# POST /v1/ingest/url
# ---------------------------------------------------------------------------


class IngestUrlRequest(BaseModel):
    url: str
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None


@router.post("/url", status_code=202, dependencies=[Depends(require_auth)])
def ingest_url(
    body: IngestUrlRequest,
    session=Depends(_db_session),
) -> IngestJobResponse:
    """
    Fetch a public web page and queue its text content for ingestion.

    The pipeline strips HTML tags, chunks the plain text, and stores the
    results as RepoChunk rows with ``source_url`` set so chat responses can
    cite the original page.
    """
    from backend.app.models import IngestJob

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://",
        )

    job_id = uuid.uuid4()
    job = IngestJob(
        id=job_id,
        kind="url",
        source=url,
        status="queued",
        conversation_id=body.conversation_id,
        workspace_id=body.workspace_id,
    )
    session.add(job)
    session.commit()

    _enqueue(str(job_id))
    return _to_response(job)


# ---------------------------------------------------------------------------
# POST /v1/ingest/repo
# ---------------------------------------------------------------------------


class IngestRepoRequest(BaseModel):
    repo_url: str
    branch: str = "main"
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    force_refresh: bool = False


@router.post("/repo", status_code=202, dependencies=[Depends(require_auth)])
def ingest_repo(
    body: IngestRepoRequest,
    session=Depends(_db_session),
) -> IngestJobResponse:
    """
    Ingest a GitHub repository.

    Uses the GitHub Trees API (single request for the full file tree) then
    fetches raw file content from raw.githubusercontent.com.  Supports all
    text and code file types in INGESTIBLE_EXTENSIONS.

    Deduplication: if a job for the same ``(repo_url, branch,
    conversation_id)`` already exists and is not failed, the existing job
    is returned — unless ``force_refresh=true``.
    """
    import re

    from sqlmodel import select

    from backend.app.models import IngestJob

    # Require github.com as a proper domain — reject substrings like notgithub.com
    if not re.search(r"(?<![a-zA-Z0-9])github\.com/[^/]+/[^/]+", body.repo_url):
        raise HTTPException(
            status_code=400,
            detail="Invalid GitHub repository URL. Expected https://github.com/owner/repo",
        )

    source_key = f"{body.repo_url}@{body.branch}"

    # Deduplication: return existing active job unless force_refresh
    if not body.force_refresh:
        existing = session.exec(
            select(IngestJob)
            .where(IngestJob.kind == "repo")
            .where(IngestJob.source == source_key)
            .where(IngestJob.conversation_id == body.conversation_id)
            .order_by(IngestJob.created_at.desc())
        ).first()
        if existing and existing.status in ("queued", "running", "success"):
            return _to_response(existing)

    job_id = uuid.uuid4()
    job = IngestJob(
        id=job_id,
        kind="repo",
        source=source_key,
        branch=body.branch,
        status="queued",
        conversation_id=body.conversation_id,
        workspace_id=body.workspace_id,
    )
    session.add(job)
    session.commit()

    _enqueue(str(job_id))
    return _to_response(job)


# ---------------------------------------------------------------------------
# GET /v1/ingest/jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", status_code=200, dependencies=[Depends(require_auth)])
def list_ingest_jobs(
    conversation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    session=Depends(_db_session),
) -> list[IngestJobResponse]:
    """
    List ingestion jobs, optionally filtered by conversation, workspace,
    kind (file | url | repo), and status (queued | running | success | failed).

    Returns at most 200 results, newest first.
    """
    from sqlmodel import select

    from backend.app.models import IngestJob

    stmt = select(IngestJob).order_by(IngestJob.created_at.desc()).limit(200)
    if conversation_id:
        stmt = stmt.where(IngestJob.conversation_id == conversation_id)
    if workspace_id:
        stmt = stmt.where(IngestJob.workspace_id == workspace_id)
    if kind:
        stmt = stmt.where(IngestJob.kind == kind)
    if status:
        stmt = stmt.where(IngestJob.status == status)

    return [_to_response(j) for j in session.exec(stmt).all()]


# ---------------------------------------------------------------------------
# GET /v1/ingest/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}", status_code=200, dependencies=[Depends(require_auth)])
def get_ingest_job(
    job_id: str,
    session=Depends(_db_session),
) -> IngestJobResponse:
    """Return the current status and statistics of an ingestion job."""
    from backend.app.models import IngestJob

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id: must be a UUID")

    job = session.get(IngestJob, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    return _to_response(job)


# ---------------------------------------------------------------------------
# DELETE /v1/ingest/{job_id}
# ---------------------------------------------------------------------------


@router.delete("/{job_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_ingest_job(
    job_id: str,
    session=Depends(_db_session),
) -> None:
    """
    Delete an ingestion job and all chunks it produced.

    Also removes the staged file from disk (for file uploads).
    """
    from sqlmodel import select

    from backend.app.models import IngestJob, RepoChunk

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id: must be a UUID")

    job = session.get(IngestJob, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    # Remove associated chunks first
    for chunk in session.exec(
        select(RepoChunk).where(RepoChunk.ingest_job_id == job_uuid)
    ).all():
        session.delete(chunk)

    # Clean up the staged file from disk
    if job.source_path:
        try:
            Path(job.source_path).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete staged file %s: %s", job.source_path, exc)

    session.delete(job)
    session.commit()
    return None
