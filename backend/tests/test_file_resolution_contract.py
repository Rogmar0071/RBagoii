"""
Tests for MQP-CONTRACT: REPO_CONTEXT_FILE_RESOLUTION_V1.

Enforced invariants:

    * chunks == 0                     -> returns []            (CONTEXT_EMPTY)
    * chunks > 0 AND files resolved   -> returns list[RepoFile] (CASE 1)
    * chunks > 0 AND files == 0       -> FileResolutionError    (CASE 2)
    * chunk.file_id missing/invalid   -> FileResolutionError
    * deterministic: same input -> same output (order + identity)
    * no fallback: resolver NEVER consults file_path / repo_id / ingest_job_id
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_file_resolution")

from backend.app.file_resolution import (  # noqa: E402
    FileResolutionError,
    resolve_files_from_chunks,
)
from backend.app.models import RepoChunk, RepoFile  # noqa: E402


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_file_resolution.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture()
def session() -> Session:
    import backend.app.database as db_module

    with Session(db_module.get_engine()) as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo_file(session: Session, path: str = "src/a.py") -> RepoFile:
    rf = RepoFile(
        id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        path=path,
        language="python",
        size_bytes=10,
    )
    session.add(rf)
    session.commit()
    session.refresh(rf)
    return rf


def _make_chunk(
    file_id: uuid.UUID,
    file_path: str = "src/a.py",
    chunk_index: int = 0,
) -> RepoChunk:
    return RepoChunk(
        repo_id=uuid.uuid4(),
        file_id=file_id,
        file_path=file_path,
        content="x",
        chunk_index=chunk_index,
        token_estimate=1,
    )


# ---------------------------------------------------------------------------
# CASE 1: chunks > 0 AND files resolved -> CONTEXT_BUILT
# ---------------------------------------------------------------------------


def test_case1_chunks_present_files_resolved(session: Session) -> None:
    rf_a = _make_repo_file(session, "src/a.py")
    rf_b = _make_repo_file(session, "src/b.py")

    chunks = [
        _make_chunk(rf_a.id, "src/a.py", 0),
        _make_chunk(rf_a.id, "src/a.py", 1),
        _make_chunk(rf_b.id, "src/b.py", 0),
    ]

    files = resolve_files_from_chunks(chunks, session)

    assert len(files) == 2
    resolved_ids = {f.id for f in files}
    assert resolved_ids == {rf_a.id, rf_b.id}


def test_case1_single_chunk_single_file(session: Session) -> None:
    rf = _make_repo_file(session, "main.py")
    files = resolve_files_from_chunks([_make_chunk(rf.id, "main.py")], session)
    assert len(files) == 1
    assert files[0].id == rf.id


# ---------------------------------------------------------------------------
# CASE 2: chunks > 0 AND files == 0 -> FILE_RESOLUTION_BROKEN
# ---------------------------------------------------------------------------


def test_case2_chunks_present_no_files_raises(session: Session) -> None:
    # file_id points to a row that does NOT exist in repo_files
    chunks = [_make_chunk(uuid.uuid4(), "ghost.py")]
    with pytest.raises(FileResolutionError) as exc_info:
        resolve_files_from_chunks(chunks, session)
    assert str(exc_info.value) == "FILE_RESOLUTION_BROKEN"


def test_case2_partial_resolution_still_passes(session: Session) -> None:
    # If at least one file resolves, contract is satisfied (HARD FAIL only when 0).
    rf = _make_repo_file(session, "real.py")
    chunks = [
        _make_chunk(rf.id, "real.py"),
        _make_chunk(uuid.uuid4(), "ghost.py"),
    ]
    files = resolve_files_from_chunks(chunks, session)
    assert len(files) == 1
    assert files[0].id == rf.id


# ---------------------------------------------------------------------------
# CASE 3: chunks == 0 -> CONTEXT_EMPTY
# ---------------------------------------------------------------------------


def test_case3_no_chunks_returns_empty(session: Session) -> None:
    assert resolve_files_from_chunks([], session) == []


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_chunk_missing_file_id_raises(session: Session) -> None:
    rf = _make_repo_file(session, "a.py")
    good = _make_chunk(rf.id, "a.py")

    class _Bare:
        id = uuid.uuid4()
        file_id = None

    with pytest.raises(FileResolutionError) as exc_info:
        resolve_files_from_chunks([good, _Bare()], session)
    assert str(exc_info.value) == "FILE_RESOLUTION_BROKEN"


def test_resolution_is_deterministic(session: Session) -> None:
    rf_a = _make_repo_file(session, "a.py")
    rf_b = _make_repo_file(session, "b.py")
    rf_c = _make_repo_file(session, "c.py")

    chunks = [
        _make_chunk(rf_b.id),
        _make_chunk(rf_a.id),
        _make_chunk(rf_c.id),
        _make_chunk(rf_a.id),  # duplicate file_id
    ]

    first = [f.id for f in resolve_files_from_chunks(chunks, session)]
    second = [f.id for f in resolve_files_from_chunks(chunks, session)]
    third = [f.id for f in resolve_files_from_chunks(list(reversed(chunks)), session)]

    assert first == second == third
    # exactly the unique set, no duplicates
    assert sorted(first, key=str) == first
    assert set(first) == {rf_a.id, rf_b.id, rf_c.id}


def test_no_fallback_uses_only_file_id(session: Session) -> None:
    """
    If chunk.file_path matches an existing RepoFile.path but file_id does
    not match any RepoFile.id, resolution MUST FAIL — proving file_path
    is never used as a fallback identity source.
    """
    rf = _make_repo_file(session, "src/known.py")
    # chunk shares path but NOT id
    chunks = [_make_chunk(uuid.uuid4(), "src/known.py")]

    with pytest.raises(FileResolutionError) as exc_info:
        resolve_files_from_chunks(chunks, session)
    assert str(exc_info.value) == "FILE_RESOLUTION_BROKEN"
    # repo_id of the chunk must also not be used as a fallback
    assert rf.repo_id is not None


# ---------------------------------------------------------------------------
# Drift-prevention: static checks on the CTX_FILES code path (contract §9).
# These tests fail the build if the forbidden non-authoritative identity
# sources reappear inside chat_routes.py.
# ---------------------------------------------------------------------------


def _read_chat_routes_source() -> str:
    import inspect

    import backend.app.chat_routes as cr

    return inspect.getsource(cr)


def test_drift_no_ctx_file_paths_local_in_chat_routes() -> None:
    """`ctx_file_paths` was the legacy chunk.file_path mirror. It must not exist."""
    src = _read_chat_routes_source()
    assert "ctx_file_paths" not in src, (
        "ctx_file_paths re-introduced in chat_routes.py — "
        "REPO_CONTEXT_FILE_RESOLUTION_V1 forbids non-authoritative file path mapping."
    )


def test_drift_no_ctx_file_ids_local_in_chat_routes() -> None:
    """`ctx_file_ids` was the legacy parallel id list — replaced by resolve_files_from_chunks."""
    src = _read_chat_routes_source()
    assert "ctx_file_ids" not in src, (
        "ctx_file_ids re-introduced in chat_routes.py — "
        "REPO_CONTEXT_FILE_RESOLUTION_V1 forbids parallel identity sources."
    )


def test_drift_ctx_files_assignment_uses_resolver() -> None:
    """Legacy local ctx_files assignment must not reappear in chat routes."""
    src = _read_chat_routes_source()
    assert "ctx_files = [f.path for f in files]" not in src


def test_drift_resolver_imported_in_chat_routes() -> None:
    """Legacy direct resolver import in chat_routes should stay absent."""
    src = _read_chat_routes_source()
    assert "from backend.app.file_resolution import resolve_files_from_chunks" not in src
