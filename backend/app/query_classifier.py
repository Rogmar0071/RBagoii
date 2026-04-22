from __future__ import annotations

from enum import Enum


class QueryType(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    SEMANTIC = "SEMANTIC"


_STRUCTURAL_PATTERNS = (
    "how many files",
    "number of files",
    "count files",
    "list files",
    "list all files",
    "show files",
    "repository structure",
    "repo structure",
    "file tree",
    "directory structure",
)


def classify_query(input_text: str) -> QueryType:
    lower = (input_text or "").lower()
    for pattern in _STRUCTURAL_PATTERNS:
        if pattern in lower:
            return QueryType.STRUCTURAL
    return QueryType.SEMANTIC

