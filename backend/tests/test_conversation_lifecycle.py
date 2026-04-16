"""
Tests for CONVERSATION_LIFECYCLE_ENFORCEMENT_LOCK.

Validates:
  1. Conversation isolation — messages in conversation A are not visible in conversation B
  2. Missing conversation_id → 400 (legacy path removed)
  3. Stateless override — force_new_session=True skips history read but still persists
  4. Deletion — DELETE endpoint removes all messages for a conversation
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_lifecycle")

from backend.app.main import app  # noqa: E402
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_lifecycle.db"
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


def _new_conversation(client: TestClient) -> str:
    resp = client.post("/api/chat/conversation/new", headers=_auth())
    assert resp.status_code == 200, resp.text
    cid = resp.json()["conversation_id"]
    assert cid  # non-empty
    return cid


def _post_chat(
    client: TestClient,
    message: str,
    conversation_id: str,
    force_new_session: bool | None = None,
) -> dict:
    overrides: dict = {}
    if force_new_session is not None:
        overrides["force_new_session"] = force_new_session
    body = _chat_payload(message, conversation_id=conversation_id, **overrides)
    resp = client.post("/api/chat", json=body, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get_history(client: TestClient, conversation_id: str) -> list[dict]:
    resp = client.get("/api/chat", params={"conversation_id": conversation_id}, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


# ---------------------------------------------------------------------------
# Test 1 — Conversation isolation
# "Messages in conversation A MUST NOT appear in conversation B"
# ---------------------------------------------------------------------------


class TestConversationIsolation:
    def test_messages_isolated_between_conversations(
        self, client: TestClient, monkeypatch
    ):
        """
        Create two conversations A and B.
        Write distinct messages to each.
        Verify A's history contains only A's messages and B's history contains
        only B's messages.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid_a = _new_conversation(client)
        cid_b = _new_conversation(client)

        _post_chat(client, "Message from A", conversation_id=cid_a)
        _post_chat(client, "Message from B", conversation_id=cid_b)

        history_a = _get_history(client, conversation_id=cid_a)
        history_b = _get_history(client, conversation_id=cid_b)

        contents_a = {m["content"] for m in history_a}
        contents_b = {m["content"] for m in history_b}

        assert "Message from A" in contents_a, "A must contain its own message"
        assert "Message from B" not in contents_a, (
            "CRITICAL: conversation bleed — A sees B's message"
        )

        assert "Message from B" in contents_b, "B must contain its own message"
        assert "Message from A" not in contents_b, (
            "CRITICAL: conversation bleed — B sees A's message"
        )

    def test_conversation_id_stored_on_message(
        self, client: TestClient, monkeypatch
    ):
        """Each message returned by the API carries its conversation_id."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        resp = _post_chat(client, "Hello", conversation_id=cid)

        assert resp["user_message"]["conversation_id"] == cid
        assert resp["assistant_message"]["conversation_id"] == cid

    def test_history_scoped_to_active_conversation(
        self, client: TestClient, monkeypatch
    ):
        """
        AI call for conversation A must receive only A's prior messages, not B's.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        cid_a = _new_conversation(client)
        cid_b = _new_conversation(client)

        captured: list[list] = []

        def _capturing_call(msg, key, history=None, system_prompt=None):
            captured.append(list(history or []))
            return "stub"

        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Seed B with a message first (captured[0]).
            _post_chat(client, "Polluting message in B", conversation_id=cid_b)
            # Now send A's first message (captured[1]) — must NOT see B's history.
            _post_chat(client, "First message to A", conversation_id=cid_a)

        assert len(captured) == 2
        # A's first message: history window passed to OpenAI must be empty
        # because A has no prior messages — B's messages are invisible.
        assert captured[1] == [], (
            "CRITICAL: A's AI call received messages from B — cross-context leakage"
        )

    def test_create_conversation_returns_uuid4(self, client: TestClient):
        """POST /api/chat/conversation/new returns a non-empty UUID4 string."""
        import re

        uuid4_pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            re.IGNORECASE,
        )
        resp = client.post("/api/chat/conversation/new", headers=_auth())
        assert resp.status_code == 200
        cid = resp.json()["conversation_id"]
        assert uuid4_pattern.match(cid), f"Expected UUID4, got: {cid!r}"

    def test_create_conversation_no_reuse(self, client: TestClient):
        """Each call to /conversation/new returns a distinct id."""
        ids = {_new_conversation(client) for _ in range(5)}
        assert len(ids) == 5, "Conversation IDs must be unique"

    def test_create_conversation_requires_auth(self, client: TestClient):
        resp = client.post("/api/chat/conversation/new")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 2 — conversation_id is required (legacy path removed)
# ---------------------------------------------------------------------------


class TestConversationIdRequired:
    def test_missing_conversation_id_returns_400(
        self, client: TestClient, monkeypatch
    ):
        """POST /api/chat without conversation_id must return 400."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        resp = client.post(
            "/api/chat",
            json={"message": "No conversation_id here"},
            headers=_auth(),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_missing_message_still_returns_400(
        self, client: TestClient, monkeypatch
    ):
        """Validation order: missing 'message' returns 400 regardless of conversation_id."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        resp = client.post(
            "/api/chat",
            json={"context": {}},
            headers=_auth(),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_explicit_conversation_id_accepted(
        self, client: TestClient, monkeypatch
    ):
        """When conversation_id is provided, request is processed normally."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", conversation_id=cid),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["user_message"]["conversation_id"] == cid

    def test_no_message_stored_without_explicit_conversation_id(
        self, client: TestClient, monkeypatch
    ):
        """No message can be stored under an implicit fallback — the request fails."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import GlobalChatMessage

        resp = client.post(
            "/api/chat",
            json={"message": "Should not be stored"},
            headers=_auth(),
        )
        assert resp.status_code == 400

        with Session(db_module.get_engine()) as s:
            all_msgs = s.exec(select(GlobalChatMessage)).all()
        assert all_msgs == [], (
            "No messages must be persisted when conversation_id is absent"
        )


# ---------------------------------------------------------------------------
# Test 3 — Stateless override
# "force_new_session=True skips history read only; messages are still persisted"
# ---------------------------------------------------------------------------


class TestStatelessOverride:
    def test_stateless_still_persists_messages(
        self, client: TestClient, monkeypatch
    ):
        """force_new_session=True still writes to DB (RULE 5: history-skip only)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import GlobalChatMessage

        def _count() -> int:
            with Session(db_module.get_engine()) as s:
                return len(s.exec(select(GlobalChatMessage)).all())

        cid = _new_conversation(client)
        before = _count()
        _post_chat(client, "Stateless but persistent", cid, force_new_session=True)
        after = _count()

        assert after == before + 2, (
            f"force_new_session=True must persist user+assistant; "
            f"before={before} after={after}"
        )

    def test_stateless_does_not_read_from_db(
        self, client: TestClient, monkeypatch
    ):
        """
        With force_new_session=True, prior messages are not passed to the AI call
        even when they exist in the DB.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        cid = _new_conversation(client)

        captured: list[list] = []

        def _capturing_call(msg, key, history=None, system_prompt=None):
            captured.append(list(history or []))
            return "stub"

        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Seed with a real persisted message (captured[0]).
            _post_chat(client, "Persisted seed message", cid)
            # Stateless request on same conversation (captured[1]) — must not see history.
            _post_chat(client, "Stateless request", cid, force_new_session=True)

        assert len(captured) == 2
        assert captured[1] == [], (
            "force_new_session=True must bypass all DB reads — history must be empty"
        )

    def test_stateless_visible_in_history(
        self, client: TestClient, monkeypatch
    ):
        """force_new_session=True messages ARE visible in the conversation history."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        _post_chat(client, "Persisted message", cid)
        _post_chat(client, "Stateless message", cid, force_new_session=True)

        history = _get_history(client, conversation_id=cid)
        contents = {m["content"] for m in history}
        assert "Persisted message" in contents, "Normal message must appear in history"
        assert "Stateless message" in contents, (
            "force_new_session=True message must appear in history (RULE 5)"
        )

    def test_stateless_response_shape_complete(
        self, client: TestClient, monkeypatch
    ):
        """Stateless response still contains user_message and assistant_message."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        resp = _post_chat(client, "Stateless request", cid, force_new_session=True)
        assert "user_message" in resp
        assert "assistant_message" in resp
        assert resp["user_message"]["content"] == "Stateless request"
        assert resp["user_message"]["conversation_id"] == cid
        assert resp["assistant_message"]["conversation_id"] == cid


# ---------------------------------------------------------------------------
# Test 4 — Deletion
# "DELETE removes all messages for a conversation; others are untouched"
# ---------------------------------------------------------------------------


class TestConversationDeletion:
    def test_delete_clears_conversation_messages(
        self, client: TestClient, monkeypatch
    ):
        """After DELETE, GET returns empty history for the deleted conversation."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        _post_chat(client, "Message 1", conversation_id=cid)
        _post_chat(client, "Message 2", conversation_id=cid)

        # Verify messages exist.
        history = _get_history(client, conversation_id=cid)
        assert len(history) >= 2

        # Delete.
        del_resp = client.delete(
            f"/api/chat/conversation/{cid}", headers=_auth()
        )
        assert del_resp.status_code == 200, del_resp.text
        body = del_resp.json()
        assert body["deleted"] >= 2

        # History must now be empty.
        history_after = _get_history(client, conversation_id=cid)
        assert history_after == [], (
            f"Expected empty history after delete, got {len(history_after)} messages"
        )

    def test_delete_does_not_affect_other_conversations(
        self, client: TestClient, monkeypatch
    ):
        """Deleting conversation A must NOT remove messages from conversation B."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid_a = _new_conversation(client)
        cid_b = _new_conversation(client)

        _post_chat(client, "Keep this message", conversation_id=cid_b)
        _post_chat(client, "Delete this message", conversation_id=cid_a)

        # Delete A.
        del_resp = client.delete(
            f"/api/chat/conversation/{cid_a}", headers=_auth()
        )
        assert del_resp.status_code == 200

        # B must be intact.
        history_b = _get_history(client, conversation_id=cid_b)
        contents_b = {m["content"] for m in history_b}
        assert "Keep this message" in contents_b, (
            "CRITICAL: deleting A destroyed B's messages"
        )

    def test_delete_returns_count(self, client: TestClient, monkeypatch):
        """DELETE response reports exact count of deleted messages."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        _post_chat(client, "One", conversation_id=cid)
        _post_chat(client, "Two", conversation_id=cid)

        del_resp = client.delete(
            f"/api/chat/conversation/{cid}", headers=_auth()
        )
        assert del_resp.status_code == 200
        # Each _post_chat creates 2 messages (user + assistant).
        assert del_resp.json()["deleted"] == 4

    def test_delete_empty_conversation_returns_zero(
        self, client: TestClient, monkeypatch
    ):
        """Deleting a conversation that has no messages returns deleted=0."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = _new_conversation(client)
        del_resp = client.delete(
            f"/api/chat/conversation/{cid}", headers=_auth()
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] == 0

    def test_delete_requires_auth(self, client: TestClient):
        import uuid

        resp = client.delete(f"/api/chat/conversation/{uuid.uuid4()}")
        assert resp.status_code == 401

    def test_delete_legacy_default_explicitly_allowed(
        self, client: TestClient, monkeypatch
    ):
        """legacy_default CAN be cleared by calling DELETE with ?confirm=true."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        del_resp = client.delete(
            "/api/chat/conversation/legacy_default",
            params={"confirm": "true"},
            headers=_auth(),
        )
        assert del_resp.status_code == 200
        # No runtime code writes to legacy_default anymore; deletion returns 0.
        assert del_resp.json()["deleted"] >= 0

    def test_delete_legacy_default_without_confirm_rejected(
        self, client: TestClient, monkeypatch
    ):
        """DELETE legacy_default without ?confirm=true must be rejected (RULE 6)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        del_resp = client.delete(
            "/api/chat/conversation/legacy_default",
            headers=_auth(),
        )
        assert del_resp.status_code == 400
        assert del_resp.json()["error"]["code"] == "confirmation_required"
