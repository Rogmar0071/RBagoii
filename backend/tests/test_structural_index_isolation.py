from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_structural_isolation")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_structural_isolation.db"
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


def _seed_repo(file_count: int = 200) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoIndexRegistry

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-index",
                owner="acme",
                name="repo-index",
                branch="main",
                ingestion_status="success",
                total_files=file_count,
                total_chunks=file_count,
            )
        )
        for i in range(file_count):
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_id=uuid.uuid4(),
                    file_path=f"src/file_{i}.py",
                    content=f"# file {i}\n",
                    chunk_index=0,
                    token_estimate=4,
                )
            )
        session.add(
            RepoIndexRegistry(
                repo_id=repo_id,
                total_files=file_count,
                total_chunks=file_count,
                indexed=True,
                status="indexed",
            )
        )
        session.commit()
    return str(repo_id)


def test_phase1_index_truth_validation_snapshot():
    import backend.app.database as db_module
    from backend.app.models import RepoChunk, RepoIndexRegistry

    repo_id = _seed_repo(200)
    repo_uuid = uuid.UUID(repo_id)
    with Session(db_module.get_engine()) as session:
        reg = session.get(RepoIndexRegistry, repo_uuid)
        assert reg is not None
        paths = [
            row.file_path
            for row in session.exec(
                select(RepoChunk)
                .where(RepoChunk.repo_id == repo_uuid)
                .order_by(RepoChunk.file_path.asc(), RepoChunk.chunk_index.asc())
            )
            .all()
        ]

    assert len(set(paths)) == 200
    assert reg.total_files == 200
    sample = sorted(set(paths))[:10]
    assert len(sample) == 10


def test_phase2_debug_endpoint_returns_full_list(client: TestClient):
    repo_id = _seed_repo(200)
    resp = client.get(f"/debug/structural/{repo_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 200
    assert len(body["files"]) == 200
    assert len(body["files"]) == len(set(body["files"]))


def test_phase3_completeness_guard_enforced(client: TestClient):
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoIndexRegistry

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-mismatch",
                owner="acme",
                name="repo-mismatch",
                branch="main",
                ingestion_status="success",
                total_files=200,
                total_chunks=200,
            )
        )
        for i in range(3):
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_id=uuid.uuid4(),
                    file_path=f"src/file_{i}.py",
                    content=f"# file {i}\n",
                    chunk_index=0,
                    token_estimate=4,
                )
            )
        session.add(
            RepoIndexRegistry(
                repo_id=repo_id,
                total_files=200,
                total_chunks=200,
                indexed=True,
                status="indexed",
            )
        )
        session.commit()

    resp = client.get(f"/debug/structural/{repo_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["error_code"] == "INSUFFICIENT_CONTEXT"


def test_phase4_direct_handler_no_retrieval_or_llm(monkeypatch: pytest.MonkeyPatch):
    import backend.app.database as db_module
    import backend.app.repo_retrieval as rr
    from backend.app.structural_handler import handle_structural_query

    repo_id = _seed_repo(200)
    repo_uuid = uuid.UUID(repo_id)

    monkeypatch.setattr(
        rr,
        "retrieve_relevant_chunks",
        lambda *args, **kwargs: pytest.fail("called"),
    )

    with Session(db_module.get_engine()) as session:
        result = handle_structural_query(
            db=session,
            repo_ids=[repo_uuid],
            query_text="how many files",
        )
    assert result["error_code"] is None
    assert result["data"]["count"] == 200
    assert result["retrieved_chunks"] == result["total_chunks"] == 200
