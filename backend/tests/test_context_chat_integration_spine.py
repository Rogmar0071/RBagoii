from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_context_chat_spine")

from backend.app.main import app  # noqa: E402
from backend.app.models import CodeSymbol, EntryPoint, Repo, RepoChunk, RepoFile, RepoIndexRegistry  # noqa: E402
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_context_chat_spine.db"
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


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed_pipeline_ready_repo() -> str:
    import backend.app.database as db_module

    repo_id = uuid.uuid4()
    file_id = uuid.uuid4()
    with Session(db_module.get_engine()) as db:
        db.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/context-spine",
                owner="acme",
                name="context-spine",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
            )
        )
        db.add(
            RepoFile(
                id=file_id,
                repo_id=repo_id,
                path="app.py",
                language="python",
                size_bytes=100,
            )
        )
        db.add(
            CodeSymbol(
                file_id=file_id,
                name="main",
                symbol_type="function",
                start_line=1,
                end_line=3,
            )
        )
        db.add(EntryPoint(file_id=file_id, entry_type="main", line=1))
        db.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_id,
                file_path="app.py",
                content="def main():\n    return 'ok'\n",
                chunk_index=0,
                token_estimate=6,
            )
        )
        db.add(
            RepoIndexRegistry(
                repo_id=repo_id,
                total_files=1,
                total_chunks=1,
                indexed=True,
                status="indexed",
            )
        )
        db.commit()
    return str(repo_id)


def _load_repo_chunks(repo_id: str) -> list[RepoChunk]:
    import backend.app.database as db_module

    with Session(db_module.get_engine()) as db:
        return list(
            db.exec(select(RepoChunk).where(RepoChunk.repo_id == uuid.UUID(repo_id))).all()
        )


def test_chat_returns_alignment_required_when_not_confirmed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    repo_id = _seed_pipeline_ready_repo()
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]

    resp = client.post(
        "/api/chat",
        json=_chat_payload(
            "explain app flow",
            conversation_id=cid,
            context={"repos": [repo_id]},
            alignment_confirmed=False,
        ),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ALIGNMENT_REQUIRED"
    assert isinstance(body["summary"], dict)


def test_chat_second_pass_executes_with_alignment_confirmed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    repo_id = _seed_pipeline_ready_repo()
    repo_chunks = _load_repo_chunks(repo_id)
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]

    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_run_retrieval_query", lambda *args, **kwargs: repo_chunks)
    monkeypatch.setattr(cr, "_run_chat_llm", lambda *args, **kwargs: "Grounded repo answer.")

    first = client.post(
        "/api/chat",
        json=_chat_payload(
            "explain app flow",
            conversation_id=cid,
            context={"repos": [repo_id]},
            alignment_confirmed=False,
        ),
        headers=_auth(),
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "ALIGNMENT_REQUIRED"

    second = client.post(
        "/api/chat",
        json=_chat_payload(
            "explain app flow",
            conversation_id=cid,
            context={"repos": [repo_id]},
            alignment_confirmed=True,
        ),
        headers=_auth(),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body.get("status") != "ALIGNMENT_REQUIRED"
    assert second_body.get("error") != "FINALIZE_BLOCKED"
    assert second_body["reply"] != "CONTEXT_NOT_ACTIVATED"

    ctx = client.get(f"/api/chat/conversation/{cid}/context", headers=_auth())
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["active_context_session_id"] is not None


def test_chat_without_repo_returns_deterministic_finalize_blocked(client: TestClient):
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]

    resp = client.post(
        "/api/chat",
        json=_chat_payload(
            "hello",
            conversation_id=cid,
            alignment_confirmed=True,
        ),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] == "FINALIZE_BLOCKED"
