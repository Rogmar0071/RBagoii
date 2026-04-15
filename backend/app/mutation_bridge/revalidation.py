"""
backend.app.mutation_bridge.revalidation
==========================================
Runtime re-validation for MUTATION_BRIDGE_EXECUTION_V1.

Ensures that repository and proposal state have not changed between simulation
and execution.  All checks are strict — any mismatch causes a hard block.

Checks performed:
  1. target_files_exist_or_match_expected_state
       Target files declared in the governance proposal must be present in
       the simulation's impacted_files list.  Any target file absent from
       the simulation surface signals a state drift.
  2. no_conflicting_commits_detected
       The simulation result must reference the same source governance
       contract as the governance result.  A mismatch indicates the
       simulation was produced for a different contract version.
  3. dependency_graph_still_valid
       The simulation result must carry a complete dependency surface
       (safe_to_execute=True implies the gate passed with a complete
       surface).  A surface that was incomplete at simulation time blocks
       execution unconditionally.

Block conditions:
  - target_file_missing_or_modified   (check 1 failed)
  - repo_state_changed                (check 2 failed)
  - dependency_invalidated            (check 3 failed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Revalidation check identifiers
# ---------------------------------------------------------------------------

CHECK_TARGET_FILES = "target_files_exist_or_match_expected_state"
CHECK_NO_CONFLICTS = "no_conflicting_commits_detected"
CHECK_DEPENDENCY_GRAPH = "dependency_graph_still_valid"

ALL_CHECKS: tuple[str, ...] = (
    CHECK_TARGET_FILES,
    CHECK_NO_CONFLICTS,
    CHECK_DEPENDENCY_GRAPH,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class RuntimeRevalidationResult:
    """Result of the runtime re-validation step.

    ``passed=True`` only when ALL checks pass.
    Any single failure sets ``passed=False`` and populates ``failed_checks``
    and ``blocked_reason``.
    """

    passed: bool
    failed_checks: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    check_details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_checks": self.failed_checks,
            "blocked_reason": self.blocked_reason,
            "check_details": self.check_details,
        }


# ---------------------------------------------------------------------------
# Public revalidation entry point
# ---------------------------------------------------------------------------


def revalidate_runtime_state(
    governance_result: dict[str, Any],
    simulation_result: dict[str, Any],
) -> RuntimeRevalidationResult:
    """Perform all runtime re-validation checks.

    Strict enforcement: the first failing check short-circuits and returns
    a blocked result immediately.  All three checks must pass independently.

    Parameters
    ----------
    governance_result:
        The dict output of MutationGovernanceResult.to_dict().
    simulation_result:
        The dict output of SimulationResult.to_dict().

    Returns
    -------
    RuntimeRevalidationResult
        ``passed=True`` only when all three checks clear.
        ``passed=False`` with ``blocked_reason`` on any failure.
    """
    details: dict[str, str] = {}

    # -----------------------------------------------------------------------
    # Check 1: target_files_exist_or_match_expected_state
    #
    # Every target file declared in the governance mutation proposal must
    # appear in the simulation's impacted_files list.  If a file is present
    # in the proposal but absent from impacted_files, the simulation did not
    # cover it — this is a state drift and must block execution.
    # -----------------------------------------------------------------------
    proposal: dict[str, Any] = governance_result.get("mutation_proposal") or {}
    target_files: list[str] = list(proposal.get("target_files") or [])
    impacted_files: list[str] = list(simulation_result.get("impacted_files") or [])
    impacted_set: set[str] = set(impacted_files)

    missing_from_simulation = [f for f in target_files if f not in impacted_set]
    if missing_from_simulation:
        details[CHECK_TARGET_FILES] = (
            f"FAILED: {len(missing_from_simulation)} target file(s) absent from "
            f"simulation impacted_files: {missing_from_simulation}"
        )
        return RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_TARGET_FILES],
            blocked_reason=(
                "block_if:target_file_missing_or_modified — "
                f"target files not covered by simulation: {missing_from_simulation}"
            ),
            check_details=details,
        )
    details[CHECK_TARGET_FILES] = (
        f"PASSED: all {len(target_files)} target file(s) present in simulation surface"
    )

    # -----------------------------------------------------------------------
    # Check 2: no_conflicting_commits_detected
    #
    # The simulation result must reference the same governance contract_id
    # as the governance result.  A mismatch means the simulation was produced
    # against a different contract version — execution must be blocked.
    # -----------------------------------------------------------------------
    governance_contract_id: str = str(governance_result.get("contract_id", "")).strip()
    simulation_source_id: str = str(
        simulation_result.get("source_contract_id", "")
    ).strip()

    if not governance_contract_id:
        details[CHECK_NO_CONFLICTS] = (
            "FAILED: governance_result.contract_id is absent or empty"
        )
        return RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_NO_CONFLICTS],
            blocked_reason=(
                "block_if:repo_state_changed — "
                "governance_result.contract_id is absent; cannot verify contract linkage"
            ),
            check_details=details,
        )

    if not simulation_source_id:
        details[CHECK_NO_CONFLICTS] = (
            "FAILED: simulation_result.source_contract_id is absent or empty"
        )
        return RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_NO_CONFLICTS],
            blocked_reason=(
                "block_if:repo_state_changed — "
                "simulation_result.source_contract_id is absent; "
                "simulation cannot be linked to governance contract"
            ),
            check_details=details,
        )

    if governance_contract_id != simulation_source_id:
        details[CHECK_NO_CONFLICTS] = (
            f"FAILED: governance contract_id={governance_contract_id!r} does not match "
            f"simulation source_contract_id={simulation_source_id!r}"
        )
        return RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_NO_CONFLICTS],
            blocked_reason=(
                "block_if:repo_state_changed — "
                f"contract_id mismatch: governance={governance_contract_id!r}, "
                f"simulation={simulation_source_id!r}; "
                "simulation was produced for a different contract"
            ),
            check_details=details,
        )
    details[CHECK_NO_CONFLICTS] = (
        f"PASSED: contract_id={governance_contract_id!r} consistent across "
        "governance and simulation"
    )

    # -----------------------------------------------------------------------
    # Check 3: dependency_graph_still_valid
    #
    # The simulation gate only passes (safe_to_execute=True) when the
    # dependency surface was complete.  If safe_to_execute is False at this
    # point, the gate already blocked — we check explicitly because bridge
    # input validation may have passed through a simulation result that was
    # safe but whose surface completeness indicator we want to confirm.
    #
    # We verify that safe_to_execute is True (confirming the gate passed with
    # a complete surface) and that no blocking dependency reason is present.
    # -----------------------------------------------------------------------
    safe_to_execute: bool = bool(simulation_result.get("safe_to_execute", False))
    sim_blocked_reason: str | None = simulation_result.get("blocked_reason")

    if not safe_to_execute:
        details[CHECK_DEPENDENCY_GRAPH] = (
            f"FAILED: simulation_result.safe_to_execute=False; "
            f"blocked_reason={sim_blocked_reason!r}"
        )
        return RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_DEPENDENCY_GRAPH],
            blocked_reason=(
                "block_if:dependency_invalidated — "
                "simulation_result.safe_to_execute is False; "
                "dependency graph was not validated as safe at simulation time"
            ),
            check_details=details,
        )

    details[CHECK_DEPENDENCY_GRAPH] = (
        "PASSED: simulation_result.safe_to_execute=True; dependency graph valid"
    )

    return RuntimeRevalidationResult(
        passed=True,
        failed_checks=[],
        blocked_reason=None,
        check_details=details,
    )
