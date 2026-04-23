"""
Tests for REPO_CONTEXT_FINALIZATION_V1 and GLOBAL_REPO_ASSET_SYSTEM_LOCK_V1.

Phases covered:
- Phase 1: Repo first-class entity (model creation, independent of ChatFile)
- Phase 2: POST /api/chat/{cid}/repos is DEPRECATED (returns 410)
- Phase 3: Retrieval scoped to repo_ids
- Phase 4: Token budget enforcement
- Phase 5: Enhanced scoring (filename match, recency weight)
- Phase 6: REPO STATUS block injected into AI prompt
- Phase 8: Retry endpoint POST /api/repos/{id}/retry
- Phase 9: context.repos is the authoritative repo retrieval path
- System Lock: POST /api/repos/add is the only valid ingestion entry point
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_finalization")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_finalization.db"
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


def mock_github_fetch(files=None):
    """
    Create a context manager that mocks GitHub fetch functions for testing.
    Usage: with mock_github_fetch([("file.py", "content")]):
    """
    if files is None:
        files = [("README.md", "# Hello")]

    def mock_fetch_tree(owner, repo, branch, token):
        return [{"path": path} for path, _ in files]

    def mock_fetch_file(owner, repo, branch, path, client):
        for file_path, content in files:
            if file_path == path:
                return content.encode("utf-8")
        return None

    async def mock_fetch_repo_file_list(owner, repo, branch, headers):
        return list(files)

    from contextlib import ExitStack
    from unittest.mock import patch as _patch

    class _MockContext:
        def __enter__(self):
            self._stack = ExitStack()
            self._stack.enter_context(_patch.multiple(
                "backend.app.ingest_pipeline",
                _fetch_github_tree=mock_fetch_tree,
                _fetch_raw_file=mock_fetch_file,
            ))
            self._stack.enter_context(_patch(
                "backend.app.github_routes._fetch_repo_file_list",
                new=mock_fetch_repo_file_list,
            ))
            return self

        def __exit__(self, *args):
            return self._stack.__exit__(*args)

    return _MockContext()


@pytest.fixture(name="mock_github_fetch")
def _mock_github_fetch_fixture():
    """Fixture returning the mock_github_fetch context-manager factory."""
    return mock_github_fetch


# ---------------------------------------------------------------------------
# Phase 1 — Repo first-class entity
# ---------------------------------------------------------------------------


class TestRepoFirstClassEntity:
    def test_repo_model_independent_of_chat_file(self):
        """Repo rows can be created without any ChatFile dependency."""
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        with Session(db_module.get_engine()) as session:
            repo = Repo(
                id=uuid.uuid4(),
                conversation_id=str(uuid.uuid4()),
                repo_url="https://github.com/owner/myrepo",
                owner="owner",
                name="myrepo",
                branch="main",
                ingestion_status="pending",
                total_files=0,
                total_chunks=0,
            )
            session.add(repo)
            session.commit()
            session.refresh(repo)

            assert repo.id is not None
            assert repo.ingestion_status == "pending"
            assert repo.total_files == 0

    def test_repo_has_all_required_fields(self):
        """Repo model exposes owner, name, branch, status, files, chunks."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())
        with Session(db_module.get_engine()) as session:
            repo = Repo(
                conversation_id=cid,
                repo_url="https://github.com/acme/proj",
                owner="acme",
                name="proj",
                branch="develop",
                ingestion_status="success",
                total_files=10,
                total_chunks=42,
            )
            session.add(repo)
            session.commit()

            found = session.exec(select(Repo).where(Repo.conversation_id == cid)).first()
            assert found is not None
            assert found.owner == "acme"
            assert found.name == "proj"
            assert found.branch == "develop"
            assert found.total_files == 10
            assert found.total_chunks == 42


# ---------------------------------------------------------------------------
# Phase 2 — Async ingestion endpoint
# ---------------------------------------------------------------------------


class TestDeprecatedIngestionEndpoint:
    """POST /api/chat/{cid}/repos is permanently retired — must return 410."""

    def test_post_repos_returns_410(self, client: TestClient):
        """POST /api/chat/{cid}/repos returns 410 Gone."""
        cid = str(uuid.uuid4())
        resp = client.post(
            f"/api/chat/{cid}/repos",
            json={"repo_url": "https://github.com/owner/myrepo", "branch": "main"},
            headers=AUTH,
        )
        assert resp.status_code == 410
        assert "DEPRECATED" in resp.json().get("detail", "")

    def test_post_repos_410_regardless_of_payload(self, client: TestClient):
        """POST /api/chat/{cid}/repos always returns 410 regardless of body."""
        cid = str(uuid.uuid4())
        resp = client.post(
            f"/api/chat/{cid}/repos",
            json={"repo_url": "https://github.com/any/repo", "branch": "dev"},
            headers=AUTH,
        )
        assert resp.status_code == 410

    def test_post_repos_410_lists_correct_replacement(self, client: TestClient):
        """The 410 response detail references /api/repos/add."""
        cid = str(uuid.uuid4())
        resp = client.post(
            f"/api/chat/{cid}/repos",
            json={"repo_url": "https://github.com/owner/r", "branch": "main"},
            headers=AUTH,
        )
        assert resp.status_code == 410
        assert "/api/repos/add" in resp.json().get("detail", "")

    def test_repo_creation_via_add_endpoint_still_works(
        self, client: TestClient, mock_github_fetch
    ):
        """Repos can still be created — just via POST /api/repos/add."""
        with mock_github_fetch([("README.md", "# Hello world")]):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": str(uuid.uuid4()),
                    "repo_url": "https://github.com/owner/myrepo",
                    "branch": "main",
                },
                headers=AUTH,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "repo_id" in body
        assert body["status"] in ("pending", "running", "success", "failed")

    def test_ingestion_worker_creates_repo_chunks(
        self, client: TestClient, capsys, mock_github_fetch
    ):
        """run_repo_ingestion creates RepoChunk rows and prints INGEST START/DONE (V3 §7)."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Repo, RepoChunk

        cid = str(uuid.uuid4())
        fake_files = [
            ("src/app.py", "def main():\n    pass\n"),
            ("README.md", "# My Project\n"),
        ]

        with mock_github_fetch(fake_files):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/chunkrepo",
                    "branch": "main",
                },
                headers=AUTH,
            )
        assert resp.status_code == 200
        repo_id = uuid.UUID(resp.json()["repo_id"])

        # DISABLE_JOBS=1 runs the worker synchronously in _enqueue_repo_ingestion
        with Session(db_module.get_engine()) as session:
            repo = session.get(Repo, repo_id)
            assert repo is not None
            assert repo.ingestion_status == "success"
            assert repo.total_files == 2
            assert repo.total_chunks > 0

            chunks = session.exec(select(RepoChunk).where(RepoChunk.repo_id == repo_id)).all()
            assert len(chunks) > 0
            for chunk in chunks:
                assert chunk.repo_id == repo_id

        stdout = capsys.readouterr().out
        assert "INGEST START:" in stdout
        assert "INGEST DONE:" in stdout

    def test_post_repos_creates_repo_row_via_add(self, client: TestClient):
        """POST /api/repos/add creates a Repo row in the DB."""
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())

        with mock_github_fetch([("main.py", "print('hi')")]):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/proj",
                    "branch": "main",
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        repo_id = uuid.UUID(resp.json()["repo_id"])

        with Session(db_module.get_engine()) as session:
            repo = session.get(Repo, repo_id)
            assert repo is not None
            assert repo.owner == "owner"
            assert repo.name == "proj"

    def test_list_repos_returns_repo_objects(self, client: TestClient):
        """GET /api/chat/{cid}/repos lists Repo entities bound via ConversationRepo."""
        cid = str(uuid.uuid4())

        with mock_github_fetch([("app.py", "code")]):
            client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/testrepo",
                    "branch": "main",
                },
                headers=AUTH,
            )

        list_resp = client.get(f"/api/chat/{cid}/repos", headers=AUTH)
        assert list_resp.status_code == 200
        repos = list_resp.json()
        assert len(repos) == 1
        r = repos[0]
        assert r["repo_id"] == r["id"]
        assert r["owner"] == "owner"
        assert r["name"] == "testrepo"
        assert "status" in r
        assert "total_files" in r
        assert "chunk_count" in r

    def test_duplicate_add_returns_same_repo(self, client: TestClient):
        """POST /api/repos/add: first call succeeds (200), second returns 409 when
        the repo is already running or ingested (CONTRACT V2 Section 7)."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())
        payload = {
            "conversation_id": cid,
            "repo_url": "https://github.com/owner/idempotentrepo",
            "branch": "main",
        }

        with mock_github_fetch([("README.md", "# Hello")]):
            resp1 = client.post("/api/repos/add", json=payload, headers=AUTH)
            resp2 = client.post("/api/repos/add", json=payload, headers=AUTH)

        assert resp1.status_code == 200
        # Second call: repo already running or ingested — must return 409.
        assert resp2.status_code == 409

        # Only one Repo row must exist in the DB.
        with Session(db_module.get_engine()) as session:
            repos = session.exec(
                select(Repo).where(
                    Repo.repo_url == payload["repo_url"],
                    Repo.branch == payload["branch"],
                )
            ).all()
            assert len(repos) == 1


# ---------------------------------------------------------------------------
# Phase 3 — Retrieval scoped to repo_ids
# ---------------------------------------------------------------------------


class TestRetrievalScopedToRepoIds:
    def test_retrieve_by_repo_ids_not_chat_file_ids(self):
        """retrieve_relevant_chunks queries by repo_id when repo_ids supplied."""
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo, RepoChunk
        from backend.app.repo_retrieval import retrieve_relevant_chunks

        with Session(db_module.get_engine()) as session:
            repo_a = Repo(
                conversation_id="conv1",
                repo_url="https://github.com/a/r",
                owner="a",
                name="r",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
            )
            repo_b = Repo(
                conversation_id="conv1",
                repo_url="https://github.com/b/r",
                owner="b",
                name="r",
                branch="main",
                ingestion_status="success",
                total_files=1,
                total_chunks=1,
            )
            session.add(repo_a)
            session.add(repo_b)
            session.flush()

            chunk_a = RepoChunk(
                repo_id=repo_a.id,
                file_id=uuid.uuid4(),
                file_path="app.py",
                content="def hello(): return 'hello from A'",
                chunk_index=0,
                token_estimate=10,
                graph_group=str(uuid.uuid4()),
            )
            chunk_b = RepoChunk(
                repo_id=repo_b.id,
                file_id=uuid.uuid4(),
                file_path="app.py",
                content="def goodbye(): return 'goodbye from B'",
                chunk_index=0,
                token_estimate=10,
                graph_group=str(uuid.uuid4()),
            )
            session.add(chunk_a)
            session.add(chunk_b)
            session.commit()

            # Scope to repo_a only
            results = retrieve_relevant_chunks(
                user_query="hello function",
                db=session,
                repo_ids=[repo_a.id],
            )
            assert len(results["chunks"]) >= 1
            assert results["total_chunks"] == len(results["chunks"])
            assert len(results["file_ids"]) == len(results["chunks"])
            paths_owners = [c.content for c in results["chunks"]]
            assert any("hello from A" in c for c in paths_owners)
            assert all("goodbye from B" not in c for c in paths_owners)


# ---------------------------------------------------------------------------
# Phase 4 — Token budget enforcement
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_token_budget_limits_total_tokens(self):
        """_apply_token_budget drops chunks that exceed the token limit."""
        from backend.app.models import RepoChunk
        from backend.app.repo_retrieval import _apply_token_budget

        chunks = [
            RepoChunk(
                repo_id=uuid.uuid4(),
                file_path=f"file{i}.py",
                content="x" * 400,
                chunk_index=0,
                token_estimate=100,
            )
            for i in range(10)
        ]
        # Budget of 350 tokens → only 3 chunks fit (3 * 100 = 300 ≤ 350 < 400)
        result = _apply_token_budget(chunks, max_tokens=350)
        assert len(result) == 3
        total = sum(c.token_estimate for c in result)
        assert total <= 350

    def test_token_budget_allows_all_when_within_limit(self):
        """_apply_token_budget keeps all chunks when total is within budget."""
        from backend.app.models import RepoChunk
        from backend.app.repo_retrieval import _apply_token_budget

        chunks = [
            RepoChunk(
                repo_id=uuid.uuid4(),
                file_path=f"f{i}.py",
                content="x",
                chunk_index=0,
                token_estimate=50,
            )
            for i in range(5)
        ]
        result = _apply_token_budget(chunks, max_tokens=1000)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Phase 5 — Enhanced scoring
# ---------------------------------------------------------------------------


class TestEnhancedScoring:
    def test_filename_match_boosts_score(self):
        """A chunk whose filename matches the query keyword scores higher."""
        from backend.app.models import RepoChunk
        from backend.app.repo_retrieval import _score_chunk

        chunk_match = RepoChunk(
            repo_id=uuid.uuid4(),
            file_path="src/auth.py",  # matches "auth" keyword
            content="generic content here",
            chunk_index=0,
            token_estimate=10,
        )
        chunk_nomatch = RepoChunk(
            repo_id=uuid.uuid4(),
            file_path="src/utils.py",
            content="generic content here",
            chunk_index=0,
            token_estimate=10,
        )
        score_match = _score_chunk(chunk_match, ["auth"], "auth module")
        score_nomatch = _score_chunk(chunk_nomatch, ["auth"], "auth module")
        assert score_match > score_nomatch

    def test_recency_penalty_applied_to_deeper_chunks(self):
        """chunk_index > 0 receives a recency penalty reducing score."""
        from backend.app.models import RepoChunk
        from backend.app.repo_retrieval import _score_chunk

        chunk_first = RepoChunk(
            repo_id=uuid.uuid4(),
            file_path="app.py",
            content="def login(): pass",
            chunk_index=0,
            token_estimate=10,
        )
        chunk_deep = RepoChunk(
            repo_id=uuid.uuid4(),
            file_path="app.py",
            content="def login(): pass",
            chunk_index=6,  # triggers -2 penalty (6 // 3 = 2, clamped to 2)
            token_estimate=10,
        )
        score_first = _score_chunk(chunk_first, ["login"], "login")
        score_deep = _score_chunk(chunk_deep, ["login"], "login")
        assert score_first > score_deep


# ---------------------------------------------------------------------------
# Phase 6 — REPO STATUS block
# ---------------------------------------------------------------------------


class TestRepoStatusBlock:
    def test_repo_status_injected_into_prompt(
        self, client: TestClient, monkeypatch, capsys
    ):
        """REPO STATUS block appears in AI system prompt when context.repos is sent."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo, RepoChunk

        cid = str(uuid.uuid4())
        repo_id = uuid.uuid4()

        with Session(db_module.get_engine()) as session:
            repo = Repo(
                id=repo_id,
                conversation_id=cid,
                repo_url="https://github.com/test/status-repo",
                owner="test",
                name="status-repo",
                branch="main",
                ingestion_status="success",
                total_files=5,
                total_chunks=20,
            )
            session.add(repo)
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_id=uuid.uuid4(),
                    file_path="app.py",
                    content="def status_repo():\n    return True",
                    chunk_index=0,
                    token_estimate=8,
                    graph_group=str(uuid.uuid4()),
                )
            )
            session.commit()

        import backend.app.chat_routes as cr

        captured: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured.append(system_prompt or "")
            return "ok"

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            resp = client.post(
                "/api/chat",
                json={
                    "message": "what does this repo do?",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "repos": [str(repo_id)],
                    },
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        assert len(captured) == 1
        prompt = captured[0]
        assert "REPO STATUS" in prompt
        assert "status-repo" in prompt
        assert "success" in prompt
        stdout = capsys.readouterr().out
        assert "CTX_REPOS:" in stdout
        assert "CTX_FILES:" in stdout
        assert "REPO_CHUNKS:" in stdout

    def test_failed_repo_without_chunks_returns_runtime_failure(
        self, client: TestClient, monkeypatch
    ):
        """A failed repo now returns REPO_CONTEXT_EMPTY without calling OpenAI."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())
        repo_id = uuid.uuid4()

        with Session(db_module.get_engine()) as session:
            repo = Repo(
                id=repo_id,
                conversation_id=cid,
                repo_url="https://github.com/test/broken-repo",
                owner="test",
                name="broken-repo",
                branch="main",
                ingestion_status="failed",
                total_files=0,
                total_chunks=0,
            )
            session.add(repo)
            session.commit()

        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value="ok") as fake_openai:
            resp = client.post(
                "/api/chat",
                json={
                    "message": "show me the code",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "repos": [str(repo_id)],
                    },
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        assert resp.json().get("reply") == "REPO_CONTEXT_EMPTY"
        assert resp.json().get("error_code") == "REPO_CONTEXT_EMPTY"
        assert fake_openai.call_count == 0

    def test_processing_repo_without_chunks_returns_runtime_failure(
        self, client: TestClient, monkeypatch
    ):
        """A pending/running repo now returns REPO_CONTEXT_EMPTY without calling OpenAI."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())
        repo_id = uuid.uuid4()

        with Session(db_module.get_engine()) as session:
            repo = Repo(
                id=repo_id,
                conversation_id=cid,
                repo_url="https://github.com/test/running-repo",
                owner="test",
                name="running-repo",
                branch="main",
                ingestion_status="running",
                total_files=0,
                total_chunks=0,
            )
            session.add(repo)
            session.commit()

        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value="ok") as fake_openai:
            resp = client.post(
                "/api/chat",
                json={
                    "message": "show repo status",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "repos": [str(repo_id)],
                    },
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        assert resp.json().get("reply") == "REPO_CONTEXT_EMPTY"
        assert resp.json().get("error_code") == "REPO_CONTEXT_EMPTY"
        assert fake_openai.call_count == 0


# ---------------------------------------------------------------------------
# Section 8 — Timeout safety: running → failed after threshold
# ---------------------------------------------------------------------------


class TestTimeoutSafety:
    def test_stuck_running_repo_is_flipped_to_failed(
        self, client: TestClient, monkeypatch
    ):
        """A repo stuck in 'running' beyond threshold is auto-failed at chat time (V3 §8)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())
        repo_id = uuid.uuid4()

        old_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)

        with Session(db_module.get_engine()) as session:
            repo = Repo(
                id=repo_id,
                conversation_id=cid,
                repo_url="https://github.com/test/stuck-repo",
                owner="test",
                name="stuck-repo",
                branch="main",
                ingestion_status="running",
                total_files=0,
                total_chunks=0,
                updated_at=old_ts,
            )
            session.add(repo)
            session.commit()

        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value="ok") as fake_openai:
            resp = client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "repos": [str(repo_id)],
                    },
                },
                headers=AUTH,
            )

        # Timeout flips repo to failed and returns REPO_CONTEXT_EMPTY.
        assert resp.status_code == 200
        assert resp.json().get("reply") == "REPO_CONTEXT_EMPTY"
        assert resp.json().get("error_code") == "REPO_CONTEXT_EMPTY"
        assert fake_openai.call_count == 0

        # Verify DB was updated to failed
        with Session(db_module.get_engine()) as session:
            r = session.get(Repo, repo_id)
            assert r is not None
            assert r.ingestion_status == "failed"


# ---------------------------------------------------------------------------
# Phase 8 — Retry endpoint
# ---------------------------------------------------------------------------


class TestRetryEndpoint:
    def test_retry_resets_repo_to_pending(
        self, client: TestClient, mock_github_fetch
    ):
        """POST /api/repos/{id}/retry resets status to pending and re-ingests."""
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.models import Repo

        cid = str(uuid.uuid4())

        # First ingest (fails) — mock fetch to raise error
        def mock_fetch_tree_fail(owner, repo, branch, token):
            raise RuntimeError("timeout")

        with patch.multiple(
            "backend.app.ingest_pipeline",
            _fetch_github_tree=mock_fetch_tree_fail,
        ):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/flaky-repo",
                    "branch": "main",
                },
                headers=AUTH,
            )
        assert resp.status_code == 200
        repo_id = resp.json()["repo_id"]

        # Confirm it is now failed
        with Session(db_module.get_engine()) as session:
            repo = session.get(Repo, uuid.UUID(repo_id))
            assert repo is not None
            assert repo.ingestion_status == "failed"

        # Retry with good data
        with mock_github_fetch([("README.md", "# Fixed!")]):
            retry_resp = client.post(
                f"/api/repos/{repo_id}/retry",
                headers=AUTH,
            )
        assert retry_resp.status_code == 202
        # After retry (synchronous in test mode), status should be success
        with Session(db_module.get_engine()) as session:
            repo = session.get(Repo, uuid.UUID(repo_id))
            assert repo is not None
            assert repo.ingestion_status == "success"
            assert repo.total_chunks > 0

    def test_retry_404_for_unknown_repo(self, client: TestClient):
        """POST /api/repos/{unknown_id}/retry → 404."""
        resp = client.post(f"/api/repos/{uuid.uuid4()}/retry", headers=AUTH)
        assert resp.status_code == 404

    def test_delete_repo_removes_chunks(self, client: TestClient):
        """DELETE /api/chat/{cid}/repos/{id} removes Repo and its chunks."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Repo, RepoChunk

        cid = str(uuid.uuid4())
        with mock_github_fetch([("main.py", "code here")]):
            add_resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/del-repo",
                    "branch": "main",
                },
                headers=AUTH,
            )
        assert add_resp.status_code == 200
        repo_id = add_resp.json()["repo_id"]

        # Verify chunks exist
        with Session(db_module.get_engine()) as session:
            chunks = session.exec(
                select(RepoChunk).where(RepoChunk.repo_id == uuid.UUID(repo_id))
            ).all()
            assert len(chunks) > 0

        # Delete the repo
        del_resp = client.delete(f"/api/chat/{cid}/repos/{repo_id}", headers=AUTH)
        assert del_resp.status_code == 204

        # Verify repo and chunks are gone
        with Session(db_module.get_engine()) as session:
            repo = session.get(Repo, uuid.UUID(repo_id))
            assert repo is None
            chunks = session.exec(
                select(RepoChunk).where(RepoChunk.repo_id == uuid.UUID(repo_id))
            ).all()
            assert len(chunks) == 0


# ---------------------------------------------------------------------------
# Phase 9 — context.repos drives retrieval
# ---------------------------------------------------------------------------


class TestContextReposDrivesRetrieval:
    def test_context_repos_injects_chunks_into_prompt(
        self, client: TestClient, monkeypatch
    ):
        """When context.repos is sent, repo chunks appear in the AI prompt."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        cid = str(uuid.uuid4())
        fake_files = [("app.py", "def greet():\n    return 'Hello from repo'")]

        with mock_github_fetch(fake_files,
        ):
            add_resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/context-repo",
                    "branch": "main",
                },
                headers=AUTH,
            )
        assert add_resp.status_code == 200
        repo_id = add_resp.json()["repo_id"]

        import backend.app.chat_routes as cr

        captured: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured.append(system_prompt or "")
            return "got it"

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            chat_resp = client.post(
                "/api/chat",
                json={
                    "message": "greet function",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "repos": [repo_id],
                    },
                },
                headers=AUTH,
            )

        assert chat_resp.status_code == 200
        prompt = captured[0]
        # Repo context or status must appear
        assert "REPO" in prompt or "app.py" in prompt or "greet" in prompt

    def test_context_files_github_repo_path_no_longer_drives_repo_prompt(
        self, client: TestClient, monkeypatch
    ):
        """github_repo file refs no longer inject repo chunks into the prompt."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        cid = str(uuid.uuid4())
        fake_files = [("legacy.py", "def legacy(): pass")]

        with mock_github_fetch(fake_files,
        ):
            add_resp = client.post(
                f"/api/chat/{cid}/github/repos",
                json={"repo_url": "https://github.com/owner/legacy-repo", "branch": "main"},
                headers=AUTH,
            )
        assert add_resp.status_code == 201
        file_id = add_resp.json()["id"]

        import backend.app.chat_routes as cr

        captured: list[str] = []

        def _fake_openai(msg, key, history=None, system_prompt=None):
            captured.append(system_prompt or "")
            return "legacy ok"

        with patch.object(cr, "_call_openai_chat", side_effect=_fake_openai):
            chat_resp = client.post(
                "/api/chat",
                json={
                    "message": "what is legacy",
                    "conversation_id": cid,
                    "agent_mode": False,
                    "context": {
                        "session_id": None,
                        "domain_profile_id": None,
                        "files": [
                            {
                                "id": file_id,
                                "filename": "owner/legacy-repo",
                                "category": "github_repo",
                                "mime_type": "application/x-git-repository",
                            }
                        ],
                    },
                },
                headers=AUTH,
            )

        assert chat_resp.status_code == 200
        prompt = captured[0]
        assert "REPO CONTEXT" not in prompt


# ---------------------------------------------------------------------------
# GLOBAL_REPO_ASSET_INGESTION_AND_CONTEXT_BINDING_V1
# POST /api/repos/add — global upsert + conversation binding
# ---------------------------------------------------------------------------


class TestGlobalRepoAddEndpoint:
    """Tests for the global repo upsert + conversation binding endpoint."""

    def test_add_repo_returns_200(self, client: TestClient):
        """POST /api/repos/add returns 200 with repo_id and status."""
        with mock_github_fetch([("README.md", "# Hello")]):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": str(uuid.uuid4()),
                    "repo_url": "https://github.com/owner/global-repo",
                    "branch": "main",
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "repo_id" in body
        assert "status" in body
        assert body["status"] in ("pending", "running", "success", "failed")

    def test_add_repo_is_globally_idempotent(self, client: TestClient):
        """Two different conversations adding the same URL share ONE Repo row.
        First call returns 200; second returns 409 because the repo is already
        running or ingested (CONTRACT V2 Section 7)."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import Repo

        conv_a = str(uuid.uuid4())
        conv_b = str(uuid.uuid4())
        payload_base = {"repo_url": "https://github.com/owner/shared-repo", "branch": "main"}

        with mock_github_fetch([("main.py", "print('hi')")]):
            resp_a = client.post(
                "/api/repos/add",
                json={"conversation_id": conv_a, **payload_base},
                headers=AUTH,
            )
            resp_b = client.post(
                "/api/repos/add",
                json={"conversation_id": conv_b, **payload_base},
                headers=AUTH,
            )

        assert resp_a.status_code == 200
        # Second call: repo already running or ingested — must return 409.
        assert resp_b.status_code == 409

        # Only ONE Repo row must exist for this (repo_url, branch).
        with Session(db_module.get_engine()) as session:
            repos = session.exec(
                select(Repo).where(
                    Repo.repo_url == payload_base["repo_url"],
                    Repo.branch == payload_base["branch"],
                )
            ).all()
            assert len(repos) == 1

    def test_add_repo_creates_conversation_binding(self, client: TestClient):
        """POST /api/repos/add creates a ConversationRepo binding row."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import ConversationRepo

        cid = str(uuid.uuid4())

        with mock_github_fetch([("app.py", "code")]):
            resp = client.post(
                "/api/repos/add",
                json={
                    "conversation_id": cid,
                    "repo_url": "https://github.com/owner/binding-repo",
                    "branch": "main",
                },
                headers=AUTH,
            )

        assert resp.status_code == 200
        repo_id = uuid.UUID(resp.json()["repo_id"])

        with Session(db_module.get_engine()) as session:
            binding = session.exec(
                select(ConversationRepo).where(
                    ConversationRepo.conversation_id == cid,
                    ConversationRepo.repo_id == repo_id,
                )
            ).first()
            assert binding is not None

    def test_add_repo_binding_is_idempotent(self, client: TestClient):
        """Calling /api/repos/add twice for the same (conversation, repo)
        does not create duplicate bindings."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import ConversationRepo

        cid = str(uuid.uuid4())
        payload = {
            "conversation_id": cid,
            "repo_url": "https://github.com/owner/idempotent-binding",
            "branch": "main",
        }

        with mock_github_fetch([("README.md", "hello")]):
            resp1 = client.post("/api/repos/add", json=payload, headers=AUTH)
            client.post("/api/repos/add", json=payload, headers=AUTH)

        repo_id = uuid.UUID(resp1.json()["repo_id"])

        with Session(db_module.get_engine()) as session:
            bindings = session.exec(
                select(ConversationRepo).where(
                    ConversationRepo.conversation_id == cid,
                    ConversationRepo.repo_id == repo_id,
                )
            ).all()
            assert len(bindings) == 1

    def test_add_repo_invalid_url(self, client: TestClient):
        """POST /api/repos/add with a non-GitHub URL returns 400."""
        resp = client.post(
            "/api/repos/add",
            json={
                "conversation_id": str(uuid.uuid4()),
                "repo_url": "https://example.com/not-a-repo",
                "branch": "main",
            },
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_old_endpoint_returns_410(self, client: TestClient):
        """Old POST /api/chat/{cid}/repos is permanently retired — returns 410."""
        cid = str(uuid.uuid4())
        resp = client.post(
            f"/api/chat/{cid}/repos",
            json={"repo_url": "https://github.com/owner/convergence-repo", "branch": "main"},
            headers=AUTH,
        )
        assert resp.status_code == 410
        assert "DEPRECATED" in resp.json().get("detail", "")
        assert "/api/repos/add" in resp.json().get("detail", "")
