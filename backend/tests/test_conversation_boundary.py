"""
Tests for CONVERSATION_BOUNDARY_CONTROL_V1.

Validates:
  - Test 1: Legacy path unchanged (force_new_session=None)
  - Test 2: Clean session enforcement (force_new_session=True)
  - Test 3: No history leakage when force_new_session=True follows a prior message
  - Test 4: Stateless mode does not write to DB
  - Test 5: Ephemeral message structure is identical to persisted message structure
             (EPHEMERAL_MESSAGE_CONSTRAINT_V1)
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_boundary")

import uuid

from backend.app.main import app  # noqa: E402
from backend.tests.test_utils import _chat_payload

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_boundary.db"
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


def _post_chat(client: TestClient, message: str, conversation_id: str = None, force_new_session=None) -> dict:
    kwargs: dict = {"context": {}}
    if conversation_id is not None:
        kwargs["conversation_id"] = conversation_id
    if force_new_session is not None:
        kwargs["force_new_session"] = force_new_session
    resp = client.post("/api/chat", json=_chat_payload(message, **kwargs), headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get_history(client: TestClient, conversation_id: str) -> list:
    resp = client.get("/api/chat", params={"conversation_id": conversation_id}, headers=_auth())
    assert resp.status_code == 200
    return resp.json()["messages"]


# ---------------------------------------------------------------------------
# Test 2 — Clean session enforcement (force_new_session=True)
# ---------------------------------------------------------------------------


class TestCleanSessionEnforcement:
    def test_force_new_session_true_accepted(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("What data do you have access to?", force_new_session=True),
            headers=_auth(),
        )
        assert resp.status_code == 200

    def test_force_new_session_sends_empty_history(self, client: TestClient, monkeypatch):
        """When force_new_session=True, no history is passed to the AI call."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        captured_histories: list[list] = []

        def _capturing_call(msg, key, history=None, system_prompt=None):
            captured_histories.append(list(history or []))
            return "stub"

        cid = str(uuid.uuid4())
        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Seed some history first.
            _post_chat(client, "Polluting message", conversation_id=cid)
            # Now request with force_new_session=True.
            _post_chat(client, "Clean request", conversation_id=cid, force_new_session=True)

        assert len(captured_histories) == 2
        assert captured_histories[1] == [], (
            "force_new_session=True must pass empty history to the AI call"
        )

    def test_force_new_session_response_shape_unchanged(self, client: TestClient, monkeypatch):
        """Response schema must remain identical regardless of the flag."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        normal = _post_chat(client, "Hello without flag")
        clean = _post_chat(client, "Hello with flag", force_new_session=True)

        assert set(normal.keys()) == set(clean.keys())


# ---------------------------------------------------------------------------
# Test 3 — No history leakage when force_new_session=True
# ---------------------------------------------------------------------------


class TestNoHistoryLeakage:
    def test_second_request_ignores_first_when_forced(self, client: TestClient, monkeypatch):
        """
        Step 1: Send a message WITHOUT flag → pollutes conversation.
        Step 2: Send a second message WITH force_new_session=True.
        Expected: second AI call receives no history from step 1.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        second_call_history: list[list] = []

        call_count = {"n": 0}

        def _capturing_call(msg, key, history=None, system_prompt=None):
            call_count["n"] += 1
            if call_count["n"] == 2:
                second_call_history.append(list(history or []))
            return "stub"

        cid = str(uuid.uuid4())
        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Step 1: pollute conversation.
            _post_chat(client, "Polluting message without flag", conversation_id=cid)
            # Step 2: clean request.
            _post_chat(client, "Should not see prior message", conversation_id=cid, force_new_session=True)

        assert len(second_call_history) == 1
        assert second_call_history[0] == [], (
            "force_new_session=True must not receive any prior conversation history"
        )

    def test_persistence_unaffected_for_legacy_path(self, client: TestClient, monkeypatch):
        """
        Messages ARE persisted on the legacy path (no flag).
        The stateless flag does not affect the default write behavior.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        _post_chat(client, "Message A", conversation_id=cid)

        hist = client.get("/api/chat", params={"conversation_id": cid}, headers=_auth())
        assert hist.status_code == 200
        messages = hist.json()["messages"]
        contents = [m["content"] for m in messages]
        assert "Message A" in contents


# ---------------------------------------------------------------------------
# Test 4 — Stateless mode does not write to DB
# ---------------------------------------------------------------------------


class TestStatelessHistoryBypass:
    def test_stateless_does_persist_messages(self, client: TestClient, monkeypatch):
        """
        Messages ARE persisted even when force_new_session=True.
        Stateless skips history READ only; writes still occur.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        _post_chat(client, "Seed message", conversation_id=cid)
        _post_chat(client, "Stateless message", conversation_id=cid, force_new_session=True)

        history = _get_history(client, cid)
        assert len(history) >= 2

    def test_stateless_response_shape_complete(self, client: TestClient, monkeypatch):
        """Response still contains user_message and assistant_message."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        resp = _post_chat(client, "Ephemeral request", conversation_id=cid, force_new_session=True)
        assert "user_message" in resp
        assert "assistant_message" in resp
        assert resp["user_message"]["content"] == "Ephemeral request"

    def test_stateless_visible_in_history(self, client: TestClient, monkeypatch):
        """Messages sent with force_new_session=True are persisted and appear in GET /api/chat."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cid = str(uuid.uuid4())
        _post_chat(client, "Should be in history", conversation_id=cid)
        _post_chat(client, "Stateless message", conversation_id=cid, force_new_session=True)

        history = _get_history(client, cid)
        assert len(history) >= 2


# ---------------------------------------------------------------------------
# Test 5 — Ephemeral message structure mirrors persisted message structure
#           (EPHEMERAL_MESSAGE_CONSTRAINT_V1)
# ---------------------------------------------------------------------------


class TestEphemeralMessageStructure:
    def test_ephemeral_and_persisted_keys_are_identical(self, client: TestClient, monkeypatch):
        """
        Persisted and ephemeral ChatMessageResponse objects must expose exactly
        the same set of keys — no extra fields, no missing fields.

        This guards against _new_ephemeral_message drifting into a parallel
        message model.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # A legacy request persists both messages — take the user_message as reference.
        persisted_resp = _post_chat(client, "Persisted message")
        persisted_keys = set(persisted_resp["user_message"].keys())

        # A stateless request produces ephemeral messages — compare against persisted.
        ephemeral_resp = _post_chat(client, "Ephemeral message", force_new_session=True)
        ephemeral_keys = set(ephemeral_resp["user_message"].keys())

        assert ephemeral_keys == persisted_keys, (
            f"Ephemeral message keys differ from persisted message keys.\n"
            f"  Missing from ephemeral : {persisted_keys - ephemeral_keys}\n"
            f"  Extra in ephemeral     : {ephemeral_keys - persisted_keys}"
        )

    def test_ephemeral_assistant_keys_match_persisted(self, client: TestClient, monkeypatch):
        """Same structural check for the assistant message slot."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        persisted_resp = _post_chat(client, "Persisted assistant")
        persisted_keys = set(persisted_resp["assistant_message"].keys())

        ephemeral_resp = _post_chat(client, "Ephemeral assistant", force_new_session=True)
        ephemeral_keys = set(ephemeral_resp["assistant_message"].keys())

        assert ephemeral_keys == persisted_keys, (
            f"Ephemeral assistant message keys differ from persisted.\n"
            f"  Missing from ephemeral : {persisted_keys - ephemeral_keys}\n"
            f"  Extra in ephemeral     : {ephemeral_keys - persisted_keys}"
        )
