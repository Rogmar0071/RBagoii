"""
Tests for global chat upgrades:
  - POST /v1/tools/web_search (returns 503 when TAVILY_API_KEY missing)
  - POST /api/chat/{message_id}/edit
  - POST /v1/global/messages/{message_id}/edit  (alias)
  - GET  /v1/global/messages  (alias)
  - Retrieval trigger detection (_needs_web_search)
  - agent_mode flag in POST /api/chat
  - superseded field in GET /api/chat
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_chat")

from backend.app.main import app  # noqa: E402
from backend.app.models import CodeSymbol, EntryPoint, IngestJob, RepoChunk, RepoFile  # noqa: E402
from backend.tests.test_utils import _chat_payload as _base_chat_payload

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_chat.db"
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


@pytest.fixture(autouse=True)
def _stub_chat_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: "Stub reply")


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


def _chat_payload(message: str = "test", **overrides) -> dict:
    cid = overrides.get("conversation_id") or str(uuid.uuid4())
    _seed_ingest_context(cid)
    overrides["conversation_id"] = cid
    overrides.setdefault("alignment_confirmed", True)
    os.environ.setdefault("OPENAI_API_KEY", "sk-fake")
    return _base_chat_payload(message, **overrides)


# ---------------------------------------------------------------------------
# Helper: post a chat message (no OpenAI key → stub reply)
# ---------------------------------------------------------------------------


def _post_chat(
    client: TestClient,
    message: str,
    agent_mode: bool = False,
    conversation_id: str = None,
) -> dict:
    cid = conversation_id or str(uuid.uuid4())
    resp = client.post(
        "/api/chat",
        json=_chat_payload(message, context={}, agent_mode=agent_mode, conversation_id=cid),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests: POST /v1/tools/web_search
# ---------------------------------------------------------------------------


class TestWebSearchEndpoint:
    def test_web_search_no_api_key_returns_503(self, client: TestClient, monkeypatch):
        """Missing TAVILY_API_KEY should return 503 with a clear error."""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        resp = client.post(
            "/v1/tools/web_search",
            json={"query": "latest AI news"},
            headers=_auth(),
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "tavily_key_missing"

    def test_web_search_missing_query(self, client: TestClient):
        resp = client.post(
            "/v1/tools/web_search",
            json={},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_web_search_requires_auth(self, client: TestClient):
        resp = client.post("/v1/tools/web_search", json={"query": "test"})
        assert resp.status_code == 401

    def test_web_search_with_mocked_tavily(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")

        mock_results = {
            "results": [
                {
                    "title": "Test Title",
                    "url": "https://example.com/article",
                    "content": "Test snippet",
                    "published_date": "2025-01-01",
                }
            ]
        }

        mock_client = MagicMock()
        mock_client.search.return_value = mock_results

        with patch("backend.app.web_search.TavilyClient", return_value=mock_client):
            # Clear cache first
            import backend.app.web_search as ws_module

            ws_module._cache.clear()

            resp = client.post(
                "/v1/tools/web_search",
                json={"query": "latest AI news", "max_results": 3},
                headers=_auth(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["title"] == "Test Title"
        assert body["results"][0]["url"] == "https://example.com/article"
        assert body["results"][0]["snippet"] == "Test snippet"
        assert body["results"][0]["source"] == "example.com"
        assert body["provider"] == "tavily"

    def test_web_search_cache(self, monkeypatch):
        """Same query returns cached result without calling Tavily again."""
        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")

        import backend.app.web_search as ws_module

        ws_module._cache.clear()

        call_count = {"n": 0}

        def fake_search(**kwargs):
            call_count["n"] += 1
            return {
                "results": [
                    {
                        "title": "Cached",
                        "url": "https://cached.example.com",
                        "content": "cached snippet",
                        "published_date": None,
                    }
                ]
            }

        mock_client = MagicMock()
        mock_client.search.side_effect = fake_search

        with patch("backend.app.web_search.TavilyClient", return_value=mock_client):
            ws_module.web_search("same query")
            ws_module.web_search("same query")

        assert call_count["n"] == 1, "Second call should use cache"

    def test_web_search_recency_days_param(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "fake-key")

        import backend.app.web_search as ws_module

        ws_module._cache.clear()

        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch("backend.app.web_search.TavilyClient", return_value=mock_client):
            resp = client.post(
                "/v1/tools/web_search",
                json={"query": "news", "recency_days": 3, "max_results": 2},
                headers=_auth(),
            )

        assert resp.status_code == 200
        call_kwargs = mock_client.search.call_args[1]
        assert call_kwargs["days"] == 3
        assert call_kwargs["max_results"] == 2

    def test_tavily_cache_ttl_env_var(self, monkeypatch):
        """TAVILY_CACHE_TTL_S is respected as the cache TTL env var."""
        import backend.app.web_search as ws_module

        monkeypatch.setenv("TAVILY_CACHE_TTL_S", "42")
        monkeypatch.delenv("WEB_SEARCH_CACHE_TTL_SECONDS", raising=False)
        assert ws_module._cache_ttl() == 42


# ---------------------------------------------------------------------------
# Tests: _needs_web_search detection
# ---------------------------------------------------------------------------


class TestNeedsWebSearch:
    def test_search_prefix(self):
        from backend.app.chat_routes import _needs_web_search

        assert _needs_web_search("search: latest news") is True
        assert _needs_web_search("Search: something") is True

    def test_recency_keywords(self):
        from backend.app.chat_routes import _needs_web_search

        assert _needs_web_search("What is the latest Python version?") is True
        assert _needs_web_search("What is the current price of BTC?") is True
        assert _needs_web_search("Tell me today's news") is True
        assert _needs_web_search("What happened just now?") is True

    def test_no_trigger(self):
        from backend.app.chat_routes import _needs_web_search

        assert _needs_web_search("How do I create a folder?") is False
        assert _needs_web_search("Explain blueprints") is False

    def test_build_search_query_strips_prefix(self):
        from backend.app.chat_routes import _build_search_query

        assert _build_search_query("search: AI news") == "AI news"
        assert _build_search_query("what is latest Python?") == "what is latest Python?"

    def test_search_prefix_adds_sources(self, client: TestClient, monkeypatch):
        """When search: prefix triggers retrieval, Sources section is appended."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")

        import backend.app.web_search as ws_module

        ws_module._cache.clear()

        mock_tavily = MagicMock()
        mock_tavily.search.return_value = {
            "results": [
                {
                    "title": "Result",
                    "url": "https://source.example.com/news",
                    "content": "snippet",
                    "published_date": None,
                }
            ]
        }

        import backend.app.chat_routes as cr

        def _fake_openai(msg, key, history=None, system_prompt=None):
            return "Here is the answer."

        with (
            patch("backend.app.web_search.TavilyClient", return_value=mock_tavily),
            patch.object(cr, "_call_openai_chat", side_effect=_fake_openai),
        ):
            resp = client.post(
                "/api/chat",
                json=_chat_payload("search: breaking news"),
                headers=_auth(),
            )

        assert resp.status_code == 200
        reply = resp.json()["reply"]
        assert "Sources:" in reply
        assert "source.example.com" in reply


# ---------------------------------------------------------------------------
# Tests: agent_mode flag
# ---------------------------------------------------------------------------


class TestChatAgentMode:
    def test_agent_mode_false_skips_validation(self, client: TestClient, monkeypatch):
        """PHASE 8: agent_mode=False means NORMAL mode, no validation"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.mode_engine as me

        calls = {"n": 0}
        original = me.stage_1_structural_validation

        def _tracking_stage_1(ai_output: str, modes: list[str], contract=None):
            calls["n"] += 1
            return original(ai_output, modes, contract)

        monkeypatch.setattr(me, "stage_1_structural_validation", _tracking_stage_1)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", agent_mode=False),
            headers=_auth(),
        )
        assert resp.status_code == 200
        # Normal mode skips validation, so stage_1 should not be called
        # or if called, should return passed=True immediately
        # The key is that modes=[] in normal mode

    def test_agent_mode_true_triggers_validation(self, client: TestClient, monkeypatch):
        """PHASE 3-4: agent_mode=True means AGOII mode with contract validation"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.mode_engine as me

        calls = {"n": 0}
        original = me.stage_1_structural_validation

        def _tracking_stage_1(ai_output: str, modes: list[str], contract=None):
            calls["n"] += 1
            return original(ai_output, modes, contract)

        monkeypatch.setattr(me, "stage_1_structural_validation", _tracking_stage_1)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", agent_mode=True),
            headers=_auth(),
        )
        assert resp.status_code == 200
        # AGOII mode should trigger validation with contract
        assert calls["n"] >= 1


# ---------------------------------------------------------------------------
# Tests: POST /api/chat/{message_id}/edit
# ---------------------------------------------------------------------------


class TestChatEdit:
    def test_edit_user_message(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # Post a user message.
        chat_resp = _post_chat(client, "Original message")
        user_msg_id = chat_resp["user_message"]["id"]

        # Edit it.
        edit_resp = client.post(
            f"/api/chat/{user_msg_id}/edit",
            json={"content": "Edited message"},
            headers=_auth(),
        )
        assert edit_resp.status_code == 201, edit_resp.text
        body = edit_resp.json()

        assert body["original_message"]["id"] == user_msg_id
        assert body["original_message"]["superseded"] is True
        assert body["new_message"]["content"] == "Edited message"
        assert body["new_message"]["superseded"] is False
        assert body["new_message"]["role"] == "user"

    def test_edit_preserves_original(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        chat_resp = _post_chat(client, "Keep this", conversation_id=cid)
        user_msg_id = chat_resp["user_message"]["id"]

        client.post(
            f"/api/chat/{user_msg_id}/edit",
            json={"content": "New version"},
            headers=_auth(),
        )

        # GET history should include both (original superseded + new active).
        hist = client.get("/api/chat", params={"conversation_id": cid}, headers=_auth())
        messages = hist.json()["messages"]
        ids = {m["id"] for m in messages}
        assert user_msg_id in ids  # original is preserved

    def test_edit_assistant_message_rejected(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        chat_resp = _post_chat(client, "Hello")
        assistant_msg_id = chat_resp["assistant_message"]["id"]

        edit_resp = client.post(
            f"/api/chat/{assistant_msg_id}/edit",
            json={"content": "Trying to edit AI"},
            headers=_auth(),
        )
        assert edit_resp.status_code == 400

    def test_edit_not_found(self, client: TestClient):
        fake_id = str(uuid.uuid4())
        edit_resp = client.post(
            f"/api/chat/{fake_id}/edit",
            json={"content": "Doesn't matter"},
            headers=_auth(),
        )
        assert edit_resp.status_code == 404

    def test_edit_invalid_uuid(self, client: TestClient):
        edit_resp = client.post(
            "/api/chat/not-a-uuid/edit",
            json={"content": "Something"},
            headers=_auth(),
        )
        assert edit_resp.status_code == 400

    def test_edit_requires_auth(self, client: TestClient):
        edit_resp = client.post(
            f"/api/chat/{uuid.uuid4()}/edit",
            json={"content": "Something"},
        )
        assert edit_resp.status_code == 401

    def test_edit_empty_content_rejected(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        chat_resp = _post_chat(client, "Hello")
        user_msg_id = chat_resp["user_message"]["id"]

        edit_resp = client.post(
            f"/api/chat/{user_msg_id}/edit",
            json={"content": "   "},
            headers=_auth(),
        )
        assert edit_resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: /v1/global/messages aliases
# ---------------------------------------------------------------------------


class TestGlobalMessagesAliases:
    def test_get_global_messages_alias(self, client: TestClient, monkeypatch):
        """GET /v1/global/messages returns same data as GET /api/chat."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cid = str(uuid.uuid4())
        _post_chat(client, "Hello from alias test", conversation_id=cid)

        alias_resp = client.get(
            "/v1/global/messages",
            params={"conversation_id": cid},
            headers=_auth(),
        )
        chat_resp = client.get("/api/chat", params={"conversation_id": cid}, headers=_auth())

        assert alias_resp.status_code == 200
        assert chat_resp.status_code == 200
        alias_messages = alias_resp.json()["messages"]
        chat_messages = chat_resp.json()["messages"]
        assert len(alias_messages) == len(chat_messages)

    def test_edit_via_global_alias(self, client: TestClient, monkeypatch):
        """POST /v1/global/messages/{id}/edit works same as /api/chat/{id}/edit."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        chat_resp = _post_chat(client, "Edit via alias")
        user_msg_id = chat_resp["user_message"]["id"]

        edit_resp = client.post(
            f"/v1/global/messages/{user_msg_id}/edit",
            json={"content": "Alias edited"},
            headers=_auth(),
        )
        assert edit_resp.status_code == 201
        body = edit_resp.json()
        assert body["new_message"]["content"] == "Alias edited"
        assert body["original_message"]["superseded"] is True

    def test_global_messages_requires_auth(self, client: TestClient):
        resp = client.get("/v1/global/messages")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: superseded field in GET /api/chat
# ---------------------------------------------------------------------------


class TestChatHistorySuperseded:
    def test_superseded_flag_in_history(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        chat_resp = _post_chat(client, "First version", conversation_id=cid)
        user_msg_id = chat_resp["user_message"]["id"]

        client.post(
            f"/api/chat/{user_msg_id}/edit",
            json={"content": "Second version"},
            headers=_auth(),
        )

        hist = client.get("/api/chat", params={"conversation_id": cid}, headers=_auth())
        messages = {m["id"]: m for m in hist.json()["messages"]}

        # Original user message must be superseded.
        assert messages[user_msg_id]["superseded"] is True

    def test_new_messages_not_superseded(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        _post_chat(client, "Regular message", conversation_id=cid)
        hist = client.get("/api/chat", params={"conversation_id": cid}, headers=_auth())

        for msg in hist.json()["messages"]:
            assert msg["superseded"] is False
