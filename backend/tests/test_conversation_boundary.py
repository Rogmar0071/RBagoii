"""
Tests for CONVERSATION_BOUNDARY_CONTROL_V1.

Validates:
  - Test 1: Legacy path unchanged (force_new_session=None)
  - Test 2: Clean session enforcement (force_new_session=True)
  - Test 3: No history leakage when force_new_session=True follows a prior message
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_boundary")

from backend.app.main import app  # noqa: E402

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


def _post_chat(client: TestClient, message: str, force_new_session=None) -> dict:
    body: dict = {"message": message, "context": {}}
    if force_new_session is not None:
        body["force_new_session"] = force_new_session
    resp = client.post("/api/chat", json=body, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Test 1 — Legacy path unchanged (force_new_session=None)
# ---------------------------------------------------------------------------


class TestLegacyPathUnchanged:
    """force_new_session=None must produce behavior identical to pre-change."""

    def test_omitted_flag_succeeds(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = _post_chat(client, "Hello")
        assert "reply" in resp
        assert "user_message" in resp
        assert "assistant_message" in resp

    def test_none_flag_explicit_succeeds(self, client: TestClient, monkeypatch):
        """Explicitly passing force_new_session=None is accepted."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "force_new_session": None},
            headers=_auth(),
        )
        assert resp.status_code == 200

    def test_false_flag_accepted(self, client: TestClient, monkeypatch):
        """force_new_session=False is accepted and behaves like None."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "force_new_session": False},
            headers=_auth(),
        )
        assert resp.status_code == 200

    def test_history_still_loaded_without_flag(self, client: TestClient, monkeypatch):
        """When flag is omitted, history is loaded normally (legacy behavior)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        captured_histories: list[list] = []

        def _capturing_call(msg, key, history=None, system_prompt=None):
            captured_histories.append(list(history or []))
            return "stub"

        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # First message (no flag) — seeds the history.
            _post_chat(client, "First message")
            # Second message (no flag) — history should contain the first exchange.
            _post_chat(client, "Second message")

        # The second call should have received non-empty history.
        assert len(captured_histories) == 2
        assert len(captured_histories[1]) > 0, (
            "Legacy path: second call must receive prior history"
        )


# ---------------------------------------------------------------------------
# Test 2 — Clean session enforcement (force_new_session=True)
# ---------------------------------------------------------------------------


class TestCleanSessionEnforcement:
    def test_force_new_session_true_accepted(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "What data do you have access to?", "force_new_session": True},
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

        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Seed some history first.
            _post_chat(client, "Polluting message")
            # Now request with force_new_session=True.
            _post_chat(client, "Clean request", force_new_session=True)

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

        with patch.object(cr, "_call_openai_chat", side_effect=_capturing_call):
            # Step 1: pollute conversation.
            _post_chat(client, "Polluting message without flag")
            # Step 2: clean request.
            _post_chat(client, "Should not see prior message", force_new_session=True)

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

        _post_chat(client, "Message A")

        hist = client.get("/api/chat", headers=_auth())
        assert hist.status_code == 200
        messages = hist.json()["messages"]
        contents = [m["content"] for m in messages]
        assert "Message A" in contents


# ---------------------------------------------------------------------------
# Test 4 — Stateless mode does not write to DB
# ---------------------------------------------------------------------------


class TestStatelessNoPersistence:
    def test_stateless_does_not_persist_messages(self, client: TestClient, monkeypatch):
        """
        DB message count must be unchanged after a force_new_session=True request.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.database as db_module
        from backend.app.models import GlobalChatMessage
        from sqlmodel import Session, select

        def _count_messages() -> int:
            with Session(db_module.get_engine()) as s:
                return len(s.exec(select(GlobalChatMessage)).all())

        before = _count_messages()
        _post_chat(client, "Stateless message", force_new_session=True)
        after = _count_messages()

        assert after == before, (
            f"Stateless execution must not write to DB; before={before} after={after}"
        )

    def test_stateless_response_shape_complete(self, client: TestClient, monkeypatch):
        """Response still contains user_message and assistant_message (ephemeral)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        resp = _post_chat(client, "Ephemeral request", force_new_session=True)
        assert "user_message" in resp
        assert "assistant_message" in resp
        assert resp["user_message"]["content"] == "Ephemeral request"

    def test_stateless_not_visible_in_history(self, client: TestClient, monkeypatch):
        """Messages sent with force_new_session=True must not appear in GET /api/chat."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        _post_chat(client, "Should be in history")
        _post_chat(client, "Should NOT be in history", force_new_session=True)

        hist = client.get("/api/chat", headers=_auth())
        assert hist.status_code == 200
        contents = [m["content"] for m in hist.json()["messages"]]
        assert "Should be in history" in contents
        assert "Should NOT be in history" not in contents
