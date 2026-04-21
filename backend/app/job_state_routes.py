from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from backend.app.auth import require_auth
from backend.app.ingest_routes import _db_session
from backend.app.models import IngestJob

router = APIRouter(tags=["job_state"])


class JobStateResponse(BaseModel):
    job_id: str
    status: str
    execution_locked: bool
    created_at: str
    updated_at: str
    source: str
    kind: str
    conversation_id: Optional[str] = None


def _project_status(raw_status: str) -> str:
    if raw_status in {"success", "failed", "queued", "running"}:
        return raw_status
    if raw_status in {"created", "stored"}:
        return "queued"
    if raw_status in {"processing", "indexing", "finalizing"}:
        return "running"
    raise HTTPException(status_code=500, detail=f"Unsupported job status: {raw_status}")


def _to_job_state(job: IngestJob) -> JobStateResponse:
    return JobStateResponse(
        job_id=str(job.id),
        status=_project_status(job.status),
        execution_locked=bool(job.execution_locked),
        created_at=job.created_at.isoformat() if job.created_at else "",
        updated_at=job.updated_at.isoformat() if job.updated_at else "",
        source=job.source,
        kind=job.kind,
        conversation_id=job.conversation_id,
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(require_auth)], status_code=200)
@router.get("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)], status_code=200)
def get_job_state(job_id: str, session=Depends(_db_session)) -> JobStateResponse:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id: must be a UUID")

    job = session.get(IngestJob, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_job_state(job)


@router.get("/chat/{conversation_id}/jobs", dependencies=[Depends(require_auth)], status_code=200)
@router.get(
    "/v1/chat/{conversation_id}/jobs",
    dependencies=[Depends(require_auth)],
    status_code=200,
)
def list_conversation_jobs(
    conversation_id: str,
    kind: Optional[str] = Query(default=None),
    session=Depends(_db_session),
) -> list[JobStateResponse]:
    stmt = (
        select(IngestJob)
        .where(IngestJob.conversation_id == conversation_id)
        .order_by(IngestJob.created_at.desc())
        .limit(200)
    )
    if kind:
        stmt = stmt.where(IngestJob.kind == kind)
    return [_to_job_state(job) for job in session.exec(stmt).all()]
