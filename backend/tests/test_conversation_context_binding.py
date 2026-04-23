from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")

from backend.app.main import app  # noqa: E402
from backend.app.models import Repo  # noqa: E402
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_context_binding.db"
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


def _insert_repo(repo_id: uuid.UUID) -> None:
    import backend.app.database as db_module

    with Session(db_module.get_engine()) as db:
        db.add(
            Repo(
                id=repo_id,
                repo_url=f"https://github.com/example/repo-{repo_id.hex[:8]}",
                owner="example",
                name=f"repo-{repo_id.hex[:8]}",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()


def test_create_conversation_initializes_context_binding(client: TestClient):
    create = client.post("/api/chat/conversation/new", headers=_auth())
    assert create.status_code == 200, create.text
    cid = create.json()["conversation_id"]

    ctx = client.get(f"/api/chat/conversation/{cid}/context", headers=_auth())
    assert ctx.status_code == 200, ctx.text
    payload = ctx.json()
    assert payload["conversation_id"] == cid
    assert payload["repo_id"] is None


def test_context_binding_endpoint_returns_404_for_unknown_conversation(client: TestClient):
    cid = str(uuid.uuid4())
    resp = client.get(f"/api/chat/conversation/{cid}/context", headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "context_binding_not_found"


def test_chat_rejects_multiple_repo_ids_for_active_binding(client: TestClient, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    create = client.post("/api/chat/conversation/new", headers=_auth())
    cid = create.json()["conversation_id"]

    repo_a = uuid.uuid4()
    repo_b = uuid.uuid4()
    _insert_repo(repo_a)
    _insert_repo(repo_b)

    resp = client.post(
        "/api/chat",
        json=_chat_payload(
            "test",
            conversation_id=cid,
            context={"repos": [str(repo_a), str(repo_b)]},
        ),
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_chat_updates_active_repo_binding(client: TestClient, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    create = client.post("/api/chat/conversation/new", headers=_auth())
    cid = create.json()["conversation_id"]

    repo_id = uuid.uuid4()
    _insert_repo(repo_id)

    chat = client.post(
        "/api/chat",
        json=_chat_payload(
            "where is main?",
            conversation_id=cid,
            context={"repos": [str(repo_id)]},
        ),
        headers=_auth(),
    )
    assert chat.status_code == 200, chat.text

    ctx = client.get(f"/api/chat/conversation/{cid}/context", headers=_auth())
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["repo_id"] == str(repo_id)
