"""
backend.app.file_resolution
===========================
MQP-CONTRACT: REPO_CONTEXT_FILE_RESOLUTION_V1

Single, deterministic chunk → file resolution boundary.

Rules (enforced):
  * file identity is derived ONLY from ``RepoChunk.file_id``
  * lookup is a single deterministic join: ``RepoChunk.file_id == RepoFile.id``
  * NO fallback, NO reconstruction, NO inference, NO probabilistic matching
  * if any chunks are supplied but the resulting file set is empty
    → ``Exception("FILE_RESOLUTION_BROKEN")`` is raised (HARD FAIL)
  * empty input → empty output (the only valid empty case)

This module is intentionally tiny: it is the sole transition authority
for the ``CHUNKS_RETRIEVED → FILES_RESOLVED`` step in the context
state machine.  All callers that build CTX_FILES MUST pass through
``resolve_files_from_chunks``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable, Sequence

from sqlmodel import Session, select

from backend.app.models import RepoChunk, RepoFile

logger = logging.getLogger(__name__)

__all__ = ["resolve_files_from_chunks", "FileResolutionError"]


class FileResolutionError(Exception):
    """Raised when the deterministic chunk→file join cannot be satisfied."""


def _coerce_file_id(value: object) -> uuid.UUID | None:
    """
    Return ``value`` as a ``uuid.UUID`` or ``None`` if it cannot be coerced.

    Strict: only ``uuid.UUID`` instances or non-empty strings parseable as
    UUIDs are accepted.  Anything else is treated as missing identity.
    """
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return uuid.UUID(s)
        except ValueError:
            return None
    return None


def resolve_files_from_chunks(
    chunks: Sequence[RepoChunk] | Iterable[RepoChunk],
    db: Session,
) -> list[RepoFile]:
    """
    Resolve ``chunks`` to their canonical ``RepoFile`` rows.

    Contract (REPO_CONTEXT_FILE_RESOLUTION_V1):

    * Extract ``file_id`` strictly from ``chunk.file_id`` (NO other source).
    * Query ``RepoFile`` using ONLY those ids — single deterministic
      ``WHERE RepoFile.id IN (...)`` query.
    * Decision table:
        - chunks == 0                          → returns ``[]`` (CONTEXT_EMPTY)
        - chunks > 0 AND files resolved        → returns ``list[RepoFile]``
        - chunks > 0 AND no files resolved     → ``FileResolutionError``
        - any chunk missing a usable file_id   → ``FileResolutionError``

    Parameters
    ----------
    chunks:
        Iterable of ``RepoChunk`` rows produced by retrieval.
    db:
        Active SQLModel ``Session`` used to perform the join.

    Returns
    -------
    list[RepoFile]
        The resolved files, in deterministic order (sorted by id).

    Raises
    ------
    FileResolutionError
        When the deterministic join cannot be satisfied.  The exception
        message is exactly ``"FILE_RESOLUTION_BROKEN"`` to match the
        runtime enforcement contract.
    """
    chunk_list = list(chunks)

    # CASE 3: no chunks → CONTEXT_EMPTY (the only valid empty result).
    if not chunk_list:
        logger.info(
            "FILE_RESOLUTION: chunks=0 files=0 state=CONTEXT_EMPTY"
        )
        return []

    # Extract file_ids strictly from chunk.file_id — no other source.
    file_ids: list[uuid.UUID] = []
    for chunk in chunk_list:
        coerced = _coerce_file_id(getattr(chunk, "file_id", None))
        if coerced is None:
            # Missing / unusable file_id on a chunk is a HARD FAIL —
            # the contract guarantees file_id is REQUIRED and NON-NULL.
            logger.error(
                "FILE_RESOLUTION: chunk missing file_id chunk_id=%s",
                getattr(chunk, "id", None),
            )
            raise FileResolutionError("FILE_RESOLUTION_BROKEN")
        file_ids.append(coerced)

    # Deduplicate while preserving deterministic ordering.
    unique_ids = sorted(set(file_ids), key=str)

    # Single deterministic query: RepoChunk.file_id == RepoFile.id.
    stmt = select(RepoFile).where(RepoFile.id.in_(unique_ids))  # type: ignore[attr-defined]
    files: list[RepoFile] = list(db.exec(stmt).all())

    # CASE 2: chunks > 0 AND files == 0 → HARD FAIL.
    if not files:
        logger.error(
            "FILE_RESOLUTION: chunks=%s file_ids=%s files=0 -> FILE_RESOLUTION_BROKEN",
            len(chunk_list),
            len(unique_ids),
        )
        raise FileResolutionError("FILE_RESOLUTION_BROKEN")

    # Sort the result for temporal consistency: same input → same output order.
    files.sort(key=lambda f: str(f.id))

    logger.info(
        "FILE_RESOLUTION: chunks=%s file_ids=%s files=%s state=FILES_RESOLVED",
        len(chunk_list),
        len(unique_ids),
        len(files),
    )
    return files
