from __future__ import annotations

import re
from enum import Enum


class QueryType(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    HYBRID = "HYBRID"
    SEMANTIC = "SEMANTIC"


_STRUCTURAL_KEYWORD_RE = re.compile(r"\b(count|list|structure|paths?|files?)\b", re.IGNORECASE)
_SEMANTIC_PATTERNS = (
    "what do they do",
    "what does",
    "explain",
    "why",
    "purpose",
    "describe",
    "how does",
    "how do",
    "where",
    "important",
    "inside",
)


def classify_query(input_text: str) -> QueryType:
    lower = (input_text or "").lower()
    has_structural_intent = bool(_STRUCTURAL_KEYWORD_RE.search(lower))
    if has_structural_intent:
        has_semantic_intent = any(pattern in lower for pattern in _SEMANTIC_PATTERNS)
        if has_semantic_intent:
            return QueryType.HYBRID
        return QueryType.STRUCTURAL
    return QueryType.SEMANTIC
