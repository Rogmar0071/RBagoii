from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_retrieval_chunk_integrity")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_retrieval_chunk_integrity.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _seed_repo(conversation_id: str) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoFile

    repo_id = uuid.uuid4()
    file_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                conversation_id=conversation_id,
                repo_url="https://github.com/acme/integrity-repo",
                owner="acme",
                name="integrity-repo",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
            )
        )
        session.add(
            RepoFile(
                id=file_id,
                repo_id=repo_id,
                path="src/main.py",
                language="python",
                size_bytes=10,
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_id,
                file_path="src/main.py",
                content="def answer():\n    return 42\n",
                chunk_index=0,
                token_estimate=8,
                graph_group=str(uuid.uuid4()),
            )
        )
        session.commit()
    return str(repo_id)


def _make_invalid_chunk():
    from backend.app.models import RepoChunk

    chunk = RepoChunk(
        repo_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        file_path="src/bad.py",
        content="bad",
        chunk_index=0,
        token_estimate=1,
    )
    object.__setattr__(chunk, "file_id", None)
    return chunk


def test_retrieve_relevant_chunks_raises_invalid_chunk_shape_on_null_file_id():
    from backend.app.repo_retrieval import retrieve_relevant_chunks

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        def exec(self, _stmt):
            return _Result(self._rows)

    with pytest.raises(RuntimeError) as exc_info:
        retrieve_relevant_chunks(
            user_query="answer function",
            db=_FakeSession([_make_invalid_chunk()]),  # type: ignore[arg-type]
            repo_ids=[uuid.uuid4()],
        )
    assert str(exc_info.value) == "INVALID_CHUNK_SHAPE"


def test_chat_returns_http_200_with_invalid_chunk_shape_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo(conversation_id=str(uuid.uuid4()))

    import backend.app.chat_routes as cr

    monkeypatch.setattr(
        cr,
        "_run_retrieval_query",
        lambda **_kwargs: [_make_invalid_chunk()],
    )

    resp = client.post(
        "/api/chat",
        json={
            "message": "where is answer defined",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("error_code") in {None, "INVALID_CHUNK_SHAPE"}
