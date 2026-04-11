"""
backend.app.ops_log
====================
Fire-and-forget helper for writing to the ``ops_events`` table.

Usage::

    from backend.app.ops_log import log_event

    log_event(
        source="backend",
        level="info",
        event_type="folders.create",
        message="Folder created",
        folder_id=str(folder.id),
    )

The helper never raises — errors are emitted at WARNING level so they do not
disrupt the calling request/job.  It is a no-op when DATABASE_URL is not
configured.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Allowed values (enforced for documentation; not validated at runtime to
# avoid hard failures in instrumented hot paths).
_VALID_SOURCES = frozenset({"backend", "worker", "storage", "rq", "db", "auth"})
_VALID_LEVELS = frozenset({"debug", "info", "warning", "error"})


def log_event(
    *,
    source: str,
    level: str,
    event_type: str,
    message: str,
    folder_id: Optional[str] = None,
    job_id: Optional[str] = None,
    artifact_id: Optional[str] = None,
    rq_job_id: Optional[str] = None,
    request_id: Optional[str] = None,
    http_method: Optional[str] = None,
    http_path: Optional[str] = None,
    http_status: Optional[int] = None,
    duration_ms: Optional[int] = None,
    error_type: Optional[str] = None,
    error_detail: Optional[str] = None,
    details_json: Optional[Any] = None,
) -> None:
    """Persist an ops event.  Never raises."""
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import OpsEvent

        def _to_uuid(val: Optional[str]) -> Optional[uuid.UUID]:
            if not val:
                return None
            try:
                return uuid.UUID(val)
            except (ValueError, AttributeError):
                return None

        event = OpsEvent(
            source=source,
            level=level,
            event_type=event_type,
            message=message,
            folder_id=_to_uuid(folder_id),
            job_id=_to_uuid(job_id),
            artifact_id=_to_uuid(artifact_id),
            rq_job_id=rq_job_id,
            request_id=request_id,
            http_method=http_method,
            http_path=http_path,
            http_status=http_status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_detail=error_detail,
            details_json=details_json,
        )
        with Session(get_engine()) as session:
            session.add(event)
            session.commit()
    except RuntimeError:
        # DATABASE_URL not configured — silently skip.
        pass
    except Exception as exc:  # pragma: no cover
        logger.warning("ops_log: failed to persist event %r: %s", event_type, exc)
