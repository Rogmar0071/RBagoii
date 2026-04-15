"""
backend.app.mutation_governance.audit
=======================================
Mandatory audit persistence for MUTATION_GOVERNANCE_EXECUTION_V1.

Enforcement — ``block_if_log_not_written``:
  - If the database IS configured: the write MUST succeed.  On any failure a
    ``RuntimeError("AUDIT_LOG_FAILURE: ...")`` is raised, which propagates
    through the gateway and blocks the proposal from being returned.
  - If the database is NOT configured: a warning is logged and the function
    returns without blocking (deployment/configuration concern).

Audit log fields (mandatory per contract):
  - user_intent
  - selected_modes
  - mutation_proposal
  - validation_results
  - blocked_reason_if_any
"""

from __future__ import annotations

import logging

from .contract import MutationGovernanceAuditRecord

logger = logging.getLogger(__name__)


def persist_mutation_audit_record(record: MutationGovernanceAuditRecord) -> None:
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
            "mutation_governance: database not configured; "
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
            event_type="mutation_governance.execution_v1.audit",
            message=f"MUTATION_GOVERNANCE_EXECUTION_V1 [{record.audit_id}]",
            details_json={
                "audit_id": record.audit_id,
                "contract_id": record.contract_id,
                "user_intent": record.user_intent[:500],
                "selected_modes": record.selected_modes,
                "mutation_proposal": record.mutation_proposal,
                "validation_results": record.validation_results,
                "blocked_reason": record.blocked_reason,
                "status": record.status,
                "created_at": record.created_at,
            },
        )
        with _Session(engine) as session:
            session.add(event)
            session.commit()
    except Exception as exc:
        raise RuntimeError(f"AUDIT_LOG_FAILURE: {exc}") from exc
