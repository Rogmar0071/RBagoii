from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_repo_contract")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_repo_contract.db"
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


def _seed_repo_with_chunks(conversation_id: str, repo_name: str = "repo-a") -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        repo = Repo(
            id=repo_id,
            conversation_id=conversation_id,
            repo_url=f"https://github.com/acme/{repo_name}",
            owner="acme",
            name=repo_name,
            branch="main",
            ingestion_status="success",
            total_files=2,
            total_chunks=2,
        )
        session.add(repo)
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_path="src/main.py",
                content="def answer():\n    return 42\n",
                chunk_index=0,
                token_estimate=8,
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_path="README.md",
                content="Project contract and setup instructions.",
                chunk_index=0,
                token_estimate=8,
            )
        )
        session.commit()
    return str(repo_id)


def test_visibility_structure_reports_indexed_surface(client: TestClient):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    resp = client.get(f"/repos/{repo_id}/structure", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_files"] > 0
    assert body["total_chunks"] > 0
    assert len(body["files"]) == body["total_files"]


def test_retrieval_is_repo_scoped(client: TestClient):
    conv_id = str(uuid.uuid4())
    repo_a = _seed_repo_with_chunks(conversation_id=conv_id, repo_name="repo-a")
    _ = _seed_repo_with_chunks(conversation_id=conv_id, repo_name="repo-b")

    resp = client.post(
        f"/repos/{repo_a}/retrieve",
        json={"query": "answer function", "top_k": 12},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieved_count"] > 0
    assert all(
        chunk["file_path"] in {"src/main.py", "README.md"} for chunk in body["retrieved_chunks"]
    )


def test_retrieval_is_deterministic_for_same_query(client: TestClient):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    results = []
    for _ in range(3):
        resp = client.post(
            f"/repos/{repo_id}/retrieve",
            json={"query": "project setup", "top_k": 12},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        results.append(resp.json()["retrieved_chunks"])
    assert results[0] == results[1] == results[2]


def test_retrieval_failure_surfaces_contract_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    import backend.app.github_routes as gr

    monkeypatch.setattr(gr, "_score_chunk", _boom)
    resp = client.post(
        f"/repos/{repo_id}/retrieve",
        json={"query": "answer", "top_k": 12},
        headers=AUTH,
    )
    assert resp.status_code == 500
    assert resp.json().get("detail") == "RETRIEVAL_FAILURE"


def test_chat_grounding_uses_repo_context(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    def _fake_openai(message, key, history=None, system_prompt=None):  # noqa: ARG001
        if system_prompt and "FILE: src/main.py" in system_prompt:
            return "Grounded in src/main.py"
        return "Not grounded"

    monkeypatch.setattr(cr, "_call_openai_chat", _fake_openai)

    resp = client.post(
        "/api/chat",
        json={
            "message": "Where is the answer function?",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "src/main.py" in resp.json()["reply"]


def test_chat_returns_no_index_data_when_retrieval_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "retrieve_relevant_chunks", lambda *args, **kwargs: [])
    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: "should-not-run")
    resp = client.post(
        "/api/chat",
        json={
            "message": "Explain repository setup",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"] == "NO_INDEX_DATA"
