"""
backend.app.mutation_bridge.audit
====================================
Mandatory audit persistence for MUTATION_BRIDGE_EXECUTION_V1.

Enforcement — ``block_if:audit_write_failure``:
  - The database MUST be configured and the write MUST succeed.  On any
    failure (including DATABASE_URL not configured) a
    ``RuntimeError("BRIDGE_AUDIT_LOG_FAILURE: ...")`` is raised.
    NO try/except suppression is permitted.

Contract invariant: ``audit_is_mandatory`` — no execution without audit.

Audit log fields (mandatory per contract):
  - governance_result
  - simulation_result
  - runtime_validation_result
  - execution_actions
  - artifacts
  - timestamp
"""

from __future__ import annotations

from .contract import BridgeAuditRecord


def persist_bridge_audit_record(record: BridgeAuditRecord) -> None:
    """Write *record* to the ``ops_events`` table.

    Raises
    ------
    RuntimeError
        If the database is not configured or the write fails
        (``block_if:audit_write_failure`` invariant — audit is mandatory).
        This exception is NEVER suppressed — it must propagate.
    """
    try:
        from backend.app.database import get_engine

        engine = get_engine()
    except RuntimeError as exc:
        raise RuntimeError(
            f"AUDIT_SYSTEM_UNAVAILABLE: DATABASE_URL is not configured — "
            f"audit cannot be persisted (record {record.audit_id}): {exc}"
        ) from exc

    # DO NOT wrap the write in try/except — audit failure must propagate
    # (block_if:audit_write_failure invariant).
    from sqlmodel import Session as _Session

    from backend.app.models import OpsEvent

    event = OpsEvent(
        source="backend",
        level="info",
        event_type="mutation_bridge.execution_v1.audit",
        message=f"MUTATION_BRIDGE_EXECUTION_V1 [{record.audit_id}]",
        details_json={
            "audit_id": record.audit_id,
            "bridge_id": record.bridge_id,
            "governance_result": record.governance_result,
            "simulation_result": record.simulation_result,
            "runtime_validation_result": record.runtime_validation_result,
            "execution_actions": record.execution_actions,
            "artifacts": record.artifacts,
            "status": record.status,
            "blocked_reason": record.blocked_reason,
            "override_used": record.override_used,
            "override_details": record.override_details,
            "created_at": record.created_at,
        },
    )
    with _Session(engine) as session:
        session.add(event)
        session.commit()
