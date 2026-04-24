from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_context_chat_spine_v11")

from backend.app.main import app  # noqa: E402
from backend.app.models import CodeSymbol, EntryPoint, IngestJob, RepoChunk, RepoFile  # noqa: E402
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_context_chat_spine_v11.db"
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


def _seed_ingest_context(conversation_id: str) -> str:
    import backend.app.database as db_module

    job_id = uuid.uuid4()
    file_id = uuid.uuid4()
    with Session(db_module.get_engine()) as db:
        db.add(
            IngestJob(
                id=job_id,
                kind="repo",
                source="https://github.com/acme/context-spine@main",
                branch="main",
                status="success",
                conversation_id=conversation_id,
                file_count=1,
                chunk_count=1,
            )
        )
        db.add(
            RepoFile(
                id=file_id,
                repo_id=job_id,
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
                ingest_job_id=job_id,
                file_id=file_id,
                file_path="app.py",
                content="def main():\n    return 'ok'\n",
                chunk_index=0,
                token_estimate=6,
            )
        )
        db.commit()
    return str(job_id)


def test_chat_requires_session(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]
    _seed_ingest_context(cid)

    resp = client.post(
        "/api/chat",
        json=_chat_payload(
            "explain app flow",
            conversation_id=cid,
            alignment_confirmed=False,
        ),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ALIGNMENT_REQUIRED"
    assert isinstance(body["summary"], dict)


def test_chat_blocks_without_ingest(client: TestClient):
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]
    resp = client.post(
        "/api/chat",
        json=_chat_payload("hello", conversation_id=cid, alignment_confirmed=True),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] == "FINALIZE_BLOCKED"
    assert body["details"] == "NO_INGEST_CONTEXT"


def test_session_reuse_same_intent(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]
    _seed_ingest_context(cid)

    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_run_chat_llm", lambda *args, **kwargs: "Grounded repo answer.")

    r1 = client.post(
        "/api/chat",
        json=_chat_payload("map execution path", conversation_id=cid, alignment_confirmed=True),
        headers=_auth(),
    )
    assert r1.status_code == 200, r1.text
    sid1 = r1.json()["details"]["session_id"]

    r2 = client.post(
        "/api/chat",
        json=_chat_payload("map execution path", conversation_id=cid, alignment_confirmed=True),
        headers=_auth(),
    )
    assert r2.status_code == 200, r2.text
    sid2 = r2.json()["details"]["session_id"]

    assert sid1 == sid2


def test_session_invalidates_on_intent_change(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    cid = client.post("/api/chat/conversation/new", headers=_auth()).json()["conversation_id"]
    _seed_ingest_context(cid)

    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_run_chat_llm", lambda *args, **kwargs: "Grounded repo answer.")

    r1 = client.post(
        "/api/chat",
        json=_chat_payload("intent one", conversation_id=cid, alignment_confirmed=True),
        headers=_auth(),
    )
    assert r1.status_code == 200, r1.text
    sid1 = r1.json()["details"]["session_id"]

    r2 = client.post(
        "/api/chat",
        json=_chat_payload("intent two", conversation_id=cid, alignment_confirmed=True),
        headers=_auth(),
    )
    assert r2.status_code == 200, r2.text
    sid2 = r2.json()["details"]["session_id"]

    assert sid1 != sid2


def test_no_execution_without_session():
    import backend.app.chat_routes as cr

    with pytest.raises(RuntimeError, match="SESSION_REQUIRED"):
        cr._run_retrieval_query(  # type: ignore[attr-defined]
            user_query="x",
            db=None,  # type: ignore[arg-type]
        )


def test_forbidden_patterns_not_present_in_chat_routes():
    text = Path("/home/runner/work/RBagoii/RBagoii/backend/app/chat_routes.py").read_text(
        encoding="utf-8"
    )
    assert "retrieve_relevant_chunks(" not in text
    assert "handle_structural_query(" not in text
