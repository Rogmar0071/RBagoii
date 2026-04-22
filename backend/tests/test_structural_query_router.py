from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_structural_router")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_structural_router.db"
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


def _seed_indexed_repo(file_paths: list[str]) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoIndexRegistry

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-structural",
                owner="acme",
                name="repo-structural",
                branch="main",
                ingestion_status="success",
                total_files=len(file_paths),
                total_chunks=len(file_paths),
            )
        )
        for idx, file_path in enumerate(file_paths):
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_path=file_path,
                    content=f"# chunk {idx}\n",
                    chunk_index=0,
                    token_estimate=8,
                )
            )
        session.add(
            RepoIndexRegistry(
                repo_id=repo_id,
                total_files=len(file_paths),
                total_chunks=len(file_paths),
                indexed=True,
                status="indexed",
            )
        )
        session.commit()
    return str(repo_id)


def _chat(client: TestClient, *, message: str, repo_id: str, conversation_id: str | None = None):
    cid = conversation_id or str(uuid.uuid4())
    return client.post(
        "/api/chat",
        json={
            "message": message,
            "conversation_id": cid,
            "agent_mode": False,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )


def test_s1_file_count_determinism(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    repo_id = _seed_indexed_repo(["README.md", "src/main.py", "src/utils/helpers.py"])

    import backend.app.chat_routes as cr

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: pytest.fail("LLM called"))

    resp = _chat(client, message="how many files", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["count"] == 3
    assert body["source"] == "index"
    assert body["error_code"] is None
    assert body["total_chunks"] == body["retrieved_chunks"] == 3


def test_s2_full_file_list_no_truncation_or_duplicates(client: TestClient):
    file_paths = [f"src/file_{i}.py" for i in range(25)]
    repo_id = _seed_indexed_repo(file_paths)

    resp = _chat(client, message="list all files", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    files = body["data"]["files"]
    assert len(files) == len(file_paths)
    assert len(files) == len(set(files))
    assert sorted(files) == sorted(file_paths)


def test_s3_structure_output_deterministic(client: TestClient):
    repo_id = _seed_indexed_repo(["src/main.py", "src/api/routes.py", "docs/readme.md"])
    resp = _chat(client, message="show repo structure", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["structure"] == {
        "docs": {"readme.md": {}},
        "src": {"api": {"routes.py": {}}, "main.py": {}},
    }


def test_s4_structural_query_bypasses_retrieval(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    repo_id = _seed_indexed_repo(["src/main.py"])
    import backend.app.chat_routes as cr

    monkeypatch.setattr(
        cr,
        "retrieve_relevant_chunks",
        lambda *args, **kwargs: pytest.fail("retrieval called"),
    )
    resp = _chat(client, message="list files", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["error_code"] is None


def test_s5_structural_query_blocks_llm(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    repo_id = _seed_indexed_repo(["src/main.py"])
    import backend.app.chat_routes as cr

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: pytest.fail("LLM called"))
    resp = _chat(client, message="repository structure", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "index"


def test_s6_semantic_query_uses_retrieval_not_structural(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_indexed_repo(["src/main.py"])
    import backend.app.chat_routes as cr
    from backend.app.models import RepoChunk

    calls = {"retrieval": 0}

    def _fake_retrieval(*args, **kwargs):
        calls["retrieval"] += 1
        rid = uuid.UUID(repo_id)
        return [
            RepoChunk(
                repo_id=rid,
                file_path="src/main.py",
                content="def answer(): return 42",
                chunk_index=0,
                token_estimate=8,
            )
        ]

    monkeypatch.setattr(cr, "retrieve_relevant_chunks", _fake_retrieval)
    monkeypatch.setattr(
        cr,
        "handle_structural_query",
        lambda *args, **kwargs: pytest.fail("structural handler called for semantic query"),
    )

    resp = _chat(client, message="where is the answer function", repo_id=repo_id)
    assert resp.status_code == 200, resp.text
    assert calls["retrieval"] >= 1
    assert resp.json()["source"] is None


def test_s7_incomplete_index_returns_insufficient_context(client: TestClient):
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-missing-index",
                owner="acme",
                name="repo-missing-index",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_path="src/main.py",
                content="print('x')",
                chunk_index=0,
                token_estimate=4,
            )
        )
        session.commit()

    resp = _chat(client, message="list all files", repo_id=str(repo_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_code"] == "INSUFFICIENT_CONTEXT"

