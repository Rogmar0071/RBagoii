"""
backend.app.mutation_governance.audit
=======================================
Mandatory audit persistence for MUTATION_GOVERNANCE_EXECUTION_V1.

Enforcement — ``block_if_log_not_written``:
  - The database MUST be configured and the write MUST succeed.  On any
    failure (including DATABASE_URL not configured) a
    ``RuntimeError("AUDIT_LOG_FAILURE: ...")`` is raised, which propagates
    through the gateway and blocks the proposal from being returned.

Contract invariant: ``audit_is_mandatory`` — no execution without audit.

Audit log fields (mandatory per contract):
  - user_intent
  - selected_modes
  - mutation_proposal
  - validation_results
  - blocked_reason_if_any
"""

from __future__ import annotations

from .contract import MutationGovernanceAuditRecord


def persist_mutation_audit_record(record: MutationGovernanceAuditRecord) -> None:
    """Write *record* to the ``ops_events`` table.

    Raises
    ------
    RuntimeError
        If the database is not configured or the write fails
        (``block_if_log_not_written`` invariant — audit is mandatory).
    """
    try:
        from backend.app.database import get_engine

        engine = get_engine()
    except RuntimeError as exc:
        raise RuntimeError(
            f"AUDIT_SYSTEM_UNAVAILABLE: DATABASE_URL is not configured — "
            f"audit cannot be persisted (record {record.audit_id}): {exc}"
        ) from exc

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
