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
        """Construct from raw dict (AI JSON output).

        Accepts both lowercase field names (canonical) and uppercase aliases
        produced by the mode engine marker convention
        (e.g. ``"ASSUMPTIONS"`` → ``assumptions``).
        Unknown keys (e.g. ``SECTION_MUTATION_CONTRACT``) are silently ignored.
        Does not perform validation — call the validation pipeline separately.
        """

        def _get(lower: str, upper: str, default: Any) -> Any:
            return data.get(lower, data.get(upper, default))

        return cls(
            target_files=_get("target_files", "TARGET_FILES", []),
            operation_type=_get("operation_type", "OPERATION_TYPE", ""),
            proposed_changes=_get("proposed_changes", "PROPOSED_CHANGES", ""),
            assumptions=_get("assumptions", "ASSUMPTIONS", []),
            alternatives=_get("alternatives", "ALTERNATIVES", []),
            confidence=_get("confidence", "CONFIDENCE", ""),
            risks=_get("risks", "RISKS", []),
            missing_data=_get("missing_data", "MISSING_DATA", []),
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
