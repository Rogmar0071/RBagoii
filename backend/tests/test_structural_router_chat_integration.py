from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_structural_chat")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_structural_chat.db"
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


def _seed_repo(file_count: int = 200) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoIndexRegistry

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-chat-structural",
                owner="acme",
                name="repo-chat-structural",
                branch="main",
                ingestion_status="success",
                total_files=file_count,
                total_chunks=file_count,
            )
        )
        for i in range(file_count):
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_path=f"src/file_{i}.py",
                    content=f"# file {i}\n",
                    chunk_index=0,
                    token_estimate=4,
                )
            )
        session.add(
            RepoIndexRegistry(
                repo_id=repo_id,
                total_files=file_count,
                total_chunks=file_count,
                indexed=True,
                status="indexed",
            )
        )
        session.commit()
    return str(repo_id)


def _chat(client: TestClient, *, repo_id: str, message: str):
    return client.post(
        "/api/chat",
        json={
            "message": message,
            "conversation_id": str(uuid.uuid4()),
            "context": {"repos": [repo_id]},
            "agent_mode": False,
        },
        headers=AUTH,
    )


def test_structural_how_many_files_bypasses_retrieval_and_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo(200)
    import backend.app.chat_routes as cr

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(
        cr,
        "retrieve_relevant_chunks",
        lambda *args, **kwargs: pytest.fail("retrieval called on structural path"),
    )
    monkeypatch.setattr(
        cr,
        "_call_openai_chat",
        lambda *args, **kwargs: pytest.fail("llm called on structural path"),
    )

    resp = _chat(client, repo_id=repo_id, message="how many files are in the repository")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "structural"
    assert body["file_count"] == 200
    assert len(body["files"]) == 200
    assert body["retrieved_chunks"] == 0
    assert body["retrieved_count"] == 0


def test_structural_list_all_files_matches_debug_endpoint(client: TestClient):
    repo_id = _seed_repo(200)
    chat_resp = _chat(client, repo_id=repo_id, message="list all files")
    assert chat_resp.status_code == 200, chat_resp.text
    chat_body = chat_resp.json()
    assert chat_body["type"] == "structural"
    assert chat_body["file_count"] == 200
    assert len(chat_body["files"]) == 200
    assert len(chat_body["files"]) == len(set(chat_body["files"]))
    assert chat_body["retrieved_chunks"] == 0

    debug_resp = client.get(f"/debug/structural/{repo_id}", headers=AUTH)
    assert debug_resp.status_code == 200, debug_resp.text
    debug_body = debug_resp.json()
    assert debug_body["count"] == chat_body["file_count"]
    assert debug_body["files"] == chat_body["files"]
