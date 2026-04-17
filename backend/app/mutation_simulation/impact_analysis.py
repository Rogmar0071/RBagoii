"""
backend.app.mutation_simulation.impact_analysis
=================================================
Impact analysis for MUTATION_SIMULATION_EXECUTION_V1.

Determines structural, behavioral, and data-flow effects of the proposed
mutation.  Analysis is deterministic — no AI calls, no I/O.

Checks:
  - cross_module_effects
  - interface_changes
  - schema_break_risk
"""

from __future__ import annotations

from typing import Any

from .contract import DependencySurface, ImpactAnalysis

# ---------------------------------------------------------------------------
# Operation-type structural impact descriptors
# ---------------------------------------------------------------------------

_STRUCTURAL_IMPACT_BY_OP: dict[str, list[str]] = {
    "create_file": [
        "new_file_introduced",
        "module_surface_expanded",
    ],
    "update_file": [
        "existing_file_modified",
        "interface_may_change",
    ],
    "delete_file": [
        "file_removed_from_module_surface",
        "callers_may_break",
        "potential_import_errors",
    ],
}

# Extension-based structural risk hints
_SCHEMA_RISK_EXTENSIONS: frozenset[str] = frozenset(
    {".json", ".yaml", ".yml", ".proto", ".graphql"}
)
_INTERFACE_RISK_EXTENSIONS: frozenset[str] = frozenset({".py", ".kt", ".java", ".ts", ".js"})


def _extension(path: str) -> str:
    """Return the lowercased file extension of *path*."""
    idx = path.rfind(".")
    return path[idx:].lower() if idx != -1 else ""


def analyze_impact(
    contract_dict: dict[str, Any],
    surface: DependencySurface,
) -> ImpactAnalysis:
    """Determine the impact of the proposed mutation.

    Parameters
    ----------
    contract_dict:
        The ``mutation_proposal`` dict from an approved MutationGovernanceResult.
    surface:
        Pre-computed dependency surface.

    Returns
    -------
    ImpactAnalysis
        Populated structural, behavioral, and data-flow impact lists.
    """
    operation_type: str = contract_dict.get("operation_type", "")
    proposed_changes: str = contract_dict.get("proposed_changes", "")
    risks: list[str] = contract_dict.get("risks", [])
    target_files: list[str] = contract_dict.get("target_files", [])

    # -----------------------------------------------------------------------
    # Structural impact
    # -----------------------------------------------------------------------
    structural: list[str] = list(
        _STRUCTURAL_IMPACT_BY_OP.get(operation_type, ["unknown_operation_type"])
    )

    # Schema / interface break risk from file extensions
    for path in target_files:
        ext = _extension(path)
        if ext in _SCHEMA_RISK_EXTENSIONS:
            structural.append(f"schema_break_risk:{path}")
        if ext in _INTERFACE_RISK_EXTENSIONS and operation_type in (
            "update_file",
            "delete_file",
        ):
            structural.append(f"interface_change_risk:{path}")

    # Structural risk if multiple modules are impacted.
    if len(surface.impacted_modules) > 1:
        structural.append(f"cross_module_structural_impact:{len(surface.impacted_modules)}_modules")

    # -----------------------------------------------------------------------
    # Behavioral impact
    # -----------------------------------------------------------------------
    behavioral: list[str] = []

    changes_lower = proposed_changes.lower()
    if any(kw in changes_lower for kw in ("delete", "remove", "drop")):
        behavioral.append("removal_of_existing_behavior")
    if any(kw in changes_lower for kw in ("add", "insert", "create", "new")):
        behavioral.append("new_behavior_introduced")
    if any(kw in changes_lower for kw in ("modify", "update", "change", "refactor", "rename")):
        behavioral.append("existing_behavior_modified")
    if any(kw in changes_lower for kw in ("validation", "guard", "check", "enforce")):
        behavioral.append("validation_or_guard_affected")
    if any(kw in changes_lower for kw in ("auth", "permission", "access", "secret", "key")):
        behavioral.append("auth_or_access_control_affected")

    # If risks mention callers / breaking changes
    for risk in risks:
        risk_lower = risk.lower()
        if any(kw in risk_lower for kw in ("caller", "break", "incompatible", "backward")):
            behavioral.append("backward_compatibility_risk")
            break

    if not behavioral:
        behavioral.append("no_significant_behavioral_change_detected")

    # -----------------------------------------------------------------------
    # Data-flow impact
    # -----------------------------------------------------------------------
    data_flow: list[str] = []

    if surface.dependency_links:
        direct_count = sum(1 for lnk in surface.dependency_links if lnk.get("type") == "direct")
        indirect_count = sum(1 for lnk in surface.dependency_links if lnk.get("type") == "indirect")
        if direct_count:
            data_flow.append(f"direct_dependency_data_flow:{direct_count}_links")
        if indirect_count:
            data_flow.append(f"indirect_dependency_data_flow:{indirect_count}_links")

    if len(surface.impacted_modules) > 2:
        data_flow.append(f"cross_module_data_flow_risk:{len(surface.impacted_modules)}_modules")

    if not data_flow:
        data_flow.append("no_cross_module_data_flow_impact")

    return ImpactAnalysis(
        structural_impact=structural,
        behavioral_impact=behavioral,
        data_flow_impact=data_flow,
    )
