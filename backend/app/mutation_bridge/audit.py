"""
backend.app.mutation_bridge.audit
====================================
Mandatory audit persistence for MUTATION_BRIDGE_EXECUTION_V1.

Enforcement — ``block_if:audit_write_failure``:
  - If the database IS configured: the write MUST succeed.  On any failure a
    ``RuntimeError("BRIDGE_AUDIT_LOG_FAILURE: ...")`` is raised, which
    propagates through the gateway and blocks the result from being returned.
    NO try/except suppression is permitted.
  - If the database is NOT configured: a warning is logged and the function
    returns without blocking (deployment/configuration concern).

Audit log fields (mandatory per contract):
  - governance_result
  - simulation_result
  - runtime_validation_result
  - execution_actions
  - artifacts
  - timestamp
"""

from __future__ import annotations

import logging

from .contract import BridgeAuditRecord

logger = logging.getLogger(__name__)


def persist_bridge_audit_record(record: BridgeAuditRecord) -> None:
    """Write *record* to the ``ops_events`` table.

    Raises
    ------
    RuntimeError
        If the database is configured and the write fails
        (``block_if:audit_write_failure`` invariant).
        This exception is NEVER suppressed — it must propagate.
    """
    try:
        from backend.app.database import get_engine

        engine = get_engine()
    except RuntimeError:
        logger.warning(
            "mutation_bridge: database not configured; "
            "audit record %s not persisted",
            record.audit_id,
        )
        return

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
            "created_at": record.created_at,
        },
    )
    with _Session(engine) as session:
        session.add(event)
        session.commit()
