"""
backend.app.mutation_governance.gate
=====================================
Mutation enforcement gate for MUTATION_GOVERNANCE_EXECUTION_V1.

Rules enforced:
  - no_execution_without_validation
  - no_direct_repo_modification
  - proposals_only_at_this_phase

Block conditions:
  - validation_failed   (any validation stage did not pass)
  - missing_required_fields (structural stage failure)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import MutationValidationResult


@dataclass
class GateResult:
    """Result of the mutation enforcement gate."""

    passed: bool
    blocked_reason: str | None = None
    failed_stages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked_reason": self.blocked_reason,
            "failed_stages": self.failed_stages,
        }


def mutation_enforcement_gate(
    validation_results: list[MutationValidationResult],
) -> GateResult:
    """Evaluate all validation results and determine whether the mutation
    proposal may proceed.

    Contract invariants enforced:
      - ``no_execution_without_validation``: ALL validation stages must pass.
      - ``no_direct_repo_modification``:     only proposals are returned; this
        gate blocks any path that would lead to actual file writes.
      - ``proposals_only_at_this_phase``:    execution boundary is maintained.

    Parameters
    ----------
    validation_results:
        Results from all validation stages (structural → logical → scope).

    Returns
    -------
    GateResult
        ``passed=True`` when all stages passed; ``passed=False`` with a
        ``blocked_reason`` summarising which stages and rules failed.
    """
    failed_stages = [vr.stage for vr in validation_results if not vr.passed]

    if not failed_stages:
        return GateResult(passed=True)

    all_failed_rules = [
        rule
        for vr in validation_results
        if not vr.passed
        for rule in vr.failed_rules
    ]
    # Truncate to a reasonable length for the reason string.
    rule_sample = ", ".join(all_failed_rules[:8])
    blocked_reason = (
        f"validation_failed: stages={failed_stages!r}; rules=[{rule_sample}]"
    )
    return GateResult(
        passed=False,
        blocked_reason=blocked_reason,
        failed_stages=failed_stages,
    )
