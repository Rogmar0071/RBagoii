"""
backend.app.analysis_routes
============================
REST API endpoints for the standalone analysis job system.

Endpoints
---------
POST   /v1/analysis                    create and enqueue an analysis job  [auth]
GET    /v1/analysis/{job_id}           get job status + partial results     [auth]
GET    /v1/analysis/{job_id}/results   get full results JSON                [auth]
DELETE /v1/analysis/{job_id}           cancel / delete a job               [auth]
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from backend.app.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/analysis", tags=["analysis"])

_UUID_RE_STR = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _db_session():
    """FastAPI dependency — yields a DB session or raises 503 if unconfigured."""
    try:
        from backend.app.database import get_session

        yield from get_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _parse_uuid(value: str, field: str = "job_id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value!r}") from None


def _dt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _job_dict(job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "file_path": job.file_path,
        "status": job.status,
        "results_json": job.results_json,
        "errors_json": job.errors_json,
        "warnings_json": job.warnings_json,
        "created_at": _dt(job.created_at),
        "updated_at": _dt(job.updated_at),
    }


def _enqueue_analysis_job(job_id: str, file_path: str) -> None:
    """Enqueue the analysis job via RQ if available, else run in a thread."""
    disable = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        return

    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        try:
            # MQP-CONTRACT:QUEUE_SINGLE_PATH_ENFORCEMENT_V1 §2 — Use single entry point
            from backend.app.worker import enqueue_job

            enqueue_job(job_id, "process_analysis_job")
            return
        except Exception as exc:
            logger.warning("RQ unavailable (%s); running analysis job in thread.", exc)

    from backend.app.analysis_job_processor import process_analysis_job

    t = threading.Thread(
        target=process_analysis_job,
        args=(job_id,),
        daemon=True,
        name=f"analysis-{job_id}",
    )
    t.start()


# ---------------------------------------------------------------------------
# POST /v1/analysis  — create and enqueue an analysis job
# ---------------------------------------------------------------------------


@router.post("", status_code=201, dependencies=[Depends(require_auth)])
def create_analysis_job(body: dict[str, Any] = None, db=Depends(_db_session)) -> JSONResponse:
    """
    Create an analysis job for a file that was previously uploaded via the
    chunked-upload or single-upload endpoint.

    Request body
    ------------
    .. code-block:: json

        {
            "file_path": "/tmp/uploads/<uuid>.zip"
        }

    Returns the job id and initial status ``queued``.
    """
    from pathlib import Path

    from backend.app.models import AnalysisJob

    if body is None:
        body = {}
    file_path: str = body.get("file_path", "")
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")

    # Confine file_path to the uploads directory to prevent path traversal via the API.
    from backend.app.main import _UPLOADS_DIR as _uploads_root

    try:
        resolved = Path(file_path).resolve()
        resolved.relative_to(_uploads_root.resolve())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="file_path must be within the uploads directory",
        ) from None

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    job = AnalysisJob(file_path=file_path, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)

    _enqueue_analysis_job(str(job.id), file_path)

    return JSONResponse(content=_job_dict(job), status_code=201)


# ---------------------------------------------------------------------------
# GET /v1/analysis/{job_id}  — job status + partial results
# ---------------------------------------------------------------------------


@router.get("/{job_id}", dependencies=[Depends(require_auth)])
def get_analysis_job(job_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return current status and any partial results for the analysis job."""
    from backend.app.models import AnalysisJob

    jid = _parse_uuid(job_id)
    job = db.get(AnalysisJob, jid)
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    return JSONResponse(content=_job_dict(job))


# ---------------------------------------------------------------------------
# GET /v1/analysis/{job_id}/results  — full results JSON
# ---------------------------------------------------------------------------


@router.get("/{job_id}/results", dependencies=[Depends(require_auth)])
def get_analysis_job_results(job_id: str, db=Depends(_db_session)) -> JSONResponse:
    """Return the full results JSON for a completed analysis job."""
    from backend.app.models import AnalysisJob

    jid = _parse_uuid(job_id)
    job = db.get(AnalysisJob, jid)
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if job.status not in ("succeeded", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is not yet complete (status={job.status})",
        )
    return JSONResponse(
        content={
            "id": str(job.id),
            "status": job.status,
            "results": job.results_json,
            "errors": job.errors_json,
            "warnings": job.warnings_json,
        }
    )


# ---------------------------------------------------------------------------
# DELETE /v1/analysis/{job_id}  — cancel/delete a job
# ---------------------------------------------------------------------------


@router.delete("/{job_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_analysis_job(job_id: str, db=Depends(_db_session)) -> None:
    """Cancel and delete an analysis job record."""
    from backend.app.models import AnalysisJob

    jid = _parse_uuid(job_id)
    job = db.get(AnalysisJob, jid)
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    db.delete(job)
    db.commit()
