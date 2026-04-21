"""
backend.app.execution_reconstruction
======================================
MQP-CONTRACT: GRAPH-EXECUTION-VALIDATION v1.0

Reconstructs an ordered execution chain from an EntryPoint using only
DB-backed traversal.

Traversal path:
  EntryPoint → RepoFile → CodeSymbol → SymbolCallEdge (→ CodeSymbol …)

Rules (hard):
  • NO string matching outside graph tables
  • NO fallback inference
  • ONLY DB-backed traversal
  • IF graph missing edge → STOP (do NOT guess)
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional


def reconstruct_execution(entry_point_id: str, session: Any) -> Dict[str, Any]:
    """
    Reconstruct the execution chain rooted at *entry_point_id*.

    Parameters
    ----------
    entry_point_id:
        UUID string of an ``EntryPoint`` row.
    session:
        An active SQLModel / SQLAlchemy session.

    Returns
    -------
    dict with shape::

        {
            "entry_file":      str,          # path of entry file
            "entry_symbol":    str | None,   # name of first symbol in entry file
            "execution_chain": [             # one node per symbol in entry file
                {
                    "symbol": str,
                    "file":   str,
                    "calls":  [...]          # recursively same shape
                }
            ]
        }

    Raises
    ------
    ValueError
        If the EntryPoint or its RepoFile is not found in the DB.
    """
    from sqlmodel import select

    from backend.app.models import CodeSymbol, EntryPoint, RepoFile, SymbolCallEdge

    # ------------------------------------------------------------------
    # 1.  Resolve entry point → entry file
    # ------------------------------------------------------------------
    ep = session.get(EntryPoint, uuid.UUID(entry_point_id))
    if ep is None:
        raise ValueError(f"EntryPoint {entry_point_id!r} not found")

    entry_file = session.get(RepoFile, ep.file_id)
    if entry_file is None:
        raise ValueError(f"RepoFile {ep.file_id!r} not found for EntryPoint {entry_point_id!r}")

    # ------------------------------------------------------------------
    # 2.  Pre-load all symbols and files (avoids N+1 queries)
    # ------------------------------------------------------------------
    all_symbols: Dict[uuid.UUID, CodeSymbol] = {
        s.id: s for s in session.exec(select(CodeSymbol))
    }
    all_files: Dict[uuid.UUID, RepoFile] = {
        f.id: f for f in session.exec(select(RepoFile))
    }

    # Outgoing call edges indexed by source symbol id
    edges_by_source: Dict[uuid.UUID, List[SymbolCallEdge]] = {}
    for edge in session.exec(select(SymbolCallEdge)):
        edges_by_source.setdefault(edge.source_symbol_id, []).append(edge)

    # ------------------------------------------------------------------
    # 3.  Recursive DFS traversal — only DB edges, cycle guard via visited
    # ------------------------------------------------------------------
    def _traverse(symbol_id: uuid.UUID, visited: set) -> Optional[Dict[str, Any]]:
        if symbol_id in visited:
            return None  # cycle — stop here

        sym = all_symbols.get(symbol_id)
        if sym is None:
            return None  # symbol not in DB — stop

        sym_file = all_files.get(sym.file_id)
        if sym_file is None:
            return None  # file missing — stop (hard rule: do not guess)

        visited = visited | {symbol_id}  # immutable copy per path branch

        calls: List[Dict[str, Any]] = []
        for edge in edges_by_source.get(symbol_id, []):
            if edge.target_symbol_id is None:
                continue  # no DB-backed target — stop (no inference)
            child = _traverse(edge.target_symbol_id, visited)
            if child is not None:
                calls.append(child)

        return {
            "symbol": sym.name,
            "file": sym_file.path,
            "calls": calls,
        }

    # ------------------------------------------------------------------
    # 4.  Build chain from all symbols in the entry file, ordered by start_line
    # ------------------------------------------------------------------
    entry_symbols = sorted(
        [s for s in all_symbols.values() if s.file_id == ep.file_id],
        key=lambda s: s.start_line,
    )

    execution_chain: List[Dict[str, Any]] = []
    for sym in entry_symbols:
        node = _traverse(sym.id, set())
        if node is not None:
            execution_chain.append(node)

    return {
        "entry_file": entry_file.path,
        "entry_symbol": entry_symbols[0].name if entry_symbols else None,
        "execution_chain": execution_chain,
    }
