"""
Tests for REPO_CONTEXT_FLOW_RECOVERY_V1:

- Phase 4: POST /api/chat/{id}/github/repos returns 422 when no files retrieved
- Phase 5: ingestion_status field set to "success" / "failed"
- Phase 8: context fallback — backend loads repos when context.files absent
- Phase 9: [REPO_PRESENT_BUT_EMPTY] injected when repo has no chunks
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_repo_recovery")

from backend.app.main import app  # noqa: E402
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_repo_recovery.db"
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


# ---------------------------------------------------------------------------
# Phase 4 — Ingestion Truth Lock
# ---------------------------------------------------------------------------


class TestIngestionTruthLock:
    def test_422_when_file_list_empty(self, client: TestClient):
        """POST /api/chat/{id}/github/repos → 422 when repo has no fetchable files."""
        cid = str(uuid.uuid4())

        with patch(
            "backend.app.github_routes._fetch_repo_file_list",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/empty-repo", "branch": "main"},
                headers=AUTH,
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["status"] == "failed"
        assert body["detail"]["reason"] == "repo_ingestion_failed"

    def test_422_when_fetch_raises(self, client: TestClient):
        """POST /api/chat/{id}/github/repos → 422 when GitHub fetch raises exception."""
        cid = str(uuid.uuid4())

        with patch(
            "backend.app.github_routes._fetch_repo_file_list",
            new_callable=AsyncMock,
            side_effect=RuntimeError("GitHub API 403"),
        ):
            resp = client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/forbidden-repo", "branch": "main"},
                headers=AUTH,
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["reason"] == "repo_ingestion_failed"
        assert "GitHub API 403" in body["detail"]["detail"]

    def test_201_when_files_present(self, client: TestClient):
        """POST /api/chat/{id}/github/repos → 201 when files are successfully retrieved."""
        cid = str(uuid.uuid4())
        fake_files = [("README.md", "# Hello\nThis is a test repo.")]

        with patch(
            "backend.app.github_routes._fetch_repo_file_list",
            new_callable=AsyncMock,
            return_value=fake_files,
        ):
            resp = client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/good-repo", "branch": "main"},
                headers=AUTH,
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["ingestion_status"] == "success"


# ---------------------------------------------------------------------------
# Phase 5 — ingestion_status field
# ---------------------------------------------------------------------------


class TestIngestionStatusField:
    def test_ingestion_status_success_in_list(self, client: TestClient):
        """GET /api/chat/{id}/github/repos returns ingestion_status = 'success'."""
        cid = str(uuid.uuid4())
        fake_files = [("main.py", "print('hello')")]

        with patch(
            "backend.app.github_routes._fetch_repo_file_list",
            new_callable=AsyncMock,
            return_value=fake_files,
        ):
            client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/myrepo", "branch": "main"},
                headers=AUTH,
            )

        list_resp = client.get(f"/api/chat/{cid}/github/repos", headers=AUTH)
        assert list_resp.status_code == 200
        repos = list_resp.json()
        assert len(repos) == 1
        assert repos[0]["ingestion_status"] == "success"


# ---------------------------------------------------------------------------
# Phase 8 — Backend Context Fallback
# ---------------------------------------------------------------------------


class TestContextFallback:
    def test_fallback_loads_repo_when_context_files_absent(
        self, client: TestClient, monkeypatch
    ):
        """Backend injects REPO CONTEXT even when context.files is not in the request."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        cid = str(uuid.uuid4())
        fake_files = [("app.py", "def hello():\n    return 'world'")]

        with patch(
            "backend.app.github_routes._fetch_repo_file_list",
            new_callable=AsyncMock,
            return_value=fake_files,
        ):
            add_resp = client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/fallback-repo", "branch": "main"},
                headers=AUTH,
            )
        assert add_resp.status_code == 201

        import backend.app.chat_routes as cr

        captured_prompt: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured_prompt.append(system_prompt or "")
            return "Fallback repo answer."

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            chat_resp = client.post(
                "/api/chat",
                # Send request WITHOUT context.files
                json={
                    "message": "hello",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {"session_id": None, "domain_profile_id": None},
                },
                headers=AUTH,
            )

        assert chat_resp.status_code == 200
        assert len(captured_prompt) == 1
        # Backend fallback must have injected repo context or file reference
        assert "fallback-repo" in captured_prompt[0] or "REPO CONTEXT" in captured_prompt[0]

    def test_no_fallback_when_no_repos_in_conversation(
        self, client: TestClient, monkeypatch
    ):
        """Backend does not inject repo block when conversation has no repos."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        cid = str(uuid.uuid4())

        import backend.app.chat_routes as cr

        captured_prompt: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured_prompt.append(system_prompt or "")
            return "No repo answer."

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            chat_resp = client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {"session_id": None, "domain_profile_id": None},
                },
                headers=AUTH,
            )

        assert chat_resp.status_code == 200
        assert len(captured_prompt) == 1
        # No repo context should appear
        assert "REPO CONTEXT" not in captured_prompt[0]
        assert "Uploaded Files" not in captured_prompt[0]


# ---------------------------------------------------------------------------
# Phase 9 — REPO_PRESENT_BUT_EMPTY marker
# ---------------------------------------------------------------------------


class TestRepoPresentButEmpty:
    def test_marker_injected_when_repo_has_no_chunks(
        self, client: TestClient, monkeypatch
    ):
        """[REPO_PRESENT_BUT_EMPTY] appears in prompt when ingestion_status is None/failed."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import ChatFile

        # Insert a github_repo ChatFile with no chunks and no ingestion_status
        db = Session(db_module.get_engine())
        cid = str(uuid.uuid4())
        file_id = uuid.uuid4()
        ghost_file = ChatFile(
            id=file_id,
            conversation_id=cid,
            filename="owner/ghost-repo",
            mime_type="application/x-git-repository",
            size_bytes=0,
            object_key=f"github:https://github.com/owner/ghost-repo@main",
            category="github_repo",
            included_in_context=True,
            extracted_text="Repo: owner/ghost-repo\nFiles: 0\nTop Files:\n",
            ingestion_status=None,  # no ingestion status = failed
        )
        db.add(ghost_file)
        db.commit()
        db.close()

        import backend.app.chat_routes as cr

        captured_prompt: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured_prompt.append(system_prompt or "")
            return "ghost repo answer."

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            chat_resp = client.post(
                "/api/chat",
                json={
                    "message": "show me the code",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "files": [
                            {
                                "id": str(file_id),
                                "filename": "owner/ghost-repo",
                                "category": "github_repo",
                                "mime_type": "application/x-git-repository",
                            }
                        ],
                    },
                },
                headers=AUTH,
            )

        assert chat_resp.status_code == 200
        assert len(captured_prompt) == 1
        prompt = captured_prompt[0]
        assert "[REPO_PRESENT_BUT_EMPTY]" in prompt or "[NO_REPO_CONTENT_AVAILABLE]" in prompt
