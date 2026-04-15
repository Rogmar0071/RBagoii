"""
backend.app.mutation_simulation.failure_prediction
====================================================
Failure prediction for MUTATION_SIMULATION_EXECUTION_V1.

Requirement: ALL four failure categories are evaluated for EVERY simulation.
Each category always produces at least one entry, even when no evidence of
risk in that category is found (severity=low, description="no_X_detected").

Failure types (per ALL_FAILURE_CATEGORIES):
  - build_failure
  - runtime_failure
  - dependency_break
  - contract_violation
"""

from __future__ import annotations

from typing import Any

from .contract import (
    ALL_FAILURE_CATEGORIES,
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


def _default_alternative(alternatives: list[str]) -> str | None:
    return alternatives[0] if alternatives else None


def predict_failures(
    contract_dict: dict[str, Any],
    impact: ImpactAnalysis,
    surface: DependencySurface,
) -> FailurePrediction:
    """Predict failure modes for the proposed mutation.

    ALL four failure categories are evaluated for every simulation:
      1. build_failure       — compilation/import failures
      2. runtime_failure     — execution-time failures from contract risks
      3. dependency_break    — cross-module consumer breakage
      4. contract_violation  — schema/protocol/missing-data violations

    Every category produces at least one entry.  If no evidence of risk is found
    in a category, a low-severity entry records that explicitly.

    Parameters
    ----------
    contract_dict:
        The mutation_proposal dict from an approved MutationGovernanceResult.
    impact:
        Pre-computed impact analysis.
    surface:
        Pre-computed dependency surface.

    Returns
    -------
    FailurePrediction
        Failures grouped by type; every category represented.
    """
    operation_type: str = contract_dict.get("operation_type", "")
    risks: list[str] = contract_dict.get("risks", [])
    missing_data: list[str] = contract_dict.get("missing_data", [])
    alternatives: list[str] = contract_dict.get("alternatives", [])

    default_alt = _default_alternative(alternatives)

    predicted: list[PredictedFailure] = []

    # -----------------------------------------------------------------------
    # Category 1: build_failure
    # -----------------------------------------------------------------------
    build_entries = _evaluate_build_failures(operation_type, impact, default_alt)
    predicted.extend(build_entries)

    # -----------------------------------------------------------------------
    # Category 2: runtime_failure
    # -----------------------------------------------------------------------
    runtime_entries = _evaluate_runtime_failures(risks, default_alt)
    predicted.extend(runtime_entries)

    # -----------------------------------------------------------------------
    # Category 3: dependency_break
    # -----------------------------------------------------------------------
    dep_entries = _evaluate_dependency_break(operation_type, surface, default_alt)
    predicted.extend(dep_entries)

    # -----------------------------------------------------------------------
    # Category 4: contract_violation
    # -----------------------------------------------------------------------
    cv_entries = _evaluate_contract_violations(risks, missing_data, surface, default_alt)
    predicted.extend(cv_entries)

    seen_types = {f.failure_type for f in predicted}
    return FailurePrediction(
        predicted_failures=predicted,
        failure_types=sorted(seen_types),
    )


# ---------------------------------------------------------------------------
# Category evaluators
# ---------------------------------------------------------------------------


def _evaluate_build_failures(
    operation_type: str,
    impact: ImpactAnalysis,
    default_alt: str | None,
) -> list[PredictedFailure]:
    """Evaluate build_failure category."""
    build_triggers = [
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

    if operation_type == "delete_file":
        return [
            PredictedFailure(
                failure_type=FAILURE_BUILD,
                description=(
                    "delete_file operation will remove the file from the import "
                    "graph. Any module that imports it will raise ImportError at "
                    "build or startup time."
                ),
                severity="high",
                alternative_scenario=default_alt,
            )
        ]

    if build_triggers:
        return [
            PredictedFailure(
                failure_type=FAILURE_BUILD,
                description=(
                    "Structural impact items suggest potential build/import failures. "
                    f"Triggers: {build_triggers}"
                ),
                severity="medium",
                alternative_scenario=default_alt,
            )
        ]

    return [
        PredictedFailure(
            failure_type=FAILURE_BUILD,
            description=(
                "No build failure indicators detected for this operation type "
                f"({operation_type!r}). Low probability of compile/import errors."
            ),
            severity="low",
            alternative_scenario=None,
        )
    ]


def _evaluate_runtime_failures(
    risks: list[str],
    default_alt: str | None,
) -> list[PredictedFailure]:
    """Evaluate runtime_failure category from contract risks."""
    real_risks = [r for r in risks if r.strip().lower() not in ("none", "n/a", "no risk")]
    if not real_risks:
        return [
            PredictedFailure(
                failure_type=FAILURE_RUNTIME,
                description=(
                    "No explicit runtime risks declared in contract. "
                    "Low probability of runtime failure."
                ),
                severity="low",
                alternative_scenario=None,
            )
        ]

    entries: list[PredictedFailure] = []
    for risk in real_risks:
        risk_lower = risk.lower()
        if any(
            kw in risk_lower
            for kw in ("contract", "schema", "invariant", "protocol", "interface")
        ):
            # These belong to contract_violation, skip here.
            continue
        severity = _risk_severity(risk)
        entries.append(
            PredictedFailure(
                failure_type=FAILURE_RUNTIME,
                description=f"Contract risk: {risk}",
                severity=severity,
                alternative_scenario=default_alt,
            )
        )

    if not entries:
        entries.append(
            PredictedFailure(
                failure_type=FAILURE_RUNTIME,
                description=(
                    "Declared risks are categorised as contract violations; "
                    "no distinct runtime failure evidence."
                ),
                severity="low",
                alternative_scenario=None,
            )
        )

    return entries


def _evaluate_dependency_break(
    operation_type: str,
    surface: DependencySurface,
    default_alt: str | None,
) -> list[PredictedFailure]:
    """Evaluate dependency_break category."""
    if surface.partially_resolved:
        return [
            PredictedFailure(
                failure_type=FAILURE_DEPENDENCY_BREAK,
                description=(
                    f"{len(surface.unresolved_files)} file(s) have no recorded "
                    "dependency mapping: their consumers are unknown and could break "
                    f"silently. Unresolved: {surface.unresolved_files}"
                ),
                severity="medium",
                alternative_scenario=default_alt,
            )
        ]

    if surface.dependency_links and operation_type in ("delete_file", "update_file"):
        affected = {lnk["source"] for lnk in surface.dependency_links}
        return [
            PredictedFailure(
                failure_type=FAILURE_DEPENDENCY_BREAK,
                description=(
                    f"Mutation affects {len(surface.dependency_links)} dependency "
                    f"link(s) from {len(affected)} source file(s). Downstream "
                    "consumers may break if interfaces change."
                ),
                severity="medium" if len(surface.dependency_links) < 5 else "high",
                alternative_scenario=default_alt,
            )
        ]

    return [
        PredictedFailure(
            failure_type=FAILURE_DEPENDENCY_BREAK,
            description=(
                "No dependency break indicators detected. "
                f"Operation type is {operation_type!r} with "
                f"{len(surface.dependency_links)} dependency link(s). "
                "Low probability of downstream consumer breakage."
            ),
            severity="low",
            alternative_scenario=None,
        )
    ]


def _evaluate_contract_violations(
    risks: list[str],
    missing_data: list[str],
    surface: DependencySurface,
    default_alt: str | None,
) -> list[PredictedFailure]:
    """Evaluate contract_violation category."""
    entries: list[PredictedFailure] = []

    # Contract-related risks
    contract_risks = [
        r for r in risks
        if r.strip().lower() not in ("none", "n/a", "no risk")
        and any(
            kw in r.lower()
            for kw in ("contract", "schema", "invariant", "protocol", "interface")
        )
    ]
    for risk in contract_risks:
        entries.append(
            PredictedFailure(
                failure_type=FAILURE_CONTRACT_VIOLATION,
                description=f"Contract/schema risk: {risk}",
                severity=_risk_severity(risk),
                alternative_scenario=default_alt,
            )
        )

    # Missing data violations
    real_missing = [
        m for m in missing_data if m.strip().lower() not in ("none", "n/a", "")
    ]
    if real_missing:
        entries.append(
            PredictedFailure(
                failure_type=FAILURE_CONTRACT_VIOLATION,
                description=(
                    "Mutation contract has declared missing data — execution with "
                    f"incomplete information may produce contract violations: {real_missing}"
                ),
                severity="medium",
                alternative_scenario=default_alt,
            )
        )

    if not entries:
        entries.append(
            PredictedFailure(
                failure_type=FAILURE_CONTRACT_VIOLATION,
                description=(
                    "No contract violation indicators detected. "
                    "No missing data declared and no schema/protocol risks found."
                ),
                severity="low",
                alternative_scenario=None,
            )
        )

    return entries
