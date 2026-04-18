"""
backend.app.repo_retrieval
==========================
REPO_CONTEXT_SELECTIVE_RETRIEVAL_LAYER_V1 — Phase 3

Deterministic keyword-based retrieval over pre-stored RepoChunk rows.
NO embeddings.  ALL retrieval uses pre-stored data — no network calls.
"""

from __future__ import annotations

import logging
from typing import List

from sqlmodel import Session, select

from backend.app.models import ChatFile, RepoChunk

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


def retrieve_relevant_chunks(
    conversation_id: str,
    user_query: str,
    db: Session,
    max_chunks: int = 5,
) -> List[RepoChunk]:
    """
    Return at most *max_chunks* RepoChunk rows relevant to *user_query*.

    Strategy (deterministic keyword matching — V1):
    1. Extract keywords from *user_query* (lowercase, stopwords removed).
    2. Load all RepoChunks belonging to github_repo ChatFiles in this conversation.
    3. Score each chunk by the number of keyword occurrences in its content.
    4. Return the top *max_chunks* by score (ties broken by DB insertion order).

    If no keywords are found, returns the first *max_chunks* chunks so the
    caller always has *something* to work with.
    """
    keywords = _extract_keywords(user_query)

    # Fetch all ChatFile IDs for this conversation that are github_repo files
    stmt = select(ChatFile).where(
        ChatFile.conversation_id == conversation_id,
        ChatFile.category == "github_repo",
        ChatFile.included_in_context.is_(True),  # type: ignore[attr-defined]
    )
    repo_files = db.exec(stmt).all()

    if not repo_files:
        return []

    file_ids = [f.id for f in repo_files]

    # Fetch all RepoChunks for those files
    chunk_stmt = select(RepoChunk).where(RepoChunk.chat_file_id.in_(file_ids))  # type: ignore[attr-defined]
    all_chunks = db.exec(chunk_stmt).all()

    if not all_chunks:
        return []

    if not keywords:
        return list(all_chunks[:max_chunks])

    # Score each chunk
    scored: list[tuple[int, RepoChunk]] = []
    for chunk in all_chunks:
        lower_content = chunk.content.lower()
        score = sum(lower_content.count(kw) for kw in keywords)
        if score > 0:
            scored.append((score, chunk))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        return [c for _, c in scored[:max_chunks]]

    # No keyword hits — return first N chunks as fallback
    return list(all_chunks[:max_chunks])
