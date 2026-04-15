"""
backend.app.mutation_bridge.contract
======================================
Data contracts for MUTATION_BRIDGE_EXECUTION_V1.

Defines all structured types used across the bridge execution pipeline.
No mutation may be bridge-executed without conforming to these types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Bridge execution status constants
# ---------------------------------------------------------------------------

BRIDGE_STATUS_EXECUTED = "executed"
BRIDGE_STATUS_BLOCKED = "blocked"

# Minimum meaningful length for override justification strings.
BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH = 10

# ---------------------------------------------------------------------------
# Build status constants
# ---------------------------------------------------------------------------

BUILD_STATUS_PASSED = "passed"
BUILD_STATUS_SKIPPED = "skipped"
BUILD_STATUS_FAILED = "failed"

# ---------------------------------------------------------------------------
# Execution boundary — enforced constants (never relaxed)
# ---------------------------------------------------------------------------

_BRIDGE_EXECUTION_BOUNDARY: dict[str, bool] = {
    "no_direct_commit_to_main": True,
    "no_auto_merge": True,
    "no_deployment_trigger": True,
}


# ---------------------------------------------------------------------------
# Bridge execution override
# ---------------------------------------------------------------------------


@dataclass
class BridgeExecutionOverride:
    """Override required for high-risk bridge execution.

    All three fields are required for a valid override:
      - explicit_approval must be True (user has explicitly approved).
      - justification must be a non-empty string of at least
        BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH characters.
      - accepted_risks must be a non-empty list of non-empty strings.

    Raises
    ------
    ValueError
        If any field does not satisfy its constraint.
    """

    explicit_approval: bool
    justification: str
    accepted_risks: list[str]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not self.explicit_approval:
            raise ValueError(
                "override.explicit_approval must be True for a valid bridge override"
            )
        jstr = self.justification.strip() if isinstance(self.justification, str) else ""
        if len(jstr) < BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH:
            raise ValueError(
                f"override.justification must be at least "
                f"{BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH} characters; "
                f"got {len(jstr)!r}"
            )
        if (
            not isinstance(self.accepted_risks, list)
            or not self.accepted_risks
            or not all(isinstance(r, str) and r.strip() for r in self.accepted_risks)
        ):
            raise ValueError(
                "override.accepted_risks must be a non-empty list of "
                "non-empty strings"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "explicit_approval": self.explicit_approval,
            "justification": self.justification,
            "accepted_risks": self.accepted_risks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeExecutionOverride":
        """Construct and validate a BridgeExecutionOverride from a raw dict.

        Raises ValueError if constraints are not satisfied.
        """
        return cls(
            explicit_approval=bool(data.get("explicit_approval", False)),
            justification=str(data.get("justification", "")),
            accepted_risks=list(data.get("accepted_risks", [])),
        )


# ---------------------------------------------------------------------------
# Bridge execution result (structured output contract)
# ---------------------------------------------------------------------------


@dataclass
class BridgeResult:
    """Structured output of MUTATION_BRIDGE_EXECUTION_V1.

    Required artifact fields per response contract:
      - branch_name
      - diff_patch
      - modified_files_list
      - execution_summary

    Guarantees:
      - full_traceability
      - reproducible_changes
      - human_review_ready
      - reversible_execution_path
    """

    bridge_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    governance_contract: str = "MUTATION_BRIDGE_EXECUTION_V1"
    source_governance_contract_id: str = ""
    source_simulation_id: str = ""
    # Execution status
    status: str = BRIDGE_STATUS_BLOCKED
    blocked_reason: str | None = None
    # Artifact outputs (all mandatory)
    branch_name: str = ""
    diff_patch: str = ""
    modified_files_list: list[str] = field(default_factory=list)
    build_status: str = BUILD_STATUS_SKIPPED
    execution_summary: str = ""
    # Traceability
    override_used: bool = False
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Execution boundary — enforced constants
    execution_boundary: dict[str, bool] = field(
        default_factory=lambda: dict(_BRIDGE_EXECUTION_BOUNDARY)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "governance_contract": self.governance_contract,
            "source_governance_contract_id": self.source_governance_contract_id,
            "source_simulation_id": self.source_simulation_id,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "branch_name": self.branch_name,
            "diff_patch": self.diff_patch,
            "modified_files_list": self.modified_files_list,
            "build_status": self.build_status,
            "execution_summary": self.execution_summary,
            "override_used": self.override_used,
            "audit_id": self.audit_id,
            "created_at": self.created_at,
            "execution_boundary": self.execution_boundary,
        }


# ---------------------------------------------------------------------------
# Bridge audit record
# ---------------------------------------------------------------------------


@dataclass
class BridgeAuditRecord:
    """Full audit trail for a single bridge execution pipeline run.

    All fields are mandatory per the audit_layer contract.
    Logging fields (per contract):
      - governance_result
      - simulation_result
      - runtime_validation_result
      - execution_actions
      - artifacts
      - timestamp
    """

    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    bridge_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    governance_result: dict[str, Any] = field(default_factory=dict)
    simulation_result: dict[str, Any] = field(default_factory=dict)
    runtime_validation_result: dict[str, Any] = field(default_factory=dict)
    execution_actions: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    status: str = BRIDGE_STATUS_BLOCKED
    blocked_reason: str | None = None
    override_used: bool = False
    override_details: dict[str, Any] | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
