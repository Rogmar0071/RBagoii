"""
backend.app.graph_reality_validator
=====================================
MQP-CONTRACT: GRAPH-REALITY-VALIDATION v1.0
MQP-CONTRACT: GRAPH-ADVERSARIAL-VALIDATION v1.0

Validates graph correctness for a fully-ingested repository.

Produces a structured stats report covering:
  • file, symbol, dependency, call-edge and entry-point counts
  • import resolution stats (resolved / dropped)
  • symbol resolution stats (resolved / dropped / ambiguous)
  • execution-path validation (reachable paths / broken paths)
  • failure analysis (adversarial contract extension):
      - lost_valid_edges  : dropped calls where the callee IS a known repo symbol
      - alias_failures    : alias imports (import X as Y) where X resolves but
                            resolution fails because of the " as Y" suffix
      - ambiguous_drops   : calls dropped due to same-name symbol collision
      - missing_entry_points : files with undetected entry-point patterns

NO silent drops — every failure is recorded with its reason.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_repo_graph(
    repo_id: str,
    session: Any,
    *,
    manifest_files: List[Dict[str, str]],
) -> Dict[str, Any]:
    """
    Validate the graph for a single ingested repository.

    Parameters
    ----------
    repo_id:
        UUID string of the ``IngestJob`` that acted as the repo root.
    session:
        Active SQLModel / SQLAlchemy session with all graph rows committed.
    manifest_files:
        The original list of ``{"path": str, "content": str}`` dicts from
        the ingest manifest — needed to recompute raw-import counts for the
        "resolved vs. dropped" stats without going back to the network.

    Returns
    -------
    dict with shape::

        {
            "repo": str,
            "files": int,
            "symbols": int,
            "dependencies": int,
            "call_edges": int,
            "entry_points": int,

            "resolution_stats": {
                "resolved": int,        # raw imports that became FileDependency rows
                "dropped": int,         # raw imports silently dropped (external/unresolved)
                "ambiguous": int,       # symbol calls with >1 global candidate, no import path
            },

            "execution_validation": {
                "paths_found": int,     # entry-point chains with at least one node
                "broken_paths": int,    # entry-points whose chain is empty
            },

            "failure_analysis": {
                "lost_valid_edges": int,   # dropped calls where callee IS a known repo symbol
                "alias_failures": int,     # alias imports (X as Y) whose module resolves
                                           # but the " as Y" suffix breaks resolution
                "ambiguous_drops": int,    # calls dropped due to multi-candidate collision
                "missing_entry_points": int,  # files with entry-point patterns not in DB
            },

            "drop_log": [               # one entry per dropped import
                {"file": str, "import": str, "reason": str}
            ],
            "ambiguity_log": [          # one entry per ambiguous symbol call
                {"caller_file": str, "callee_name": str, "candidates": [str]}
            ],
            "broken_path_log": [        # one entry per empty execution chain
                {"entry_point_id": str, "file": str, "reason": str}
            ],
            "alias_failure_log": [      # one entry per detected alias import failure
                {"file": str, "import": str, "alias": str, "module": str}
            ],
            "missing_entry_point_log": [  # one entry per file with undetected entry point
                {"file": str, "pattern": str}
            ],
        }
    """
    import uuid

    from sqlmodel import select

    from backend.app.execution_reconstruction import reconstruct_execution
    from backend.app.graph_extractor import (
        extract_graph,
        extract_symbol_calls,
        resolve_import,
    )
    from backend.app.models import (
        CodeSymbol,
        EntryPoint,
        FileDependency,
        RepoFile,
        SymbolCallEdge,
    )

    repo_uuid = uuid.UUID(repo_id)

    # ------------------------------------------------------------------
    # 1. Load persisted graph rows for this repo
    # ------------------------------------------------------------------
    repo_files = list(session.exec(
        select(RepoFile).where(RepoFile.repo_id == repo_uuid)
    ))
    repo_file_ids = {rf.id for rf in repo_files}
    file_by_id: Dict[Any, RepoFile] = {rf.id: rf for rf in repo_files}
    file_by_path: Dict[str, RepoFile] = {rf.path: rf for rf in repo_files}

    symbols: list[CodeSymbol] = [
        s for s in session.exec(select(CodeSymbol))
        if s.file_id in repo_file_ids
    ]

    deps: list[FileDependency] = [
        d for d in session.exec(select(FileDependency))
        if d.source_file_id in repo_file_ids
    ]

    edges: list[SymbolCallEdge] = [
        e for e in session.exec(select(SymbolCallEdge))
        if e.source_symbol_id in {s.id for s in symbols}
    ]

    eps: list[EntryPoint] = [
        ep for ep in session.exec(select(EntryPoint))
        if ep.file_id in repo_file_ids
    ]

    # ------------------------------------------------------------------
    # 2. Import resolution stats
    #    Recompute from the original manifest so we can count "dropped"
    #    without touching the network.
    # ------------------------------------------------------------------
    all_paths = frozenset(rf.path for rf in repo_files)
    resolved_imports = 0
    dropped_imports = 0
    drop_log: List[Dict[str, str]] = []

    # Adversarial: track alias imports whose module resolves but ` as Y` breaks it
    alias_failures = 0
    alias_failure_log: List[Dict[str, str]] = []

    for file_entry in manifest_files:
        file_path = file_entry.get("path", "")
        content = file_entry.get("content", "")
        if not content or not content.strip():
            continue

        graph = extract_graph(file_path, content.encode("utf-8"))
        for imp in graph.get("imports", []):
            resolved = resolve_import(imp, file_path, all_paths)
            if resolved and resolved in file_by_path:
                resolved_imports += 1
            else:
                dropped_imports += 1
                reason = (
                    "external/stdlib"
                    if resolved is None
                    else "not in repo file set"
                )
                drop_log.append({
                    "file": file_path,
                    "import": imp,
                    "reason": reason,
                })
                logger.debug(
                    "IMPORT_DROP file=%s import=%r reason=%s",
                    file_path, imp, reason,
                )

                # Adversarial: check if this is an alias import where the
                # module part alone WOULD resolve but the " as alias" suffix
                # prevented resolution.
                if " as " in imp:
                    module_part = imp.split(" as ")[0].strip()
                    alias_part = imp.split(" as ", 1)[1].strip()
                    module_resolved = resolve_import(module_part, file_path, all_paths)
                    if module_resolved and module_resolved in file_by_path:
                        alias_failures += 1
                        alias_failure_log.append({
                            "file": file_path,
                            "import": imp,
                            "alias": alias_part,
                            "module": module_part,
                        })
                        logger.warning(
                            "ALIAS_FAILURE file=%s import=%r alias=%r "
                            "module_resolves_to=%s",
                            file_path, imp, alias_part, module_resolved,
                        )

    # ------------------------------------------------------------------
    # 3. Symbol-call ambiguity stats
    #    Walk every file's symbols and re-run the resolver to count how
    #    many potential call edges were dropped due to ambiguity.
    # ------------------------------------------------------------------
    # Rebuild the maps the pipeline uses for resolution
    file_symbol_map: Dict[Any, Dict[str, CodeSymbol]] = {}
    for sym in symbols:
        file_symbol_map.setdefault(sym.file_id, {})[sym.name] = sym

    global_symbol_map: Dict[str, List[CodeSymbol]] = {}
    for sym in symbols:
        global_symbol_map.setdefault(sym.name, []).append(sym)

    file_dependency_map: Dict[Any, List[Any]] = {}
    for dep in deps:
        file_dependency_map.setdefault(dep.source_file_id, []).append(dep.target_file_id)

    all_known_names = list(global_symbol_map.keys())

    ambiguous_count = 0
    lost_valid_edges = 0
    ambiguity_log: List[Dict[str, Any]] = []

    for file_entry in manifest_files:
        file_path = file_entry.get("path", "")
        content = file_entry.get("content", "")
        if not content or not content.strip():
            continue
        rf = file_by_path.get(file_path)
        if rf is None:
            continue

        local_syms = {name: sym for name, sym in file_symbol_map.get(rf.id, {}).items()}
        if not local_syms:
            continue

        calls = extract_symbol_calls(content, all_known_names)

        for caller_name, callee_names in calls.items():
            caller_sym = local_syms.get(caller_name)
            if caller_sym is None:
                continue

            for callee_name in callee_names:
                # Mirror the priority resolver from the pipeline
                target = _resolve_symbol_for_validation(
                    caller_sym.file_id,
                    callee_name,
                    file_symbol_map,
                    file_dependency_map,
                    global_symbol_map,
                )
                if target is None:
                    candidates = global_symbol_map.get(callee_name, [])
                    ambiguous_count += 1
                    ambiguity_log.append({
                        "caller_file": file_path,
                        "callee_name": callee_name,
                        "candidates": [
                            file_by_id[s.file_id].path
                            for s in candidates
                            if s.file_id in file_by_id
                        ],
                    })
                    logger.debug(
                        "SYMBOL_AMBIGUOUS caller_file=%s callee=%r candidates=%d",
                        file_path, callee_name, len(candidates),
                    )
                    # Adversarial: any dropped call where the callee IS a real
                    # repo symbol (even if ambiguous) counts as a lost valid edge.
                    if candidates:
                        lost_valid_edges += 1

    # ------------------------------------------------------------------
    # 4. Execution path validation
    # ------------------------------------------------------------------
    paths_found = 0
    broken_paths = 0
    broken_path_log: List[Dict[str, str]] = []

    for ep in eps:
        try:
            result = reconstruct_execution(str(ep.id), session)
        except ValueError as exc:
            broken_paths += 1
            ef = file_by_id.get(ep.file_id)
            broken_path_log.append({
                "entry_point_id": str(ep.id),
                "file": ef.path if ef else "<unknown>",
                "reason": str(exc),
            })
            logger.warning(
                "EXECUTION_BREAK entry_point=%s reason=%s", ep.id, exc
            )
            continue

        if result.get("execution_chain"):
            paths_found += 1
        else:
            broken_paths += 1
            broken_path_log.append({
                "entry_point_id": str(ep.id),
                "file": result.get("entry_file", "<unknown>"),
                "reason": "empty execution chain (no outgoing call edges from entry file symbols)",
            })
            logger.warning(
                "EXECUTION_BREAK entry_point=%s file=%s reason=empty_chain",
                ep.id, result.get("entry_file"),
            )

    # ------------------------------------------------------------------
    # 5. Missing entry points (adversarial: patterns extractor doesn't cover)
    #
    # Patterns the current extractor handles:
    #   Python: if __name__ == "__main__", FastAPI(, Flask(
    #   JS/TS:  express(), app.listen(
    #
    # Patterns the extractor does NOT handle (measured as "missing"):
    #   uvicorn.run(    — FastAPI deployment
    #   @app.route(     — Flask route decorator
    #   @app.get(       — FastAPI route decorator
    #   @app.post(      — FastAPI route decorator
    #   @router.get(    — FastAPI APIRouter
    #   @router.post(   — FastAPI APIRouter
    #   @click.command  — click CLI entry point
    # ------------------------------------------------------------------
    _UNDETECTED_PATTERNS = [
        "uvicorn.run(",
        "@app.route(",
        "@app.get(",
        "@app.post(",
        "@app.put(",
        "@app.delete(",
        "@router.get(",
        "@router.post(",
        "@router.put(",
        "@router.delete(",
        "@click.command",
    ]

    files_with_ep = {ep.file_id for ep in eps}
    missing_entry_points = 0
    missing_entry_point_log: List[Dict[str, str]] = []

    for file_entry in manifest_files:
        file_path = file_entry.get("path", "")
        content = file_entry.get("content", "")
        if not content or not content.strip():
            continue
        rf = file_by_path.get(file_path)
        if rf is None:
            continue
        # Only flag files that have NO entry point already detected
        if rf.id in files_with_ep:
            continue
        for pattern in _UNDETECTED_PATTERNS:
            if pattern in content:
                missing_entry_points += 1
                missing_entry_point_log.append({
                    "file": file_path,
                    "pattern": pattern,
                })
                logger.warning(
                    "MISSING_ENTRY_POINT file=%s pattern=%r",
                    file_path, pattern,
                )
                break  # one entry per file

    return {
        "repo": repo_id,
        "files": len(repo_files),
        "symbols": len(symbols),
        "dependencies": len(deps),
        "call_edges": len(edges),
        "entry_points": len(eps),
        "resolution_stats": {
            "resolved": resolved_imports,
            "dropped": dropped_imports,
            "ambiguous": ambiguous_count,
        },
        "execution_validation": {
            "paths_found": paths_found,
            "broken_paths": broken_paths,
        },
        "failure_analysis": {
            "lost_valid_edges": lost_valid_edges,
            "alias_failures": alias_failures,
            "ambiguous_drops": ambiguous_count,
            "missing_entry_points": missing_entry_points,
        },
        "drop_log": drop_log,
        "ambiguity_log": ambiguity_log,
        "broken_path_log": broken_path_log,
        "alias_failure_log": alias_failure_log,
        "missing_entry_point_log": missing_entry_point_log,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_symbol_for_validation(
    caller_file_id: Any,
    callee_name: str,
    file_symbol_map: Dict[Any, Dict[str, Any]],
    file_dependency_map: Dict[Any, List[Any]],
    global_symbol_map: Dict[str, List[Any]],
) -> Any:
    """
    Mirror of the pipeline's _resolve_symbol — used by the validator to
    classify which calls were resolved vs. dropped/ambiguous.
    """
    # 1. Same file
    local = file_symbol_map.get(caller_file_id, {})
    if callee_name in local:
        return local[callee_name]

    # 2. Import graph
    for dep_file_id in file_dependency_map.get(caller_file_id, []):
        dep_syms = file_symbol_map.get(dep_file_id, {})
        if callee_name in dep_syms:
            return dep_syms[callee_name]

    # 3. Globally unique
    candidates = global_symbol_map.get(callee_name, [])
    if len(candidates) == 1:
        return candidates[0]

    return None
