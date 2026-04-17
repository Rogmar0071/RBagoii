"""
Ops routes tests
================
Tests for GET /v1/ops and GET /v1/folders/{folder_id}/ops endpoints.

Uses SQLite in-memory so no Postgres instance is required.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Use an isolated SQLite DB for each test."""
    db_path = tmp_path / "test.db"
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


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers: seed ops events and folders
# ---------------------------------------------------------------------------


def _seed_event(
    *,
    source: str = "backend",
    level: str = "info",
    event_type: str = "folders.create",
    message: str = "test event",
    folder_id: str | None = None,
) -> None:
    from backend.app.ops_log import log_event

    log_event(
        source=source,
        level=level,
        event_type=event_type,
        message=message,
        folder_id=folder_id,
    )


def _create_folder(client: TestClient, title: str | None = None) -> dict:
    body = {}
    if title:
        body["title"] = title
    resp = client.post("/v1/folders", json=body, headers=_auth())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# GET /v1/ops
# ---------------------------------------------------------------------------


class TestListOpsGlobal:
    def test_returns_empty_list_when_no_events(self, client: TestClient) -> None:
        resp = client.get("/v1/ops", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_returns_seeded_events(self, client: TestClient) -> None:
        _seed_event(event_type="folders.create", message="folder A created")
        _seed_event(event_type="jobs.enqueue", message="job enqueued")
        resp = client.get("/v1/ops", headers=_auth())
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2

    def test_event_schema(self, client: TestClient) -> None:
        _seed_event(
            source="backend",
            level="info",
            event_type="folders.create",
            message="schema check",
        )
        resp = client.get("/v1/ops", headers=_auth())
        ev = resp.json()["events"][0]
        for field in ("id", "created_at", "source", "level", "event_type", "message"):
            assert field in ev
        assert ev["source"] == "backend"
        assert ev["level"] == "info"
        assert ev["event_type"] == "folders.create"

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/v1/ops")
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, client: TestClient) -> None:
        resp = client.get("/v1/ops", headers=_auth("bad"))
        assert resp.status_code == 403

    def test_filter_by_source(self, client: TestClient) -> None:
        _seed_event(source="backend", event_type="folders.create")
        _seed_event(source="worker", event_type="jobs.start")
        resp = client.get("/v1/ops?source=worker", headers=_auth())
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["source"] == "worker"

    def test_filter_by_level(self, client: TestClient) -> None:
        _seed_event(level="error", event_type="jobs.failed", message="err")
        _seed_event(level="info", event_type="jobs.succeeded", message="ok")
        resp = client.get("/v1/ops?level=error", headers=_auth())
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["level"] == "error"

    def test_filter_by_event_type(self, client: TestClient) -> None:
        _seed_event(event_type="folders.create")
        _seed_event(event_type="jobs.enqueue")
        resp = client.get("/v1/ops?event_type=folders.create", headers=_auth())
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "folders.create"

    def test_limit_parameter(self, client: TestClient) -> None:
        for i in range(5):
            _seed_event(message=f"event {i}")
        resp = client.get("/v1/ops?limit=3", headers=_auth())
        assert len(resp.json()["events"]) == 3

    def test_limit_default_is_100(self, client: TestClient) -> None:
        for i in range(10):
            _seed_event(message=f"event {i}")
        resp = client.get("/v1/ops", headers=_auth())
        # Should return all 10 (well under default limit of 100)
        assert len(resp.json()["events"]) == 10

    def test_before_filter(self, client: TestClient) -> None:
        import time
        import urllib.parse

        _seed_event(message="old event")
        time.sleep(0.05)
        # Record a timestamp after the first event.
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc).isoformat()
        time.sleep(0.05)
        _seed_event(message="new event")

        encoded_cutoff = urllib.parse.quote(cutoff)
        resp = client.get(f"/v1/ops?before={encoded_cutoff}", headers=_auth())
        events = resp.json()["events"]
        # Only the old event should appear.
        assert len(events) == 1
        assert events[0]["message"] == "old event"

    def test_invalid_before_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/ops?before=not-a-date", headers=_auth())
        assert resp.status_code == 400

    def test_events_ordered_newest_first(self, client: TestClient) -> None:
        import time

        _seed_event(message="first")
        time.sleep(0.05)
        _seed_event(message="second")
        resp = client.get("/v1/ops", headers=_auth())
        events = resp.json()["events"]
        assert events[0]["message"] == "second"
        assert events[1]["message"] == "first"

    def test_folder_create_is_logged_via_api(self, client: TestClient) -> None:
        """Creating a folder via the API should emit a folders.create ops event."""
        _create_folder(client, "Logged Folder")
        resp = client.get("/v1/ops?event_type=folders.create", headers=_auth())
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) >= 1
        assert events[0]["event_type"] == "folders.create"


# ---------------------------------------------------------------------------
# GET /v1/folders/{folder_id}/ops
# ---------------------------------------------------------------------------


class TestListOpsForFolder:
    def test_returns_empty_list_when_no_events(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        # Clear ops by using a fresh filter (folder was just created, so
        # there may be a folders.create event; filter to a different event_type)
        resp = client.get(f"/v1/folders/{fid}/ops?event_type=jobs.start", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_returns_only_events_for_folder(self, client: TestClient) -> None:
        folder_a = _create_folder(client, "A")
        folder_b = _create_folder(client, "B")
        fid_a = folder_a["id"]
        fid_b = folder_b["id"]
        _seed_event(event_type="jobs.start", message="job for A", folder_id=fid_a)
        _seed_event(event_type="jobs.start", message="job for B", folder_id=fid_b)

        resp = client.get(f"/v1/folders/{fid_a}/ops", headers=_auth())
        events = resp.json()["events"]
        folder_events = [e for e in events if e["event_type"] == "jobs.start"]
        assert len(folder_events) == 1
        assert folder_events[0]["message"] == "job for A"
        assert folder_events[0]["folder_id"] == fid_a

    def test_returns_404_for_unknown_folder(self, client: TestClient) -> None:
        resp = client.get(f"/v1/folders/{uuid.uuid4()}/ops", headers=_auth())
        assert resp.status_code == 404

    def test_returns_400_for_invalid_uuid(self, client: TestClient) -> None:
        resp = client.get("/v1/folders/not-a-uuid/ops", headers=_auth())
        assert resp.status_code == 400

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get(f"/v1/folders/{uuid.uuid4()}/ops")
        assert resp.status_code == 401

    def test_filter_by_level(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        _seed_event(level="error", event_type="jobs.failed", folder_id=fid)
        _seed_event(level="info", event_type="jobs.succeeded", folder_id=fid)
        resp = client.get(f"/v1/folders/{fid}/ops?level=error", headers=_auth())
        events = resp.json()["events"]
        error_events = [e for e in events if e["level"] == "error"]
        assert len(error_events) == 1

    def test_limit_parameter(self, client: TestClient) -> None:
        folder = _create_folder(client)
        fid = folder["id"]
        for i in range(5):
            _seed_event(message=f"ev {i}", folder_id=fid)
        resp = client.get(f"/v1/folders/{fid}/ops?limit=2", headers=_auth())
        folder_events = [e for e in resp.json()["events"] if e.get("folder_id") == fid]
        assert len(folder_events) <= 2


# ---------------------------------------------------------------------------
# ops_log helper direct tests
# ---------------------------------------------------------------------------


class TestOpsLogHelper:
    def test_log_event_does_not_raise_without_db(self, monkeypatch) -> None:
        """log_event must be a no-op (no exception) when DB is unavailable."""
        monkeypatch.setenv("DATABASE_URL", "")
        import backend.app.database as db_module

        db_module.reset_engine()
        from backend.app.ops_log import log_event

        # Should not raise.
        log_event(
            source="backend",
            level="info",
            event_type="folders.create",
            message="no-db test",
        )

    def test_error_detail_truncated_to_2000_chars(self, client: TestClient) -> None:
        long_detail = "x" * 3000
        from backend.app.ops_log import log_event

        log_event(
            source="backend",
            level="error",
            event_type="jobs.failed",
            message="truncation test",
            error_detail=long_detail,
        )
        resp = client.get("/v1/ops?event_type=jobs.failed", headers=_auth())
        events = resp.json()["events"]
        assert events[0]["error_detail"] is not None
        assert len(events[0]["error_detail"]) <= 2000
