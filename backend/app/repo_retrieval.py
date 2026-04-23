"""
backend.app.repo_retrieval
==========================
REPO_CONTEXT_INTELLIGENCE_LAYER_V2 / REPO_CONTEXT_FINALIZATION_V1

Deterministic, bounded, high-signal repository context retrieval.
NO embeddings.  ALL retrieval uses pre-stored RepoChunk rows — no network calls.

Pipeline: Normalize → Extract → Query → Score → Rank → Diversity → Budget → Return
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session, select

from backend.app.models import RepoChunk

logger = logging.getLogger(__name__)

# Common English stopwords to exclude from keyword extraction
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "his",
        "her",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "if",
        "not",
        "no",
        "so",
        "up",
        "about",
        "out",
        "into",
    }
)

_CHUNK_MAX_CHARS = 1500

# Phase 4 — diversity limits
_MAX_CHUNKS_TOTAL = 5
_MAX_CHUNKS_PER_FILE = 2
_MIN_DISTINCT_FILES = 2

# Phase 3 — path-based boost tokens
_BOOST_HIGH_TOKENS = frozenset({"main", "app", "index"})
_BOOST_DOC_TOKENS = frozenset({"readme", "docs"})

# Phase 4 (FINALIZATION) — token budget (40 % of 8 000 total context tokens)
_MAX_CONTEXT_TOKENS_REPO = 3200


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

    Scoring rules (FINALIZATION V1 — Phase 5):
    - +1 per keyword occurrence in content          (base keyword match)
    - +2 if file_path stem (filename) contains a query keyword (filename match)
    - +3 if file_path contains "main", "app", or "index" (path relevance — high-signal)
    - +2 if file_path contains "readme" or "docs"    (path relevance — documentation)
    - +2 if query contains "explain" AND doc file    (explain boost)
    - +2 if query contains "where"/"location" AND file path contains a keyword
    - +1 if chunk_index == 0                        (recency: first chunk of file)
    - -1 * (chunk_index // 3) clamped to -2         (recency weight: deeper chunks score less)
    """
    lower_content = chunk.content.lower()
    lower_path = chunk.file_path.lower()

    # Base: keyword match frequency
    score = sum(lower_content.count(kw) for kw in keywords)

    # No content matches and no query — nothing useful here
    if score == 0 and not keywords:
        return 0

    # +2 boost: filename contains a query keyword (filename match)
    filename_part = lower_path.split("/")[-1]
    filename_stem = filename_part.split(".")[0] if filename_part else ""
    if filename_stem and keywords and any(kw in filename_stem for kw in keywords):
        score += 2

    # +3 boost: high-signal file names (path relevance)
    if any(tok in lower_path for tok in _BOOST_HIGH_TOKENS):
        score += 3

    # +2 boost: documentation files (path relevance)
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

    # Recency weight: first chunk of file scores highest
    if chunk.chunk_index == 0:
        score += 1
    else:
        # Progressively reduce score for deeper chunks (max -2)
        score -= min(2, chunk.chunk_index // 3)

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
            (s, c) for s, c in scored if c not in selected and c.file_path not in distinct_files
        ]
        if remaining:
            # Find the lowest-priority selected chunk from a file that has >1 chunk
            for i in range(len(selected) - 1, -1, -1):
                candidate = selected[i]
                if per_file_count.get(candidate.file_path, 0) > 1:
                    # Replace with first chunk from a new file
                    _, new_chunk = remaining[0]
                    selected[i] = new_chunk
                    break

    return selected


def _apply_token_budget(
    chunks: list[RepoChunk],
    max_tokens: int = _MAX_CONTEXT_TOKENS_REPO,
) -> list[RepoChunk]:
    """
    REPO_CONTEXT_FINALIZATION_V1 — Phase 4.

    Trim *chunks* so total token_estimate does not exceed *max_tokens*.
    Chunks are assumed to already be priority-ordered (highest relevance first).
    """
    result: list[RepoChunk] = []
    used = 0
    for chunk in chunks:
        estimate = chunk.token_estimate or max(1, len(chunk.content) // 4)
        if used + estimate > max_tokens:
            break
        result.append(chunk)
        used += estimate
    return result


def _build_retrieval_payload(chunks: list[RepoChunk]) -> dict[str, Any]:
    """
    Build retrieval payload with mandatory file identity metadata.

    Contract:
    - chunks
    - file_ids
    - file_paths
    - total_chunks
    """
    valid_chunks: list[RepoChunk] = []
    file_ids: list[str] = []
    file_paths: list[str] = []
    for chunk in chunks:
        file_path = str(chunk.file_path or "").strip()
        if not file_path:
            continue
        if chunk.chat_file_id is not None:
            file_id = str(chunk.chat_file_id).strip()
        else:
            file_id = str(chunk.graph_group or "").strip()
        if not file_id:
            continue
        valid_chunks.append(chunk)
        file_ids.append(file_id)
        file_paths.append(file_path)

    if chunks and not valid_chunks:
        raise RuntimeError("NO_VALID_CHUNKS_WITH_FILE_ID")

    if valid_chunks and not file_ids:
        raise RuntimeError("RETRIEVAL_INTEGRITY_VIOLATION")

    return {
        "chunks": valid_chunks,
        "file_ids": file_ids,
        "file_paths": file_paths,
        "total_chunks": len(valid_chunks),
    }


def _finalize_retrieval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    chunks = list(payload["chunks"])
    file_ids = list(payload["file_ids"])
    if chunks and not file_ids:
        raise RuntimeError("RETRIEVAL_INTEGRITY_VIOLATION")
    logger.info(
        "RETRIEVAL_RESULT: chunks_count=%s file_ids_count=%s sample_file_id=%s",
        len(chunks),
        len(file_ids),
        file_ids[0] if file_ids else None,
    )
    return payload


def retrieve_relevant_chunks(
    user_query: str,
    db: Session,
    chat_file_ids: list[uuid.UUID] | None = None,
    repo_ids: list[uuid.UUID] | None = None,
    ingest_job_ids: list[uuid.UUID] | None = None,
    conversation_id: str | None = None,
    max_chunks: int = _MAX_CHUNKS_TOTAL,
    max_tokens: int = _MAX_CONTEXT_TOKENS_REPO,
) -> dict[str, Any]:
    """
    Return at most *max_chunks* RepoChunk rows relevant to *user_query*,
    subject to the token budget *max_tokens*.

    Source priority (first non-empty wins):
      1. repo_ids          — first-class Repo entities
      2. ingest_job_ids    — new unified IngestJob entities
      3. conversation_id   — all IngestJob chunks for a conversation
      4. chat_file_ids     — legacy V1 ingestion path

    Pipeline:
    1. Normalise query / extract keywords
    2. Query RepoChunk by the appropriate FK
    3. Score each chunk (keyword + path boosts + recency weight)
    4. Sort descending by score
    5. Apply diversity enforcement
    6. Apply token budget
    7. Return bounded result
    """
    has_repo_ids = bool(repo_ids)
    has_ingest_ids = bool(ingest_job_ids)
    has_conversation = bool(conversation_id)
    has_file_ids = bool(chat_file_ids)

    if not has_repo_ids and not has_ingest_ids and not has_conversation and not has_file_ids:
        return _finalize_retrieval_payload(_build_retrieval_payload([]))

    lower_query = user_query.lower()
    keywords = _extract_keywords(user_query)

    # Build the chunk query based on source priority
    if has_repo_ids:
        chunk_stmt = select(RepoChunk).where(
            RepoChunk.repo_id.in_(repo_ids)  # type: ignore[attr-defined]
        )
    elif has_ingest_ids:
        chunk_stmt = select(RepoChunk).where(
            RepoChunk.ingest_job_id.in_(ingest_job_ids)  # type: ignore[attr-defined]
        )
    elif has_conversation:
        # Resolve all IngestJob IDs for this conversation, then fetch chunks
        from backend.app.models import IngestJob

        job_ids_for_conv = [
            row.id
            for row in db.exec(
                select(IngestJob).where(
                    IngestJob.conversation_id == conversation_id,
                    IngestJob.status == "success",
                )
            ).all()
        ]
        if not job_ids_for_conv:
            return _finalize_retrieval_payload(_build_retrieval_payload([]))
        chunk_stmt = select(RepoChunk).where(
            RepoChunk.ingest_job_id.in_(job_ids_for_conv)  # type: ignore[attr-defined]
        )
    else:
        # Backward-compat path: query by chat_file_id
        chunk_stmt = select(RepoChunk).where(
            RepoChunk.chat_file_id.in_(chat_file_ids)  # type: ignore[attr-defined]
        )

    all_chunks = db.exec(chunk_stmt).all()

    if not all_chunks:
        return _finalize_retrieval_payload(_build_retrieval_payload([]))

    if not keywords:
        return _finalize_retrieval_payload(_build_retrieval_payload([]))

    scored: list[tuple[int, RepoChunk]] = []
    for chunk in all_chunks:
        score = _score_chunk(chunk, keywords, lower_query)
        if score > 0:
            scored.append((score, chunk))

    scored.sort(
        key=lambda x: (
            -x[0],
            x[1].file_path,
            x[1].chunk_index,
            str(x[1].id),
        )
    )

    if scored:
        selected = _apply_diversity(scored, max_total=max_chunks)
        bounded = _apply_token_budget(selected, max_tokens=max_tokens)
        return _finalize_retrieval_payload(_build_retrieval_payload(bounded))

    return _finalize_retrieval_payload(_build_retrieval_payload([]))
