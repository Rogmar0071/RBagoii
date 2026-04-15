"""
backend.app.mutation_simulation.dependency_surface
====================================================
Dependency surface mapping for MUTATION_SIMULATION_EXECUTION_V1.

Identifies all components (files, modules, dependency links) affected by
a proposed mutation.  Analysis is deterministic — no AI calls, no I/O.

Execution boundary:
  - no_file_write
  - no_git_commit
  - no_deployment_trigger

Block condition:
  - dependency_graph_unavailable (target_files empty or unresolvable)
"""

from __future__ import annotations

import os
from typing import Any

from .contract import DependencySurface

# ---------------------------------------------------------------------------
# Known module roots (mirrors ALLOWED_PATH_PREFIXES from governance)
# ---------------------------------------------------------------------------

_MODULE_ROOTS: tuple[str, ...] = ("backend/", "android/", "scripts/")

# Map file extensions to a language label used in dependency link descriptions.
_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".kt": "kotlin",
    ".java": "java",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
    ".md": "markdown",
}

# Inferred cross-module dependency heuristics.
# If a file in module A imports from module B, they share a dependency link.
# We encode known structural pairs within the backend package for the most
# common mutation targets.
_KNOWN_BACKEND_DEPS: dict[str, list[str]] = {
    "backend/app/main.py": [
        "backend/app/models.py",
        "backend/app/database.py",
        "backend/app/auth.py",
    ],
    "backend/app/models.py": ["backend/app/database.py"],
    "backend/app/mutation_governance/engine.py": [
        "backend/app/mutation_governance/contract.py",
        "backend/app/mutation_governance/gate.py",
        "backend/app/mutation_governance/validation.py",
        "backend/app/mutation_governance/audit.py",
        "backend/app/mode_engine.py",
    ],
    "backend/app/mutation_governance/audit.py": [
        "backend/app/models.py",
        "backend/app/database.py",
    ],
    "backend/app/mutation_simulation/engine.py": [
        "backend/app/mutation_simulation/contract.py",
        "backend/app/mutation_simulation/dependency_surface.py",
        "backend/app/mutation_simulation/impact_analysis.py",
        "backend/app/mutation_simulation/failure_prediction.py",
        "backend/app/mutation_simulation/risk_scoring.py",
        "backend/app/mutation_simulation/gate.py",
        "backend/app/mutation_simulation/audit.py",
        "backend/app/mutation_governance/contract.py",
    ],
}


def _infer_module(file_path: str) -> str:
    """Convert a file path to a Python module identifier."""
    norm = file_path.replace("\\", "/").lstrip("/")
    # Strip known roots and convert separators to dots.
    for root in _MODULE_ROOTS:
        if norm.startswith(root):
            relative = norm[len(root):]
            break
    else:
        relative = norm
    # Remove extension.
    base, _ = os.path.splitext(relative)
    return base.replace("/", ".")


def _direct_dependencies(file_path: str) -> list[str]:
    """Return known direct dependency file paths for *file_path*.

    Uses the static lookup table for well-known files; returns an empty list
    for files not in the table (treated as isolated).
    """
    norm = file_path.replace("\\", "/").lstrip("/")
    return list(_KNOWN_BACKEND_DEPS.get(norm, []))


def map_dependency_surface(contract_dict: dict[str, Any]) -> DependencySurface:
    """Map the full dependency surface for a validated mutation contract.

    Parameters
    ----------
    contract_dict:
        The ``mutation_proposal`` dict from an approved MutationGovernanceResult.

    Returns
    -------
    DependencySurface
        ``complete=False`` when target_files is absent or empty, which triggers
        a simulation block (``block_if:dependency_graph_unavailable``).
    """
    target_files: list[str] = contract_dict.get("target_files") or []

    if not target_files:
        return DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )

    all_files: set[str] = set()
    all_modules: set[str] = set()
    dependency_links: list[dict[str, str]] = []

    # Collect direct dependencies for each target file.
    seen: set[str] = set(target_files)
    all_files.update(target_files)

    for target in target_files:
        module = _infer_module(target)
        if module:
            all_modules.add(module)

        direct_deps = _direct_dependencies(target)
        new_direct: list[str] = []

        for dep in direct_deps:
            dep_module = _infer_module(dep)
            if dep_module:
                all_modules.add(dep_module)
            dependency_links.append(
                {"source": target, "target": dep, "type": "direct"}
            )
            if dep not in seen:
                seen.add(dep)
                all_files.add(dep)
                new_direct.append(dep)

        # Resolve one level of indirect dependencies from newly discovered direct deps.
        for dep in new_direct:
            for transitive in _KNOWN_BACKEND_DEPS.get(dep, []):
                transitive_module = _infer_module(transitive)
                if transitive_module:
                    all_modules.add(transitive_module)
                dependency_links.append(
                    {"source": target, "target": transitive, "type": "indirect"}
                )
                if transitive not in seen:
                    seen.add(transitive)
                    all_files.add(transitive)

    return DependencySurface(
        impacted_files=sorted(all_files),
        impacted_modules=sorted(all_modules),
        dependency_links=dependency_links,
        complete=True,
    )
