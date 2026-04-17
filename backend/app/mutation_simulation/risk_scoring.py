"""
backend.app.mutation_simulation.risk_scoring
=============================================
Deterministic risk scorer for MUTATION_SIMULATION_EXECUTION_V1.

Risk levels:
  low    — isolated_change, no_dependency_impact
  medium — limited_dependency_impact, reversible_changes,
           or partially_resolved surface (unknown deps cannot be assumed safe)
  high   — cross_module_impact, structural_changes, unknown_dependencies,
           incomplete surface

Completeness contract enforcement:
  - surface.complete=False   → always HIGH (unknown_dependencies:incomplete_surface)
  - surface.partially_resolved=True → at least MEDIUM (unresolved_deps_present)
    A partially-resolved surface means some Python source files had no dependency
    records; the scorer must not classify them as isolated/low-risk.
"""

from __future__ import annotations

from .contract import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    DependencySurface,
    FailurePrediction,
    ImpactAnalysis,
    RiskScore,
)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_HIGH_MODULE_THRESHOLD = 3  # >= this many impacted modules → elevated risk
_HIGH_DEP_LINK_THRESHOLD = 5  # >= this many dep links → elevated risk


def score_risk(
    surface: DependencySurface,
    impact: ImpactAnalysis,
    failures: FailurePrediction,
) -> RiskScore:
    """Assign a deterministic risk level to the proposed mutation.

    Evaluation order: HIGH → MEDIUM → LOW.
    The first matching level wins.

    High criteria (any one sufficient):
      - unknown_dependencies:incomplete_surface   (complete=False)
      - cross_module_impact                       (modules >= threshold)
      - structural_changes                        (delete/schema/cross-module)
      - high_severity_predicted_failure
      - cross_module_impact:dep_links             (links >= threshold)

    Medium criteria (any one sufficient, no high criterion matched):
      - unresolved_deps_present                   (partially_resolved=True)
      - limited_dependency_impact
      - dependency_links_present
      - medium_severity_predicted_failure

    Low (default): complete surface, no unresolved deps, no dependency impact.
    """
    criteria: list[str] = []

    # -----------------------------------------------------------------------
    # High criteria
    # -----------------------------------------------------------------------
    high_triggers: list[str] = []

    if not surface.complete:
        high_triggers.append("unknown_dependencies:incomplete_surface")

    if len(surface.impacted_modules) >= _HIGH_MODULE_THRESHOLD:
        high_triggers.append(f"cross_module_impact:{len(surface.impacted_modules)}_modules")

    if any(
        kw in item
        for item in impact.structural_impact
        for kw in (
            "delete_file",
            "schema_break_risk",
            "callers_may_break",
            "cross_module_structural_impact",
        )
    ):
        high_triggers.append("structural_changes:high_impact_structural_item")

    if any(f.severity == "high" for f in failures.predicted_failures):
        high_triggers.append("high_severity_predicted_failure")

    if len(surface.dependency_links) >= _HIGH_DEP_LINK_THRESHOLD:
        high_triggers.append(
            f"cross_module_impact:{len(surface.dependency_links)}_dependency_links"
        )

    if high_triggers:
        criteria.extend(high_triggers)
        return RiskScore(level=RISK_HIGH, criteria_matched=criteria)

    # -----------------------------------------------------------------------
    # Medium criteria
    # -----------------------------------------------------------------------
    medium_triggers: list[str] = []

    # Partially-resolved surface: some files had no dependency records.
    # Cannot assume isolated — must treat as at least medium risk.
    if surface.partially_resolved:
        medium_triggers.append(
            f"unresolved_deps_present:{len(surface.unresolved_files)}_file(s)"
            f"_with_no_known_dependency_record"
        )

    if len(surface.impacted_modules) > 1:
        medium_triggers.append(f"limited_dependency_impact:{len(surface.impacted_modules)}_modules")

    if surface.dependency_links:
        medium_triggers.append(f"dependency_links_present:{len(surface.dependency_links)}")

    if any(f.severity == "medium" for f in failures.predicted_failures):
        medium_triggers.append("medium_severity_predicted_failure")

    if medium_triggers:
        criteria.extend(medium_triggers)
        return RiskScore(level=RISK_MEDIUM, criteria_matched=criteria)

    # -----------------------------------------------------------------------
    # Low (default) — complete surface, fully resolved, no dependency impact.
    # -----------------------------------------------------------------------
    criteria.append("isolated_change:no_dependency_impact")
    return RiskScore(level=RISK_LOW, criteria_matched=criteria)
