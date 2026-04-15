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

Block conditions (``block_if:dependency_graph_unavailable``):
  - target_files is absent or empty
  - any target_file is a blank or non-string entry
  - any target_file is outside the allowed path scope
"""

from __future__ import annotations

import os
from typing import Any

from .contract import DependencySurface

# ---------------------------------------------------------------------------
# Known module roots (mirrors ALLOWED_PATH_PREFIXES from governance)
# ---------------------------------------------------------------------------

_MODULE_ROOTS: tuple[str, ...] = ("backend/", "android/", "scripts/")

# Paths that are explicitly blocked from appearing in target_files.
_RESTRICTED_PATH_SEGMENTS: frozenset[str] = frozenset(
    {".env", "secrets", "infra/credentials"}
)

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


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _infer_module(file_path: str) -> str:
    """Convert a file path to a Python module identifier."""
    norm = file_path.replace("\\", "/").lstrip("/")
    for root in _MODULE_ROOTS:
        if norm.startswith(root):
            relative = norm[len(root):]
            break
    else:
        relative = norm
    base, _ = os.path.splitext(relative)
    return base.replace("/", ".")


def _is_within_allowed_scope(path: str) -> bool:
    """Return True when *path* starts with one of the allowed module roots."""
    norm = path.replace("\\", "/").lstrip("/")
    return any(norm.startswith(root) for root in _MODULE_ROOTS)


def _is_restricted(path: str) -> bool:
    """Return True when *path* contains a restricted segment."""
    norm = path.replace("\\", "/").lstrip("/")
    for segment in norm.split("/"):
        if segment in _RESTRICTED_PATH_SEGMENTS:
            return True
    for restricted in _RESTRICTED_PATH_SEGMENTS:
        if restricted in norm:
            return True
    return False


def _direct_dependencies(file_path: str) -> list[str]:
    """Return known direct dependency file paths for *file_path*.

    Uses the static lookup table for well-known files; returns an empty list
    for files not in the table (treated as isolated).
    """
    norm = file_path.replace("\\", "/").lstrip("/")
    return list(_KNOWN_BACKEND_DEPS.get(norm, []))


# ---------------------------------------------------------------------------
# Public surface mapper
# ---------------------------------------------------------------------------


def map_dependency_surface(contract_dict: dict[str, Any]) -> DependencySurface:
    """Map the full dependency surface for a validated mutation contract.

    Parameters
    ----------
    contract_dict:
        The ``mutation_proposal`` dict from an approved MutationGovernanceResult.

    Returns
    -------
    DependencySurface
        ``complete=False`` in any of these cases (triggers a simulation block):
          - target_files is absent, empty, or contains non-string entries
          - any target file path is outside the allowed scope
          - any target file path is on the restricted list
    """
    raw: Any = contract_dict.get("target_files")
    target_files: list[str] = raw if isinstance(raw, list) else []

    # -----------------------------------------------------------------------
    # Guard 1: target_files must be a non-empty list
    # -----------------------------------------------------------------------
    if not target_files:
        return DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )

    # -----------------------------------------------------------------------
    # Guard 2: each entry must be a non-blank string
    # -----------------------------------------------------------------------
    blank_entries = [e for e in target_files if not (isinstance(e, str) and e.strip())]
    if blank_entries:
        return DependencySurface(
            complete=False,
            incomplete_reason=(
                "dependency_graph_unavailable:target_files_contains_blank_entries"
            ),
        )

    # -----------------------------------------------------------------------
    # Guard 3: restricted paths must never be in the surface
    # -----------------------------------------------------------------------
    restricted = [p for p in target_files if _is_restricted(p)]
    if restricted:
        return DependencySurface(
            complete=False,
            incomplete_reason=(
                f"dependency_graph_unavailable:restricted_paths={restricted}"
            ),
        )

    # -----------------------------------------------------------------------
    # Guard 4: all paths must be within the allowed scope
    # -----------------------------------------------------------------------
    out_of_scope = [p for p in target_files if not _is_within_allowed_scope(p)]
    if out_of_scope:
        return DependencySurface(
            complete=False,
            incomplete_reason=(
                f"dependency_graph_unavailable:out_of_scope_paths={out_of_scope}"
            ),
        )

    # -----------------------------------------------------------------------
    # Build the full dependency surface
    # -----------------------------------------------------------------------
    all_files: set[str] = set()
    all_modules: set[str] = set()
    dependency_links: list[dict[str, str]] = []
    # Track files whose dependency mapping had no known records.
    unresolved_files: list[str] = []

    seen: set[str] = set(target_files)
    all_files.update(target_files)

    for target in target_files:
        module = _infer_module(target)
        if module:
            all_modules.add(module)

        direct_deps = _direct_dependencies(target)
        new_direct: list[str] = []

        # If the file has no known deps in the table AND it's a Python source
        # file (where we would expect imports), mark it as unresolved so the
        # risk scorer can treat it conservatively.
        norm = target.replace("\\", "/").lstrip("/")
        if not direct_deps and norm.endswith(".py"):
            unresolved_files.append(target)

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

    partially_resolved = bool(unresolved_files)
    return DependencySurface(
        impacted_files=sorted(all_files),
        impacted_modules=sorted(all_modules),
        dependency_links=dependency_links,
        complete=True,
        partially_resolved=partially_resolved,
        unresolved_files=unresolved_files,
    )
