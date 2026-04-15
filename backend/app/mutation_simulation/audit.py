"""
backend.app.mutation_simulation.audit
=======================================
Mandatory audit persistence for MUTATION_SIMULATION_EXECUTION_V1.

Enforcement — ``block_if_log_not_written``:
  - If the database IS configured: the write MUST succeed.  On any failure a
    ``RuntimeError("SIMULATION_AUDIT_LOG_FAILURE: ...")`` is raised, which
    propagates through the gateway and blocks the result from being returned.
  - If the database is NOT configured: a warning is logged and the function
    returns without blocking (deployment/configuration concern).

Audit log fields (mandatory per contract):
  - mutation_contract
  - simulation_outputs
  - risk_level
  - decision
  - override_used
"""

from __future__ import annotations

import logging

from .contract import SimulationAuditRecord

logger = logging.getLogger(__name__)


def persist_simulation_audit_record(record: SimulationAuditRecord) -> None:
    """Write *record* to the ``ops_events`` table.

    Raises
    ------
    RuntimeError
        If the database is configured and the write fails
        (``block_if_log_not_written`` invariant).
    """
    try:
        from backend.app.database import get_engine

        engine = get_engine()
    except RuntimeError:
        logger.warning(
            "mutation_simulation: database not configured; "
            "audit record %s not persisted",
            record.audit_id,
        )
        return

    try:
        from sqlmodel import Session as _Session

        from backend.app.models import OpsEvent

        event = OpsEvent(
            source="backend",
            level="info",
            event_type="mutation_simulation.execution_v1.audit",
            message=f"MUTATION_SIMULATION_EXECUTION_V1 [{record.audit_id}]",
            details_json={
                "audit_id": record.audit_id,
                "simulation_id": record.simulation_id,
                "mutation_contract": record.mutation_contract,
                "simulation_outputs": record.simulation_outputs,
                "risk_level": record.risk_level,
                "decision": record.decision,
                "override_used": record.override_used,
                "blocked_reason": record.blocked_reason,
                "created_at": record.created_at,
            },
        )
        with _Session(engine) as session:
            session.add(event)
            session.commit()
    except Exception as exc:
        raise RuntimeError(f"SIMULATION_AUDIT_LOG_FAILURE: {exc}") from exc
