"""
backend.app.mutation_simulation.failure_prediction
====================================================
Failure prediction for MUTATION_SIMULATION_EXECUTION_V1.

Predicts possible failure modes caused by the mutation.

Failure types (per contract):
  - build_failure
  - runtime_failure
  - dependency_break
  - contract_violation

Requirements:
  - must include at least one alternative_scenario if any risk exists.
"""

from __future__ import annotations

from typing import Any

from .contract import (
    FAILURE_BUILD,
    FAILURE_CONTRACT_VIOLATION,
    FAILURE_DEPENDENCY_BREAK,
    FAILURE_RUNTIME,
    DependencySurface,
    FailurePrediction,
    ImpactAnalysis,
    PredictedFailure,
)

# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

_HIGH_SEVERITY_KEYWORDS: frozenset[str] = frozenset(
    {
        "crash",
        "data loss",
        "security",
        "auth",
        "break",
        "corrupt",
        "inject",
        "privilege",
        "bypass",
        "leak",
    }
)

_MEDIUM_SEVERITY_KEYWORDS: frozenset[str] = frozenset(
    {"fail", "error", "exception", "timeout", "unavailable", "missing", "invalid"}
)


def _risk_severity(risk_text: str) -> str:
    """Map a risk string to a severity level."""
    lower = risk_text.lower()
    if any(kw in lower for kw in _HIGH_SEVERITY_KEYWORDS):
        return "high"
    if any(kw in lower for kw in _MEDIUM_SEVERITY_KEYWORDS):
        return "medium"
    return "low"


def predict_failures(
    contract_dict: dict[str, Any],
    impact: ImpactAnalysis,
    surface: DependencySurface,
) -> FailurePrediction:
    """Predict failure modes for the proposed mutation.

    Parameters
    ----------
    contract_dict:
        The ``mutation_proposal`` dict from an approved MutationGovernanceResult.
    impact:
        Pre-computed impact analysis.
    surface:
        Pre-computed dependency surface.

    Returns
    -------
    FailurePrediction
        All predicted failures.  Every failure that carries any risk includes
        an ``alternative_scenario`` (requirement: must include at least one
        alternative if risk exists).
    """
    operation_type: str = contract_dict.get("operation_type", "")
    risks: list[str] = contract_dict.get("risks", [])
    missing_data: list[str] = contract_dict.get("missing_data", [])
    alternatives: list[str] = contract_dict.get("alternatives", [])

    predicted: list[PredictedFailure] = []
    seen_types: set[str] = set()

    # Build a default alternative scenario from the contract's own alternatives.
    default_alternative = alternatives[0] if alternatives else None

    # -----------------------------------------------------------------------
    # Build failures — compilation / import errors
    # -----------------------------------------------------------------------
    build_triggers: list[str] = [
        item
        for item in impact.structural_impact
        if any(
            kw in item
            for kw in (
                "import_error",
                "callers_may_break",
                "schema_break_risk",
                "interface_change_risk",
            )
        )
    ]
    if operation_type == "delete_file" or build_triggers:
        severity = "high" if operation_type == "delete_file" else "medium"
        predicted.append(
            PredictedFailure(
                failure_type=FAILURE_BUILD,
                description=(
                    "Mutation may cause build or import failures in dependent modules."
                    f" Triggers: {build_triggers or ['delete_file_operation']}"
                ),
                severity=severity,
                alternative_scenario=default_alternative,
            )
        )
        seen_types.add(FAILURE_BUILD)

    # -----------------------------------------------------------------------
    # Runtime failures — from explicit contract risks
    # -----------------------------------------------------------------------
    for risk in risks:
        risk_lower = risk.lower()
        # Skip generic "none" placeholders.
        if risk_lower in ("none", "n/a", "no risk"):
            continue
        severity = _risk_severity(risk)
        # Classify into runtime or contract-violation
        if any(
            kw in risk_lower
            for kw in ("caller", "exception", "crash", "error", "fail", "break")
        ):
            failure_type = FAILURE_RUNTIME
        elif any(
            kw in risk_lower
            for kw in ("contract", "schema", "invariant", "protocol", "interface")
        ):
            failure_type = FAILURE_CONTRACT_VIOLATION
        else:
            failure_type = FAILURE_RUNTIME

        predicted.append(
            PredictedFailure(
                failure_type=failure_type,
                description=f"Contract risk: {risk}",
                severity=severity,
                alternative_scenario=default_alternative,
            )
        )
        seen_types.add(failure_type)

    # -----------------------------------------------------------------------
    # Dependency break — if cross-module links exist
    # -----------------------------------------------------------------------
    if surface.dependency_links and operation_type in ("delete_file", "update_file"):
        affected_targets = {
            lnk["source"]
            for lnk in surface.dependency_links
        }
        predicted.append(
            PredictedFailure(
                failure_type=FAILURE_DEPENDENCY_BREAK,
                description=(
                    f"Mutation affects {len(surface.dependency_links)} dependency "
                    f"link(s) from {len(affected_targets)} file(s). Downstream "
                    "consumers may break if interfaces change."
                ),
                severity="medium" if len(surface.dependency_links) < 5 else "high",
                alternative_scenario=default_alternative,
            )
        )
        seen_types.add(FAILURE_DEPENDENCY_BREAK)

    # -----------------------------------------------------------------------
    # Contract violations — missing data / unknown dependencies
    # -----------------------------------------------------------------------
    real_missing = [
        m for m in missing_data if m.strip().lower() not in ("none", "n/a", "")
    ]
    if real_missing:
        predicted.append(
            PredictedFailure(
                failure_type=FAILURE_CONTRACT_VIOLATION,
                description=(
                    "Mutation contract has declared missing data, which may lead to "
                    f"incomplete execution and contract violations: {real_missing}"
                ),
                severity="medium",
                alternative_scenario=default_alternative,
            )
        )
        seen_types.add(FAILURE_CONTRACT_VIOLATION)

    # -----------------------------------------------------------------------
    # Ensure at least one failure entry exists (even if low risk)
    # -----------------------------------------------------------------------
    if not predicted:
        predicted.append(
            PredictedFailure(
                failure_type=FAILURE_RUNTIME,
                description=(
                    "No explicit risks identified; low probability of runtime impact."
                ),
                severity="low",
                alternative_scenario=None,
            )
        )
        seen_types.add(FAILURE_RUNTIME)

    return FailurePrediction(
        predicted_failures=predicted,
        failure_types=sorted(seen_types),
    )
