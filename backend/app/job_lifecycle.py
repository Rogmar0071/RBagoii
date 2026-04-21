from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GovernedJobSpec:
    model: type
    model_name: str
    queued_state: str = "queued"
    running_state: str = "running"
    terminal_states: frozenset[str] = frozenset()


def _governed_specs() -> tuple[GovernedJobSpec, ...]:
    from backend.app.models import AnalysisJob, IngestJob, Job

    return (
        GovernedJobSpec(
            model=Job,
            model_name="Job",
            terminal_states=frozenset({"succeeded", "failed"}),
        ),
        GovernedJobSpec(
            model=AnalysisJob,
            model_name="AnalysisJob",
            terminal_states=frozenset({"succeeded", "failed"}),
        ),
        GovernedJobSpec(
            model=IngestJob,
            model_name="IngestJob",
            terminal_states=frozenset({"success", "failed"}),
        ),
    )


def parse_job_uuid(job_id: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        return None


def load_governed_job(job_id: str | None):
    from backend.app.database import get_engine

    job_uuid = parse_job_uuid(job_id)
    if job_uuid is None:
        return None, None

    with Session(get_engine()) as session:
        for spec in _governed_specs():
            record = session.get(spec.model, job_uuid)
            if record is None:
                continue
            _ = record.status
            _ = getattr(record, "execution_locked", False)
            session.expunge(record)
            return record, spec
    return None, None


def claim_governed_job_execution(job_id: str | None) -> dict[str, object]:
    from backend.app.database import get_engine

    job_uuid = parse_job_uuid(job_id)
    if job_uuid is None:
        return {"state": "not_found", "job_id": job_id}

    with Session(get_engine()) as session:
        for spec in _governed_specs():
            record = session.get(spec.model, job_uuid)
            if record is None:
                continue

            now = _utcnow()
            values: dict[object, object] = {
                spec.model.status: spec.running_state,
                spec.model.execution_attempts: (
                    sa.func.coalesce(spec.model.execution_attempts, 0) + 1
                ),
                spec.model.last_execution_at: now,
            }
            if hasattr(spec.model, "updated_at"):
                values[spec.model.updated_at] = now

            result = session.execute(
                sa.update(spec.model)
                .where(spec.model.id == job_uuid)
                .where(spec.model.status == spec.queued_state)
                .where(spec.model.execution_locked.is_(False))
                .values(values)
            )
            session.commit()
            if result.rowcount == 1:
                return {
                    "state": "claimed",
                    "job_id": job_id,
                    "status": spec.running_state,
                    "model_name": spec.model_name,
                }

            refreshed = session.get(spec.model, job_uuid)
            if refreshed is None:
                return {"state": "claim_rejected", "job_id": job_id, "model_name": spec.model_name}
            if bool(getattr(refreshed, "execution_locked", False)):
                return {
                    "state": "blocked_locked",
                    "job_id": job_id,
                    "status": getattr(refreshed, "status", None),
                    "model_name": spec.model_name,
                }
            return {
                "state": "claim_rejected",
                "job_id": job_id,
                "status": getattr(refreshed, "status", None),
                "model_name": spec.model_name,
            }

    return {"state": "not_found", "job_id": job_id}


def prepare_governed_job_for_enqueue(job_id: str | None) -> dict[str, object]:
    from backend.app.database import get_engine

    job_uuid = parse_job_uuid(job_id)
    if job_uuid is None:
        return {"state": "not_found", "job_id": job_id}

    with Session(get_engine()) as session:
        for spec in _governed_specs():
            record = session.get(spec.model, job_uuid)
            if record is None:
                continue

            status = str(record.status)
            locked = bool(getattr(record, "execution_locked", False))

            if locked or status in spec.terminal_states:
                if not locked:
                    setattr(record, "execution_locked", True)
                    if hasattr(record, "updated_at"):
                        setattr(record, "updated_at", _utcnow())
                    session.add(record)
                    session.commit()
                raise RuntimeError("ENQUEUE_BLOCKED_TERMINAL_JOB")

            return {
                "state": "governed",
                "job_id": job_id,
                "status": getattr(record, "status", status),
                "model_name": spec.model_name,
            }

    return {"state": "not_found", "job_id": job_id}
