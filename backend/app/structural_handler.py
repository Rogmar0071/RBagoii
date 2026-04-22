from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlmodel import Session, select

from backend.app.models import RepoChunk, RepoIndexRegistry


def _build_tree(paths: list[str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path in sorted(paths):
        node = root
        parts = [p for p in path.split("/") if p]
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node.setdefault(parts[-1], {})
    return root


def _is_count_query(lower_query: str) -> bool:
    return any(x in lower_query for x in ("how many files", "number of files", "count files"))


def _is_list_query(lower_query: str) -> bool:
    return any(x in lower_query for x in ("list files", "list all files", "show files"))


def _is_structure_query(lower_query: str) -> bool:
    return any(
        x in lower_query
        for x in ("repository structure", "repo structure", "file tree", "directory structure")
    )


def handle_structural_query(
    *,
    db: Session,
    repo_ids: list[uuid.UUID],
    query_text: str,
) -> dict[str, Any]:
    registries = db.exec(
        select(RepoIndexRegistry).where(RepoIndexRegistry.repo_id.in_(repo_ids))  # type: ignore[attr-defined]
    ).all()
    if len(registries) != len(repo_ids):
        return {"error_code": "INSUFFICIENT_CONTEXT"}

    total_chunks = sum(int(r.total_chunks or 0) for r in registries)
    expected_total_files = sum(int(r.total_files or 0) for r in registries)

    chunks = db.exec(
        select(RepoChunk)
        .where(RepoChunk.repo_id.in_(repo_ids))  # type: ignore[attr-defined]
        .order_by(RepoChunk.repo_id.asc(), RepoChunk.file_path.asc(), RepoChunk.chunk_index.asc())
    ).all()
    retrieved_chunks = len(chunks)
    if retrieved_chunks != total_chunks:
        return {"error_code": "INSUFFICIENT_CONTEXT"}

    per_repo_files: dict[uuid.UUID, set[str]] = defaultdict(set)
    for chunk in chunks:
        if chunk.repo_id is not None and chunk.file_path:
            per_repo_files[chunk.repo_id].add(chunk.file_path)

    all_files: list[str] = []
    for rid in sorted(repo_ids, key=str):
        for path in sorted(per_repo_files.get(rid, set())):
            all_files.append(path if len(repo_ids) == 1 else f"{rid}:{path}")

    if len(all_files) != expected_total_files:
        return {"error_code": "INSUFFICIENT_CONTEXT"}

    lower_query = query_text.lower()
    data: dict[str, Any] = {"files": all_files, "count": len(all_files)}
    if _is_structure_query(lower_query):
        data["structure"] = _build_tree(all_files)
    elif _is_count_query(lower_query):
        data = {"files": all_files, "count": len(all_files)}
    elif _is_list_query(lower_query):
        data = {"files": all_files, "count": len(all_files)}

    return {
        "repo_count": len(repo_ids),
        "total_chunks": total_chunks,
        "retrieved_chunks": total_chunks,
        "data": data,
        "source": "index",
        "error_code": None,
    }

