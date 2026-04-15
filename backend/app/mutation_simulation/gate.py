"""
backend.app.mutation_simulation.gate
======================================
Simulation decision gate for MUTATION_SIMULATION_EXECUTION_V1.

Rules enforced:
  - block_if risk_level == high AND no_override
  - block_if predicted_failures_unresolved (any high-severity failure without override)
  - block_if dependency_analysis_incomplete

Output: safe_to_execute (true / false)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import (
    RISK_HIGH,
    DependencySurface,
    FailurePrediction,
    RiskScore,
    SimulationOverride,
)


@dataclass
class SimulationGateResult:
    """Result of the simulation decision gate."""

    safe_to_execute: bool
    blocked_reason: str | None = None
    override_used: bool = False
    gate_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_to_execute": self.safe_to_execute,
            "blocked_reason": self.blocked_reason,
            "override_used": self.override_used,
            "gate_notes": self.gate_notes,
        }


def simulation_decision_gate(
    risk: RiskScore,
    failures: FailurePrediction,
    surface: DependencySurface,
    override: SimulationOverride | None = None,
) -> SimulationGateResult:
    """Determine whether the mutation is safe to proceed.

    Blocking rules (evaluated in order — first block wins):
      1. dependency_analysis_incomplete — block if surface.complete is False
      2. risk_level_high_no_override    — block if risk == high and no override
      3. predicted_failures_unresolved  — block if any high-severity failure
                                          exists and no override is provided

    Override protocol:
      - When override is provided with justification + accepted_risks it
        satisfies rules 2 and 3.
      - Rule 1 (incomplete dependency analysis) is NEVER overridable.

    Parameters
    ----------
    risk:
        Computed risk score.
    failures:
        Computed failure predictions.
    surface:
        Computed dependency surface.
    override:
        Optional caller-supplied override (required for high-risk mutations).

    Returns
    -------
    SimulationGateResult
        ``safe_to_execute=True`` means no blocking conditions found.
    """
    notes: list[str] = []
    override_active = (
        override is not None
        and bool(override.justification.strip())
        and bool(override.accepted_risks)
    )

    # -----------------------------------------------------------------------
    # Rule 1: dependency_analysis_incomplete (never overridable)
    # -----------------------------------------------------------------------
    if not surface.complete:
        return SimulationGateResult(
            safe_to_execute=False,
            blocked_reason=(
                "dependency_analysis_incomplete: "
                + (surface.incomplete_reason or "dependency_graph_unavailable")
            ),
            override_used=False,
            gate_notes=["block_if:dependency_analysis_incomplete"],
        )

    # -----------------------------------------------------------------------
    # Rule 2: risk_level == high AND no_override
    # -----------------------------------------------------------------------
    if risk.level == RISK_HIGH:
        if not override_active:
            return SimulationGateResult(
                safe_to_execute=False,
                blocked_reason=(
                    f"risk_level_high_no_override: risk_level={risk.level!r}; "
                    f"criteria={risk.criteria_matched}"
                ),
                override_used=False,
                gate_notes=["block_if:risk_level==high AND no_override"],
            )
        # Override present — note it and continue.
        notes.append(
            f"override_accepted_for_high_risk: justification={override.justification!r}"
        )

    # -----------------------------------------------------------------------
    # Rule 3: predicted_failures_unresolved (high-severity failures without override)
    # -----------------------------------------------------------------------
    high_failures = [
        f for f in failures.predicted_failures if f.severity == "high"
    ]
    if high_failures and not override_active:
        failure_descriptions = [f.description[:120] for f in high_failures]
        return SimulationGateResult(
            safe_to_execute=False,
            blocked_reason=(
                "predicted_failures_unresolved: "
                + "; ".join(failure_descriptions)
            ),
            override_used=False,
            gate_notes=["block_if:predicted_failures_unresolved"],
        )
    if high_failures and override_active:
        notes.append(
            f"override_accepted_for_{len(high_failures)}_high_severity_failure(s)"
        )

    override_used = override_active and (risk.level == RISK_HIGH or bool(high_failures))
    notes.append(f"risk_level:{risk.level}")
    notes.append(
        f"predicted_failures:{len(failures.predicted_failures)}_total"
        f"_{len(high_failures)}_high"
    )

    return SimulationGateResult(
        safe_to_execute=True,
        blocked_reason=None,
        override_used=override_used,
        gate_notes=notes,
    )
