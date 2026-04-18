"""
backend.app.repo_retrieval
==========================
REPO_CONTEXT_INTELLIGENCE_LAYER_V2

Deterministic, bounded, high-signal repository context retrieval.
NO embeddings.  ALL retrieval uses pre-stored RepoChunk rows — no network calls.

Pipeline: Normalize → Extract → Query → Score → Rank → Diversity → Return
"""

from __future__ import annotations

import logging
import uuid
from typing import List

from sqlmodel import Session, select

from backend.app.models import RepoChunk

logger = logging.getLogger(__name__)

# Common English stopwords to exclude from keyword extraction
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can", "need",
        "dare", "ought", "used", "it", "its", "this", "that", "these", "those",
        "i", "me", "my", "we", "our", "you", "your", "he", "she", "his", "her",
        "they", "them", "their", "what", "which", "who", "how", "when", "where",
        "why", "if", "not", "no", "so", "up", "about", "out", "into",
    }
)

_CHUNK_MAX_CHARS = 1500

# Phase 4: diversity limits
_MAX_CHUNKS_TOTAL = 5
_MAX_CHUNKS_PER_FILE = 2
_MIN_DISTINCT_FILES = 2

# Phase 3: path-based boost tokens
_BOOST_HIGH_TOKENS = frozenset({"main", "app", "index"})
_BOOST_DOC_TOKENS = frozenset({"readme", "docs"})


def _extract_keywords(query: str) -> list[str]:
    """Split *query* into lowercase non-stopword tokens of length >= 2."""
    tokens = query.lower().split()
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def _split_into_chunks(content: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """
    Split *content* into chunks of at most *max_chars* characters, preserving
    line boundaries wherever possible.
    """
    if len(content) <= max_chars:
        return [content]

    chunks: list[str] = []
    lines = content.splitlines(keepends=True)
    current: list[str] = []
    current_len = 0

    for line in lines:
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))

    return chunks


def _score_chunk(
    chunk: RepoChunk,
    keywords: list[str],
    lower_query: str,
) -> int:
    """
    Compute the ranking score for a single chunk.

    Phase 3 rules:
    - +1 per keyword occurrence in content  (base)
    - +3 if file_path stem contains "main", "app", or "index"
    - +2 if file_path stem contains "readme" or "docs"
    - +2 if query contains "explain" AND file_path is a documentation file
    - +2 if query contains "where" or "location" AND file_path contains a keyword
    - +1 if chunk is the first chunk in its source file (chunk_index == 0)
    """
    lower_content = chunk.content.lower()
    lower_path = chunk.file_path.lower()

    # Base: keyword match frequency
    score = sum(lower_content.count(kw) for kw in keywords)

    # No content matches and no query — nothing useful here
    if score == 0 and not keywords:
        return 0

    # +3 boost: high-signal file names
    if any(tok in lower_path for tok in _BOOST_HIGH_TOKENS):
        score += 3

    # +2 boost: documentation files
    is_doc_file = any(tok in lower_path for tok in _BOOST_DOC_TOKENS)
    if is_doc_file:
        score += 2

    # +2 boost: "explain" query + documentation file
    if "explain" in lower_query and is_doc_file:
        score += 2

    # +2 boost: "where"/"location" query + file path contains a keyword
    if ("where" in lower_query or "location" in lower_query) and keywords:
        if any(kw in lower_path for kw in keywords):
            score += 2

    # +1 boost: first chunk of the file (earliest position)
    if chunk.chunk_index == 0:
        score += 1

    return score


def _apply_diversity(
    scored: list[tuple[int, RepoChunk]],
    max_total: int = _MAX_CHUNKS_TOTAL,
    max_per_file: int = _MAX_CHUNKS_PER_FILE,
    min_distinct_files: int = _MIN_DISTINCT_FILES,
) -> list[RepoChunk]:
    """
    Phase 4: enforce diversity from a score-sorted list.

    Rules:
    1. MAX *max_per_file* chunks per file_path
    2. MAX *max_total* chunks overall
    3. Attempt to return chunks from at least *min_distinct_files* distinct files

    Strategy:
    - First pass: greedy selection respecting per-file cap
    - If fewer than *min_distinct_files* distinct files were selected and there are
      remaining chunks from other files, swap the lowest-scored single-file excess
      for the first eligible chunk from a new file.
    """
    selected: list[RepoChunk] = []
    per_file_count: dict[str, int] = {}

    for _, chunk in scored:
        if len(selected) >= max_total:
            break
        count = per_file_count.get(chunk.file_path, 0)
        if count >= max_per_file:
            continue
        selected.append(chunk)
        per_file_count[chunk.file_path] = count + 1

    # Ensure minimum distinct files if possible
    distinct_files = set(c.file_path for c in selected)
    if len(distinct_files) < min_distinct_files and len(selected) == max_total:
        # Try to swap the lowest-scored chunk from a dominant file for one from a new file
        remaining = [
            (s, c) for s, c in scored
            if c not in selected and c.file_path not in distinct_files
        ]
        if remaining:
            # Find the lowest-priority selected chunk from a file that has >1 chunk
            for i in range(len(selected) - 1, -1, -1):
                candidate = selected[i]
                if per_file_count.get(candidate.file_path, 0) > 1:
                    # Replace with first chunk from a new file
                    new_score, new_chunk = remaining[0]  # noqa: F841
                    selected[i] = new_chunk
                    break

    return selected


def retrieve_relevant_chunks(
    user_query: str,
    db: Session,
    chat_file_ids: list[uuid.UUID],
    max_chunks: int = _MAX_CHUNKS_TOTAL,
) -> List[RepoChunk]:
    """
    Return at most *max_chunks* RepoChunk rows relevant to *user_query*.

    Phase 5 — Multi-repo isolation:
    Only chunks from the explicitly supplied *chat_file_ids* are considered.
    No blind conversation-wide scanning.

    Pipeline (V2):
    1. Normalize query / extract keywords
    2. Query RepoChunk by chat_file_id membership
    3. Score each chunk (Phase 3 boost rules)
    4. Sort descending by score
    5. Apply diversity enforcement (Phase 4)
    6. Return bounded result
    """
    if not chat_file_ids:
        return []

    lower_query = user_query.lower()
    keywords = _extract_keywords(user_query)

    # Phase 5: scope strictly to the provided file IDs
    chunk_stmt = select(RepoChunk).where(
        RepoChunk.chat_file_id.in_(chat_file_ids)  # type: ignore[attr-defined]
    )
    all_chunks = db.exec(chunk_stmt).all()

    if not all_chunks:
        return []

    if not keywords:
        # No keywords — fall back to first N from Phase 4 diversity pass
        fallback_scored = [(0, c) for c in all_chunks]
        return _apply_diversity(fallback_scored, max_total=max_chunks)

    # Phase 3: score every chunk
    scored: list[tuple[int, RepoChunk]] = []
    for chunk in all_chunks:
        score = _score_chunk(chunk, keywords, lower_query)
        if score > 0:
            scored.append((score, chunk))

    # Sort descending
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        # Phase 4: diversity enforcement
        return _apply_diversity(scored, max_total=max_chunks)

    # No hits at all — return diversity-filtered fallback
    fallback_scored = [(0, c) for c in all_chunks]
    return _apply_diversity(fallback_scored, max_total=max_chunks)
