"""
backend.app.mutation_simulation.gate
======================================
Simulation decision gate for MUTATION_SIMULATION_EXECUTION_V1.

Rules enforced (HARD BLOCK — not advisory):
  - block_if dependency_analysis_incomplete         (never overridable)
  - block_if risk_level == high AND no_override
  - block_if predicted_failures_unresolved          (high-severity failures without override)

Override protocol (required for high-risk):
  - justification must be a non-blank string of at least
    OVERRIDE_MIN_JUSTIFICATION_LENGTH characters.
  - accepted_risks must be a non-empty list of non-empty strings.
  - An override that does not satisfy both constraints is treated as absent.

Output: safe_to_execute (true / false) — this is a hard gate, not advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import (
    OVERRIDE_MIN_JUSTIFICATION_LENGTH,
    RISK_HIGH,
    DependencySurface,
    FailurePrediction,
    RiskScore,
    SimulationOverride,
)


@dataclass
class SimulationGateResult:
    """Result of the simulation decision gate.

    ``safe_to_execute=False`` is a HARD BLOCK.  The engine must never
    return a simulation result with ``safe_to_execute=True`` when this
    flag is False.  ``blocking_mode=True`` is always set on this
    implementation to make the contract explicit.
    """

    safe_to_execute: bool
    blocked_reason: str | None = None
    override_used: bool = False
    gate_notes: list[str] = field(default_factory=list)
    # Invariant: always True — the gate is never advisory.
    blocking_mode: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_to_execute": self.safe_to_execute,
            "blocked_reason": self.blocked_reason,
            "override_used": self.override_used,
            "gate_notes": self.gate_notes,
            "blocking_mode": self.blocking_mode,
        }


def _override_is_valid(override: SimulationOverride | None) -> bool:
    """Return True only when the override satisfies all protocol requirements.

    Requirements:
      - override is not None
      - justification is a non-blank string >= OVERRIDE_MIN_JUSTIFICATION_LENGTH chars
      - accepted_risks is a non-empty list of non-empty strings
    """
    if override is None:
        return False
    jstr = override.justification.strip() if isinstance(override.justification, str) else ""
    if len(jstr) < OVERRIDE_MIN_JUSTIFICATION_LENGTH:
        return False
    if (
        not isinstance(override.accepted_risks, list)
        or not override.accepted_risks
        or not all(isinstance(r, str) and r.strip() for r in override.accepted_risks)
    ):
        return False
    return True


def simulation_decision_gate(
    risk: RiskScore,
    failures: FailurePrediction,
    surface: DependencySurface,
    override: SimulationOverride | None = None,
) -> SimulationGateResult:
    """Determine whether the mutation is safe to proceed.

    This gate is BLOCKING — ``safe_to_execute=False`` is a hard stop.

    Blocking rules (evaluated in order — first block wins):
      1. dependency_analysis_incomplete — block if surface.complete is False
         (NEVER overridable — incomplete analysis cannot be waived)
      2. risk_level_high_no_override    — block if risk == high and override
         does not satisfy the override protocol
      3. predicted_failures_unresolved  — block if any high-severity failure
         exists and override does not satisfy the override protocol

    Override protocol:
      - justification: non-empty string of at least
        OVERRIDE_MIN_JUSTIFICATION_LENGTH characters.
      - accepted_risks: non-empty list of non-empty strings.
      - An override that fails either check is treated as absent.

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
        ``blocking_mode`` is always ``True`` — this gate is never advisory.
    """
    notes: list[str] = []
    override_valid = _override_is_valid(override)

    # -----------------------------------------------------------------------
    # Rule 1: dependency_analysis_incomplete (NEVER overridable)
    # -----------------------------------------------------------------------
    if not surface.complete:
        return SimulationGateResult(
            safe_to_execute=False,
            blocked_reason=(
                "dependency_analysis_incomplete: "
                + (surface.incomplete_reason or "dependency_graph_unavailable")
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:dependency_analysis_incomplete"],
        )

    # -----------------------------------------------------------------------
    # Rule 2: risk_level == high AND override does not satisfy protocol
    # -----------------------------------------------------------------------
    if risk.level == RISK_HIGH:
        if not override_valid:
            override_note = (
                "no override provided"
                if override is None
                else "override rejected: justification too short or accepted_risks empty"
            )
            return SimulationGateResult(
                safe_to_execute=False,
                blocked_reason=(
                    f"risk_level_high_no_override: risk_level={risk.level!r}; "
                    f"criteria={risk.criteria_matched}; {override_note}"
                ),
                override_used=False,
                gate_notes=["HARD_BLOCK:risk_level==high AND no_valid_override"],
            )
        notes.append(f"override_accepted_for_high_risk: justification={override.justification!r}")

    # -----------------------------------------------------------------------
    # Rule 3: predicted_failures_unresolved
    #         (high-severity failures without a valid override)
    # -----------------------------------------------------------------------
    high_failures = [f for f in failures.predicted_failures if f.severity == "high"]
    if high_failures and not override_valid:
        descriptions = "; ".join(f.description[:120] for f in high_failures)
        return SimulationGateResult(
            safe_to_execute=False,
            blocked_reason=(
                f"predicted_failures_unresolved ({len(high_failures)} high-severity): "
                + descriptions
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:predicted_failures_unresolved"],
        )
    if high_failures and override_valid:
        notes.append(f"override_accepted_for_{len(high_failures)}_high_severity_failure(s)")

    override_used = override_valid and (risk.level == RISK_HIGH or bool(high_failures))
    notes.append(f"risk_level:{risk.level}")
    notes.append(
        f"predicted_failures:{len(failures.predicted_failures)}_total_{len(high_failures)}_high"
    )

    return SimulationGateResult(
        safe_to_execute=True,
        blocked_reason=None,
        override_used=override_used,
        gate_notes=notes,
    )
