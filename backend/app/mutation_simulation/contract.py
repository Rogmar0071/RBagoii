"""
backend.app.mutation_simulation.contract
==========================================
Data contracts for MUTATION_SIMULATION_EXECUTION_V1.

Defines all structured types used across the simulation pipeline.
No mutation may be simulated without conforming to these types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

_VALID_RISK_LEVELS: frozenset[str] = frozenset({RISK_LOW, RISK_MEDIUM, RISK_HIGH})

# Minimum meaningful length for override justification strings.
OVERRIDE_MIN_JUSTIFICATION_LENGTH = 10

# ---------------------------------------------------------------------------
# Failure types — all four categories are evaluated for every simulation.
# ---------------------------------------------------------------------------

FAILURE_BUILD = "build_failure"
FAILURE_RUNTIME = "runtime_failure"
FAILURE_DEPENDENCY_BREAK = "dependency_break"
FAILURE_CONTRACT_VIOLATION = "contract_violation"

ALL_FAILURE_CATEGORIES: tuple[str, ...] = (
    FAILURE_BUILD,
    FAILURE_RUNTIME,
    FAILURE_DEPENDENCY_BREAK,
    FAILURE_CONTRACT_VIOLATION,
)


# ---------------------------------------------------------------------------
# Dependency surface
# ---------------------------------------------------------------------------


@dataclass
class DependencySurface:
    """Output of the dependency surface mapping step.

    Identifies all components that a proposed mutation touches.

    Completeness contract:
      ``complete=False``        — mapping failed entirely (triggers hard block).
      ``partially_resolved=True`` — mapping succeeded but some target files have
                                    no known dependency records.  The simulation
                                    continues but risk scoring treats unresolved
                                    files as a medium-risk factor (unknown deps
                                    cannot be assumed safe).
    """

    impacted_files: list[str] = field(default_factory=list)
    impacted_modules: list[str] = field(default_factory=list)
    dependency_links: list[dict[str, str]] = field(default_factory=list)
    complete: bool = True
    incomplete_reason: str | None = None
    # Partial resolution: mapping completed but some files had no known deps.
    partially_resolved: bool = False
    unresolved_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "impacted_files": self.impacted_files,
            "impacted_modules": self.impacted_modules,
            "dependency_links": self.dependency_links,
            "complete": self.complete,
            "incomplete_reason": self.incomplete_reason,
            "partially_resolved": self.partially_resolved,
            "unresolved_files": self.unresolved_files,
        }


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


@dataclass
class ImpactAnalysis:
    """Output of the impact analysis step.

    Describes structural, behavioral, and data-flow effects.
    """

    structural_impact: list[str] = field(default_factory=list)
    behavioral_impact: list[str] = field(default_factory=list)
    data_flow_impact: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "structural_impact": self.structural_impact,
            "behavioral_impact": self.behavioral_impact,
            "data_flow_impact": self.data_flow_impact,
        }


# ---------------------------------------------------------------------------
# Failure prediction
# ---------------------------------------------------------------------------


@dataclass
class PredictedFailure:
    """A single predicted failure mode."""

    failure_type: str
    description: str
    severity: str  # low / medium / high
    alternative_scenario: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_type": self.failure_type,
            "description": self.description,
            "severity": self.severity,
            "alternative_scenario": self.alternative_scenario,
        }


@dataclass
class FailurePrediction:
    """Output of the failure prediction step."""

    predicted_failures: list[PredictedFailure] = field(default_factory=list)
    failure_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicted_failures": [f.to_dict() for f in self.predicted_failures],
            "failure_types": self.failure_types,
        }


# ---------------------------------------------------------------------------
# Risk score
# ---------------------------------------------------------------------------


@dataclass
class RiskScore:
    """Output of the risk scoring step."""

    level: str  # low / medium / high
    criteria_matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "criteria_matched": self.criteria_matched,
        }


# ---------------------------------------------------------------------------
# Simulation override
# ---------------------------------------------------------------------------


@dataclass
class SimulationOverride:
    """Optional override for high-risk simulations.

    Required when risk_level == high.

    Enforcement rules:
      - justification must be a non-empty string of at least
        OVERRIDE_MIN_JUSTIFICATION_LENGTH characters.
      - accepted_risks must be a non-empty list of non-empty strings.

    Raises
    ------
    ValueError
        If either field does not satisfy its constraint.
    """

    justification: str
    accepted_risks: list[str]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        jstr = self.justification.strip() if isinstance(self.justification, str) else ""
        if len(jstr) < OVERRIDE_MIN_JUSTIFICATION_LENGTH:
            raise ValueError(
                f"override.justification must be at least "
                f"{OVERRIDE_MIN_JUSTIFICATION_LENGTH} characters; "
                f"got {len(jstr)!r}"
            )
        if (
            not isinstance(self.accepted_risks, list)
            or not self.accepted_risks
            or not all(
                isinstance(r, str) and r.strip() for r in self.accepted_risks
            )
        ):
            raise ValueError(
                "override.accepted_risks must be a non-empty list of "
                "non-empty strings"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "justification": self.justification,
            "accepted_risks": self.accepted_risks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimulationOverride":
        """Construct and validate a SimulationOverride from a raw dict.

        Raises ValueError if constraints are not satisfied.
        """
        return cls(
            justification=str(data.get("justification", "")),
            accepted_risks=list(data.get("accepted_risks", [])),
        )


# ---------------------------------------------------------------------------
# Simulation result (structured output contract)
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Structured output of MUTATION_SIMULATION_EXECUTION_V1.

    Required fields per response contract:
      - impacted_files
      - risk_level
      - predicted_failures
      - safe_to_execute
      - reasoning_summary

    Always a simulation — NEVER an execution instruction.
    ``safe_to_execute=True`` means the simulation found no blocking conditions.
    ``safe_to_execute=False`` means execution MUST NOT proceed.
    """

    simulation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    governance_contract: str = "MUTATION_SIMULATION_EXECUTION_V1"
    source_contract_id: str = ""
    source_governance_audit_id: str = ""
    # Required output fields
    impacted_files: list[str] = field(default_factory=list)
    risk_level: str = RISK_LOW
    predicted_failures: list[dict[str, Any]] = field(default_factory=list)
    safe_to_execute: bool = False
    reasoning_summary: str = ""
    # Extended outputs (also serialised)
    impacted_modules: list[str] = field(default_factory=list)
    dependency_links: list[dict[str, str]] = field(default_factory=list)
    structural_impact: list[str] = field(default_factory=list)
    behavioral_impact: list[str] = field(default_factory=list)
    data_flow_impact: list[str] = field(default_factory=list)
    failure_types: list[str] = field(default_factory=list)
    risk_criteria_matched: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    override_used: bool = False
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Execution boundary — enforced constants
    execution_boundary: dict[str, bool] = field(
        default_factory=lambda: {
            "no_file_write": True,
            "no_git_commit": True,
            "no_deployment_trigger": True,
        }
    )
    # Per-file SHA-256 fingerprints; produced by simulation_gateway,
    # verified by bridge revalidation to detect proposal tampering.
    file_snapshot_hashes: dict[str, str] = field(default_factory=dict)

    def validate_for_return(self) -> None:
        """Raise ValueError if source_governance_audit_id is absent or empty."""
        if not self.source_governance_audit_id:
            raise ValueError(
                "INVALID_SIMULATION_RESULT: missing source_governance_audit_id"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "governance_contract": self.governance_contract,
            "source_contract_id": self.source_contract_id,
            "source_governance_audit_id": self.source_governance_audit_id,
            "impacted_files": self.impacted_files,
            "risk_level": self.risk_level,
            "predicted_failures": self.predicted_failures,
            "safe_to_execute": self.safe_to_execute,
            "reasoning_summary": self.reasoning_summary,
            "impacted_modules": self.impacted_modules,
            "dependency_links": self.dependency_links,
            "structural_impact": self.structural_impact,
            "behavioral_impact": self.behavioral_impact,
            "data_flow_impact": self.data_flow_impact,
            "failure_types": self.failure_types,
            "risk_criteria_matched": self.risk_criteria_matched,
            "blocked_reason": self.blocked_reason,
            "override_used": self.override_used,
            "audit_id": self.audit_id,
            "created_at": self.created_at,
            "execution_boundary": self.execution_boundary,
            "file_snapshot_hashes": self.file_snapshot_hashes,
        }


# ---------------------------------------------------------------------------
# Simulation audit record
# ---------------------------------------------------------------------------


@dataclass
class SimulationAuditRecord:
    """Full audit trail for a single simulation pipeline execution.

    All fields are mandatory per the audit_layer contract.
    """

    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    simulation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    mutation_contract: dict[str, Any] = field(default_factory=dict)
    simulation_outputs: dict[str, Any] = field(default_factory=dict)
    risk_level: str = ""
    decision: bool = False
    override_used: bool = False
    blocked_reason: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
