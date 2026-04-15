"""
backend.app.mutation_governance.contract
=========================================
Mutation contract schema for MUTATION_GOVERNANCE_EXECUTION_V1.

Defines the required structure for all AI-generated mutation proposals.
No mutation may be processed without conforming to this contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Union


class OperationType(str, Enum):
    create_file = "create_file"
    update_file = "update_file"
    delete_file = "delete_file"


class MutationContract:
    """
    Parsed mutation proposal produced by the AI.

    All fields are required and must be non-empty (no_empty_fields constraint).
    The contract is immutable once constructed; a new draft must be created for
    any revision (FORWARD_ONLY reversibility).
    """

    REQUIRED_FIELDS: tuple[str, ...] = (
        "target_files",
        "operation_type",
        "proposed_changes",
        "assumptions",
        "alternatives",
        "confidence",
        "risks",
        "missing_data",
    )

    VALID_OPERATION_TYPES: frozenset[str] = frozenset(
        op.value for op in OperationType
    )

    def __init__(
        self,
        *,
        target_files: list[str],
        operation_type: str,
        proposed_changes: str,
        assumptions: list[str],
        alternatives: list[str],
        confidence: Union[float, int, str],
        risks: list[str],
        missing_data: list[str],
    ) -> None:
        self.target_files = target_files
        self.operation_type = operation_type
        self.proposed_changes = proposed_changes
        self.assumptions = assumptions
        self.alternatives = alternatives
        self.confidence = confidence
        self.risks = risks
        self.missing_data = missing_data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MutationContract":
        """Construct from raw dict parsed from a JSON mutation contract block.

        Only canonical lowercase field names are accepted.  The JSON produced
        by the AI must use the exact field names defined in ``REQUIRED_FIELDS``.
        Mode engine marker names (e.g. ``ASSUMPTIONS``, ``CONFIDENCE``) appear
        only as plain-text labels in the AI output and are never present in the
        JSON block — the two validation pipelines are fully independent.

        Does not validate — call the 3-stage pipeline separately.
        """
        return cls(
            target_files=data.get("target_files", []),
            operation_type=data.get("operation_type", ""),
            proposed_changes=data.get("proposed_changes", ""),
            assumptions=data.get("assumptions", []),
            alternatives=data.get("alternatives", []),
            confidence=data.get("confidence", ""),
            risks=data.get("risks", []),
            missing_data=data.get("missing_data", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_files": self.target_files,
            "operation_type": self.operation_type,
            "proposed_changes": self.proposed_changes,
            "assumptions": self.assumptions,
            "alternatives": self.alternatives,
            "confidence": self.confidence,
            "risks": self.risks,
            "missing_data": self.missing_data,
        }


@dataclass
class MutationValidationResult:
    """Outcome of a single mutation validation stage."""

    passed: bool
    stage: str = ""
    failed_rules: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    correction_instructions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "failed_rules": self.failed_rules,
            "blocked_paths": self.blocked_paths,
            "correction_instructions": self.correction_instructions,
        }


@dataclass
class MutationGovernanceAuditRecord:
    """Full audit trail for a single mutation governance pipeline execution.

    Audit layer enforcement (``block_if_log_not_written``):
    - user_intent, selected_modes, mutation_proposal, validation_results,
      and blocked_reason_if_any are all mandatory log fields.
    """

    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    contract_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_intent: str = ""
    selected_modes: list[str] = field(default_factory=list)
    mutation_proposal: dict[str, Any] = field(default_factory=dict)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: str | None = None
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
