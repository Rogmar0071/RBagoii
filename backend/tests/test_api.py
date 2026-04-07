"""
Backend API tests
=================
Uses FastAPI's TestClient (via httpx) to validate core API behaviour.
Heavy processing (extraction) is disabled via BACKEND_DISABLE_JOBS=1.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

# Disable background extraction so tests are fast.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402  (import after env setup)

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    # Reload the module-level API_KEY so the app picks it up.
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_MP4 = (
    # Minimal valid-ish MP4 stub (not a real playable video, but enough for
    # the upload endpoint which only saves the bytes without playing it).
    b"\x00\x00\x00\x20ftyp"
    b"isom\x00\x00\x02\x00"
    b"isomiso2avc1mp41"
    b"\x00\x00\x00\x08free"
)


def _upload(client: TestClient, token: str = TOKEN, meta: str = "") -> dict:
    """Helper — POST to /v1/sessions and return parsed JSON response."""
    response = client.post(
        "/v1/sessions",
        files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
        data={"meta": meta},
        headers={"Authorization": f"Bearer {token}"},
    )
    return response


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_upload_requires_auth(self, client: TestClient) -> None:
        """POST /v1/sessions without auth must return 401."""
        response = client.post(
            "/v1/sessions",
            files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
        )
        assert response.status_code == 401

    def test_upload_wrong_token_returns_403(self, client: TestClient) -> None:
        response = _upload(client, token="wrong-token")
        assert response.status_code == 403

    def test_status_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent")
        assert response.status_code == 401

    def test_blueprint_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent/blueprint")
        assert response.status_code == 401

    def test_preview_index_requires_auth(self, client: TestClient) -> None:
        response = client.get("/v1/sessions/nonexistent/preview/index")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestUpload:
    def test_upload_creates_session(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta=json.dumps({"device": "Pixel 8"}))
        assert response.status_code == 201
        body = response.json()
        assert "session_id" in body
        assert body["status"] == "queued"

    def test_upload_saves_files(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta=json.dumps({"fps": 30}))
        session_id = response.json()["session_id"]
        sdir = tmp_path / "sessions" / session_id
        assert (sdir / "clip.mp4").exists()
        assert (sdir / "meta.json").exists()
        assert (sdir / "status.json").exists()

    def test_upload_status_is_queued(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client)
        session_id = response.json()["session_id"]
        status_path = tmp_path / "sessions" / session_id / "status.json"
        with status_path.open() as fh:
            status = json.load(fh)
        assert status["status"] == "queued"

    def test_upload_invalid_meta_json_returns_422(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta="not-valid-json{{{")
        assert response.status_code == 422

    def test_upload_empty_meta_is_ok(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = _upload(client, meta="")
        assert response.status_code == 201


# ---------------------------------------------------------------------------
# Status endpoint tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_queued(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"

    def test_status_nonexistent_returns_404(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        response = client.get(
            "/v1/sessions/does-not-exist",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Blueprint / preview endpoints (pre-completion states)
# ---------------------------------------------------------------------------


class TestBlueprintAndPreview:
    def test_blueprint_not_ready_returns_404(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/blueprint",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 404

    def test_preview_index_empty_when_no_previews(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/preview/index",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["files"] == []

    def test_preview_file_traversal_rejected(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        session_id = _upload(client).json()["session_id"]
        response = client.get(
            f"/v1/sessions/{session_id}/preview/../../../etc/passwd",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        # FastAPI routes will either not match or return 400/404.
        assert response.status_code in (400, 404, 422)
