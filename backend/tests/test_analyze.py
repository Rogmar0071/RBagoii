"""
Tests for POST /v1/analyze.

All heavy processing (ffmpeg, OpenAI) is mocked so tests run fast and without
external dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402

_TINY_MP4 = (
    b"\x00\x00\x00\x20ftyp"
    b"isom\x00\x00\x02\x00"
    b"isomiso2avc1mp41"
    b"\x00\x00\x00\x08free"
)

_FAKE_ANALYSIS = {
    "summary": "The video shows a drawer being opened and closed.",
    "conclusions": ["Hinge operates smoothly.", "No visible damage."],
    "key_events": [
        {"t_sec": 1.0, "event": "Drawer begins to open"},
        {"t_sec": 3.0, "event": "Drawer fully open"},
    ],
    "confidence": 0.88,
}


@pytest.fixture(autouse=True)
def _set_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_analyze(client: TestClient, requirements: str = "Describe what happens") -> ...:
    return client.post(
        "/v1/analyze",
        files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
        data={"requirements": requirements},
    )


def _fake_frames(tmp_path: Path, count: int = 3) -> list[Path]:
    """Create tiny placeholder JPEG-like files for mocking."""
    frames = []
    for i in range(count):
        p = tmp_path / f"frame_{i:04d}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # minimal JPEG header stub
        frames.append(p)
    return frames


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------


class TestAnalyzeMissingKey:
    def test_missing_openai_key_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # Also patch the module-level env read used at import time.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            response = _post_analyze(client)
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Happy path (no audio)
# ---------------------------------------------------------------------------


class TestAnalyzeHappyPath:
    def test_returns_200_with_expected_shape(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path, count=2)

        with (
            patch(
                "backend.app.analyze_routes._extract_frames", return_value=frames
            ),
            patch(
                "backend.app.analyze_routes._has_audio", return_value=False
            ),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            response = _post_analyze(client)

        assert response.status_code == 200
        body = response.json()
        assert "summary" in body
        assert "conclusions" in body
        assert "key_events" in body
        assert "confidence" in body
        assert "diagnostics" in body

    def test_diagnostics_contain_expected_fields(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path, count=3)

        with (
            patch(
                "backend.app.analyze_routes._extract_frames", return_value=frames
            ),
            patch(
                "backend.app.analyze_routes._has_audio", return_value=False
            ),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            response = _post_analyze(client)

        diag = response.json()["diagnostics"]
        assert diag["frames_used"] == 3
        assert diag["audio_present"] is False
        assert diag["transcript_used"] is False

    def test_conclusions_is_list(self, client: TestClient, tmp_path: Path) -> None:
        frames = _fake_frames(tmp_path)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=False),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            response = _post_analyze(client)

        assert isinstance(response.json()["conclusions"], list)

    def test_key_events_contain_t_sec_and_event(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=False),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            body = _post_analyze(client).json()

        for ev in body["key_events"]:
            assert "t_sec" in ev
            assert "event" in ev

    def test_confidence_is_numeric(self, client: TestClient, tmp_path: Path) -> None:
        frames = _fake_frames(tmp_path)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=False),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            body = _post_analyze(client).json()

        assert isinstance(body["confidence"], float | int)


# ---------------------------------------------------------------------------
# Audio transcript path
# ---------------------------------------------------------------------------


class TestAnalyzeWithAudio:
    def test_audio_present_sets_diagnostics(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=True),
            patch(
                "backend.app.analyze_routes._transcribe_audio",
                return_value="Sample transcript text.",
            ),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                return_value=dict(_FAKE_ANALYSIS),
            ),
        ):
            response = _post_analyze(client)

        diag = response.json()["diagnostics"]
        assert diag["audio_present"] is True
        assert diag["transcript_used"] is True

    def test_transcript_passed_to_openai(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path)
        call_args: dict = {}

        def _capture_call(
            frames_arg: list, requirements: str, transcript: str, key: str
        ) -> dict:
            call_args["transcript"] = transcript
            return dict(_FAKE_ANALYSIS)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=True),
            patch(
                "backend.app.analyze_routes._transcribe_audio",
                return_value="Hello world.",
            ),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                side_effect=_capture_call,
            ),
        ):
            _post_analyze(client)

        assert call_args["transcript"] == "Hello world."


# ---------------------------------------------------------------------------
# OpenAI failure
# ---------------------------------------------------------------------------


class TestAnalyzeOpenAIFailure:
    def test_openai_error_returns_502(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        frames = _fake_frames(tmp_path)

        with (
            patch("backend.app.analyze_routes._extract_frames", return_value=frames),
            patch("backend.app.analyze_routes._has_audio", return_value=False),
            patch(
                "backend.app.analyze_routes._call_openai_analysis",
                side_effect=RuntimeError("API unreachable"),
            ),
        ):
            response = _post_analyze(client)

        assert response.status_code == 502
        assert "Analysis failed" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Missing form field
# ---------------------------------------------------------------------------


class TestAnalyzeBadRequest:
    def test_missing_requirements_returns_422(self, client: TestClient) -> None:
        response = client.post(
            "/v1/analyze",
            files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
            # 'requirements' field intentionally omitted
        )
        assert response.status_code == 422

    def test_missing_video_returns_422(self, client: TestClient) -> None:
        response = client.post(
            "/v1/analyze",
            data={"requirements": "Describe what happens"},
        )
        assert response.status_code == 422
