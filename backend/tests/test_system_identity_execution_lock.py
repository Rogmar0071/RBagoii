from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")

from backend.app.chat_routes import _conversation_repo_ids  # noqa: E402
from backend.app.file_resolution import FileResolutionError, resolve_files_from_chunks  # noqa: E402
from backend.app.ingest_pipeline import _assert_chunk_file_integrity, _ingest_repo  # noqa: E402
from backend.app.models import (  # noqa: E402
    Conversation,
    ConversationContext,
    IngestJob,
    Repo,
    RepoChunk,
    RepoFile,
)
from backend.app.repo_retrieval import retrieve_relevant_chunks  # noqa: E402


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_identity_execution_lock.db"
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


def _repo_manifest() -> bytes:
    return json.dumps(
        {
            "repo_url": "https://github.com/example/identity-repo",
            "owner": "example",
            "name": "identity-repo",
            "branch": "main",
            "files": [
                {"path": "src/main.py", "content": "def main():\n    return 1\n", "size": 24}
            ],
            "skipped_files": [],
        }
    ).encode("utf-8")


def test_repo_cannot_be_bypassed_for_repo_ingestion(session: Session) -> None:
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/example/identity-repo@main",
        branch="main",
        status="created",
        blob_data=_repo_manifest(),
        blob_mime_type="application/json",
        blob_size_bytes=len(_repo_manifest()),
        repo_id=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        _ingest_repo(session, job)
    assert str(exc_info.value) == "INVALID_REPO_IDENTITY"


def test_repo_file_must_be_persisted_before_chunking(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.ingest_pipeline as ip

    repo = Repo(
        id=uuid.uuid4(),
        repo_url="https://github.com/example/identity-repo",
        owner="example",
        name="identity-repo",
        branch="main",
        ingestion_status="pending",
    )
    session.add(repo)
    session.commit()

    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/example/identity-repo@main",
        branch="main",
        status="created",
        blob_data=_repo_manifest(),
        blob_mime_type="application/json",
        blob_size_bytes=len(_repo_manifest()),
        repo_id=repo.id,
    )

    monkeypatch.setattr(ip, "sa_inspect", lambda _obj: SimpleNamespace(persistent=False))

    with pytest.raises(RuntimeError) as exc_info:
        _ingest_repo(session, job)
    assert str(exc_info.value) == "INGESTION_ORDER_VIOLATION"


def test_chunk_identity_and_path_integrity_checks() -> None:
    repo_file = SimpleNamespace(id=uuid.uuid4(), path="src/main.py")

    mismatched_chunk = SimpleNamespace(file_id=uuid.uuid4(), file_path="src/main.py")
    with pytest.raises(RuntimeError) as exc_info:
        _assert_chunk_file_integrity(repo_file, mismatched_chunk)
    assert str(exc_info.value) == "CHUNK_FILE_ID_MISMATCH"

    bad_path_chunk = SimpleNamespace(file_id=repo_file.id, file_path="src/other.py")
    with pytest.raises(RuntimeError) as exc_info:
        _assert_chunk_file_integrity(repo_file, bad_path_chunk)
    assert str(exc_info.value) == "CHUNK_FILE_PATH_MISMATCH"


def test_context_cannot_bind_to_invalid_repo(session: Session) -> None:
    conversation_id = str(uuid.uuid4())
    session.add(Conversation(id=conversation_id))
    session.add(
        ConversationContext(
            conversation_id=conversation_id,
            repo_id=uuid.uuid4(),
        )
    )
    session.commit()

    with pytest.raises(RuntimeError) as exc_info:
        _conversation_repo_ids(session, conversation_id)
    assert str(exc_info.value) == "INVALID_CONTEXT_REPO"


def test_retrieval_rejects_chunks_with_empty_file_path() -> None:
    chunk = RepoChunk(
        repo_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        file_path="src/main.py",
        content="x",
        chunk_index=0,
        token_estimate=1,
    )
    object.__setattr__(chunk, "file_path", "")

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeSession:
        def exec(self, _stmt):
            return _Result([chunk])

    with pytest.raises(RuntimeError) as exc_info:
        retrieve_relevant_chunks(
            user_query="main",
            db=_FakeSession(),  # type: ignore[arg-type]
            repo_ids=[uuid.uuid4()],
        )
    assert str(exc_info.value) == "INVALID_CHUNK_SHAPE"


def test_file_resolution_never_uses_fallback(session: Session) -> None:
    repo_file = RepoFile(
        id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        path="src/main.py",
        language="python",
        size_bytes=10,
    )
    session.add(repo_file)
    session.commit()

    chunk = RepoChunk(
        repo_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        file_path="src/main.py",
        content="x",
        chunk_index=0,
        token_estimate=1,
    )

    with pytest.raises(FileResolutionError) as exc_info:
        resolve_files_from_chunks([chunk], session)
    assert str(exc_info.value) == "FILE_RESOLUTION_BROKEN"
