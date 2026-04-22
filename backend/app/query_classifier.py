from __future__ import annotations

import re
from enum import Enum


class QueryType(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    HYBRID = "HYBRID"
    SEMANTIC = "SEMANTIC"


_STRUCTURAL_TRIGGERS = (
    "how many files",
    "file count",
    "number of files",
    "list files",
    "list all files",
    "repo structure",
    "file paths",
)
_STRUCTURAL_KEYWORD_RE = re.compile(r"\b(count|list|structure|paths?|files?)\b", re.IGNORECASE)
_SEMANTIC_TRIGGERS = (
    "what does",
    "how does",
    "why",
    "explain",
    "purpose",
)


def _has_structural_intent(lower: str) -> bool:
    if any(trigger in lower for trigger in _STRUCTURAL_TRIGGERS):
        return True
    return bool(_STRUCTURAL_KEYWORD_RE.search(lower))


def _has_semantic_intent(lower: str) -> bool:
    return any(trigger in lower for trigger in _SEMANTIC_TRIGGERS)


def classify_query(input_text: str) -> QueryType:
    lower = (input_text or "").lower()
    has_structural_intent = _has_structural_intent(lower)
    has_semantic_intent = _has_semantic_intent(lower)
    if has_structural_intent and has_semantic_intent:
        return QueryType.HYBRID
    if has_structural_intent:
        return QueryType.STRUCTURAL
    return QueryType.SEMANTIC


def route_query(query: str) -> QueryType:
    return classify_query(query)
