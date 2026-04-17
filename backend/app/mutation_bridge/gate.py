"""
backend.app.mutation_bridge.gate
==================================
Execution gate for MUTATION_BRIDGE_EXECUTION_V1.

This gate is a HARD BLOCK — not advisory.  All three conditions must pass
before execution may proceed.  Any single failure immediately blocks.

Conditions required:
  1. governance_passed      — governance_result.gate_result.passed == True
  2. simulation_safe        — simulation_result.safe_to_execute == True
  3. runtime_revalidation_passed — RuntimeRevalidationResult.passed == True

Override rules (high_risk only):
  - risk_level == "high" requires a valid BridgeExecutionOverride.
  - A valid override requires: explicit_approval=True, justification of at
    least BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH characters, and a
    non-empty accepted_risks list.
  - An override that fails any constraint is treated as absent.

Block conditions:
  - governance_not_verified
  - simulation_not_verified
  - runtime_revalidation_failed
  - high_risk_without_override
  - unresolved_failures_present
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import (
    BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH,
    BridgeExecutionOverride,
)
from .revalidation import RuntimeRevalidationResult

# Risk level constant (mirrors simulation contract values)
_RISK_HIGH = "high"


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------


@dataclass
class BridgeGateResult:
    """Result of the bridge execution gate.

    ``passed=True`` only when all three conditions clear.
    ``blocking_mode=True`` is always set — this gate is never advisory.
    """

    passed: bool
    blocked_reason: str | None = None
    override_used: bool = False
    gate_notes: list[str] = field(default_factory=list)
    # Invariant: always True — the gate is never advisory.
    blocking_mode: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked_reason": self.blocked_reason,
            "override_used": self.override_used,
            "gate_notes": self.gate_notes,
            "blocking_mode": self.blocking_mode,
        }


# ---------------------------------------------------------------------------
# Override validity helper
# ---------------------------------------------------------------------------


def _override_is_valid(override: BridgeExecutionOverride | None) -> bool:
    """Return True only when the override satisfies all protocol requirements.

    Requirements:
      - override is not None
      - explicit_approval is True
      - justification is a non-blank string >= BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH
      - accepted_risks is a non-empty list of non-empty strings
    """
    if override is None:
        return False
    if not override.explicit_approval:
        return False
    jstr = override.justification.strip() if isinstance(override.justification, str) else ""
    if len(jstr) < BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH:
        return False
    if (
        not isinstance(override.accepted_risks, list)
        or not override.accepted_risks
        or not all(isinstance(r, str) and r.strip() for r in override.accepted_risks)
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Public gate function
# ---------------------------------------------------------------------------


def bridge_execution_gate(
    governance_result: dict[str, Any],
    simulation_result: dict[str, Any],
    revalidation_result: RuntimeRevalidationResult,
    override: BridgeExecutionOverride | None = None,
) -> BridgeGateResult:
    """Determine whether the bridge execution may proceed.

    This gate is BLOCKING — ``passed=False`` is a hard stop.  The engine
    must never return a BridgeResult with status="executed" when this
    gate returns passed=False.

    Blocking rules (evaluated in order — first block wins):
      1. governance_not_verified
             governance_result.gate_result.passed must be True
      2. simulation_not_verified
             simulation_result.safe_to_execute must be True
      3. runtime_revalidation_failed
             revalidation_result.passed must be True
      4. high_risk_without_override
             risk_level == "high" requires a valid BridgeExecutionOverride
      5. unresolved_failures_present
             execution_without_simulation is prohibited (safe_to_execute
             acts as the resolved-failures flag from the simulation gate)

    Parameters
    ----------
    governance_result:
        The dict output of MutationGovernanceResult.to_dict().
    simulation_result:
        The dict output of SimulationResult.to_dict().
    revalidation_result:
        Result from revalidate_runtime_state().
    override:
        Optional caller-supplied override (required when risk_level == "high").

    Returns
    -------
    BridgeGateResult
        ``passed=True`` means all conditions clear and execution may proceed.
        ``blocking_mode`` is always ``True`` — this gate is never advisory.
    """
    notes: list[str] = []
    override_valid = _override_is_valid(override)

    # -----------------------------------------------------------------------
    # Condition 1: governance_passed
    # -----------------------------------------------------------------------
    gate_result: Any = governance_result.get("gate_result")
    governance_gate_passed: bool = (
        isinstance(gate_result, dict) and gate_result.get("passed") is True
    )
    if not governance_gate_passed:
        return BridgeGateResult(
            passed=False,
            blocked_reason=(
                "block_if:governance_not_verified — "
                f"governance_result.gate_result.passed is not True; "
                f"gate_result={gate_result!r}"
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:governance_not_verified"],
        )
    notes.append("governance_passed:True")

    # -----------------------------------------------------------------------
    # Condition 2: simulation_safe
    # -----------------------------------------------------------------------
    simulation_safe: bool = bool(simulation_result.get("safe_to_execute", False))
    if not simulation_safe:
        return BridgeGateResult(
            passed=False,
            blocked_reason=(
                "block_if:simulation_not_verified — "
                "simulation_result.safe_to_execute is False; "
                f"blocked_reason={simulation_result.get('blocked_reason')!r}"
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:simulation_not_verified"],
        )
    notes.append("simulation_safe:True")

    # -----------------------------------------------------------------------
    # Condition 3: runtime_revalidation_passed
    # -----------------------------------------------------------------------
    if not revalidation_result.passed:
        return BridgeGateResult(
            passed=False,
            blocked_reason=(
                f"block_if:runtime_revalidation_failed — {revalidation_result.blocked_reason}"
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:runtime_revalidation_failed"],
        )
    notes.append("runtime_revalidation_passed:True")

    # -----------------------------------------------------------------------
    # Condition 4: high_risk_without_override
    # -----------------------------------------------------------------------
    risk_level: str = str(simulation_result.get("risk_level", "")).strip()
    if risk_level == _RISK_HIGH:
        if not override_valid:
            override_note = (
                "no override provided"
                if override is None
                else (
                    "override rejected: explicit_approval=False"
                    if not (override.explicit_approval if override else False)
                    else "override rejected: justification too short or accepted_risks empty"
                )
            )
            return BridgeGateResult(
                passed=False,
                blocked_reason=(
                    f"block_if:high_risk_without_override — "
                    f"risk_level={risk_level!r}; {override_note}"
                ),
                override_used=False,
                gate_notes=["HARD_BLOCK:high_risk_without_override"],
            )
        notes.append(f"override_accepted_for_high_risk: justification={override.justification!r}")

    # -----------------------------------------------------------------------
    # Condition 5: unresolved_failures_present
    # (simulation_result.safe_to_execute already enforces this; this check
    #  verifies the bridge-specific invariant that execution_without_simulation
    #  is prohibited by confirming the simulation contract field is present)
    # -----------------------------------------------------------------------
    sim_contract: str = str(simulation_result.get("governance_contract", "")).strip()
    if sim_contract != "MUTATION_SIMULATION_EXECUTION_V1":
        return BridgeGateResult(
            passed=False,
            blocked_reason=(
                "block_if:execution_without_simulation — "
                f"simulation_result.governance_contract={sim_contract!r}; "
                "expected 'MUTATION_SIMULATION_EXECUTION_V1'. "
                "Execution requires a verified simulation result."
            ),
            override_used=False,
            gate_notes=["HARD_BLOCK:execution_without_simulation"],
        )
    notes.append(f"simulation_contract_verified:{sim_contract!r}")

    override_used = override_valid and risk_level == _RISK_HIGH
    notes.append(f"risk_level:{risk_level}")

    return BridgeGateResult(
        passed=True,
        blocked_reason=None,
        override_used=override_used,
        gate_notes=notes,
    )
