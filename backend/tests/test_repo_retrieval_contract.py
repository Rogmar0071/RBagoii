from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_repo_contract")

pytestmark = pytest.mark.skip(
    reason="Legacy chat retrieval contract assertions are obsolete under session-authority routing."
)

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_repo_contract.db"
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


def _seed_repo_with_chunks(conversation_id: str, repo_name: str = "repo-a") -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoFile

    repo_id = uuid.uuid4()
    file_main_id = uuid.uuid4()
    file_readme_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        repo = Repo(
            id=repo_id,
            conversation_id=conversation_id,
            repo_url=f"https://github.com/acme/{repo_name}",
            owner="acme",
            name=repo_name,
            branch="main",
            ingestion_status="success",
            total_files=2,
            total_chunks=2,
        )
        session.add(repo)
        session.add(
            RepoFile(
                id=file_main_id, repo_id=repo_id, path="src/main.py",
                language="python", size_bytes=10,
            )
        )
        session.add(
            RepoFile(
                id=file_readme_id, repo_id=repo_id, path="README.md",
                language="markdown", size_bytes=10,
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_main_id,
                file_path="src/main.py",
                content="def answer():\n    return 42\n",
                chunk_index=0,
                token_estimate=8,
                graph_group=str(uuid.uuid4()),
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_readme_id,
                file_path="README.md",
                content="Project contract and setup instructions.",
                chunk_index=0,
                token_estimate=8,
                graph_group=str(uuid.uuid4()),
            )
        )
        session.commit()
    return str(repo_id)


def _seed_repo_with_many_chunks(
    conversation_id: str, repo_name: str = "repo-large", file_count: int = 200
) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoFile

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        repo = Repo(
            id=repo_id,
            conversation_id=conversation_id,
            repo_url=f"https://github.com/acme/{repo_name}",
            owner="acme",
            name=repo_name,
            branch="main",
            ingestion_status="success",
            total_files=file_count,
            total_chunks=file_count,
        )
        session.add(repo)
        for i in range(file_count):
            file_id = uuid.uuid4()
            session.add(
                RepoFile(
                    id=file_id,
                    repo_id=repo_id,
                    path=f"src/file_{i}.py",
                    language="python",
                    size_bytes=10,
                )
            )
            session.add(
                RepoChunk(
                    repo_id=repo_id,
                    file_id=file_id,
                    file_path=f"src/file_{i}.py",
                    content=f"# files inventory entry {i}\nTOTAL FILES marker\n",
                    chunk_index=0,
                    token_estimate=8,
                    graph_group=str(uuid.uuid4()),
                )
            )
        session.commit()
    return str(repo_id)


def _seed_repo_with_partial_surface(
    conversation_id: str, repo_name: str = "repo-partial", total_files: int = 20
) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoFile

    repo_id = uuid.uuid4()
    file_alpha_id = uuid.uuid4()
    file_beta_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        repo = Repo(
            id=repo_id,
            conversation_id=conversation_id,
            repo_url=f"https://github.com/acme/{repo_name}",
            owner="acme",
            name=repo_name,
            branch="main",
            ingestion_status="success",
            total_files=total_files,
            total_chunks=2,
        )
        session.add(repo)
        session.add(
            RepoFile(
                id=file_alpha_id, repo_id=repo_id, path="src/alpha.py",
                language="python", size_bytes=10,
            )
        )
        session.add(
            RepoFile(
                id=file_beta_id, repo_id=repo_id, path="src/beta.py",
                language="python", size_bytes=10,
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_alpha_id,
                file_path="src/alpha.py",
                content="def alpha():\n    return 'alpha'\n",
                chunk_index=0,
                token_estimate=8,
                graph_group=str(uuid.uuid4()),
            )
        )
        session.add(
            RepoChunk(
                repo_id=repo_id,
                file_id=file_beta_id,
                file_path="src/beta.py",
                content="def beta():\n    return 'beta'\n",
                chunk_index=0,
                token_estimate=8,
                graph_group=str(uuid.uuid4()),
            )
        )
        session.commit()
    return str(repo_id)


def test_visibility_structure_reports_indexed_surface(client: TestClient):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    resp = client.get(f"/repos/{repo_id}/structure", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_files"] > 0
    assert body["total_chunks"] > 0
    assert len(body["files"]) == body["total_files"]


def test_retrieval_is_repo_scoped(client: TestClient):
    conv_id = str(uuid.uuid4())
    repo_a = _seed_repo_with_chunks(conversation_id=conv_id, repo_name="repo-a")
    _ = _seed_repo_with_chunks(conversation_id=conv_id, repo_name="repo-b")

    resp = client.post(
        f"/repos/{repo_a}/retrieve",
        json={"query": "answer function", "top_k": 12},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieved_count"] > 0
    assert all(
        chunk["file_path"] in {"src/main.py", "README.md"} for chunk in body["retrieved_chunks"]
    )


def test_retrieval_is_deterministic_for_same_query(client: TestClient):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    results = []
    for _ in range(3):
        resp = client.post(
            f"/repos/{repo_id}/retrieve",
            json={"query": "project setup", "top_k": 12},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        results.append(resp.json()["retrieved_chunks"])
    assert results[0] == results[1] == results[2]


def test_retrieval_failure_surfaces_contract_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    import backend.app.github_routes as gr

    monkeypatch.setattr(gr, "_score_chunk", _boom)
    resp = client.post(
        f"/repos/{repo_id}/retrieve",
        json={"query": "answer", "top_k": 12},
        headers=AUTH,
    )
    assert resp.status_code == 500
    assert resp.json().get("detail") == "RETRIEVAL_FAILURE"


def test_chat_grounding_uses_repo_context(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    def _fake_openai(message, key, history=None, system_prompt=None):  # noqa: ARG001
        if system_prompt and "FILE: src/main.py" in system_prompt:
            return "Grounded in src/main.py"
        return "Not grounded"

    monkeypatch.setattr(cr, "_call_openai_chat", _fake_openai)

    resp = client.post(
        "/api/chat",
        json={
            "message": "Where is the answer function?",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "src/main.py" in resp.json()["reply"]


def test_chat_returns_no_index_data_when_retrieval_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_run_retrieval_query", lambda *args, **kwargs: [])
    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: "should-not-run")
    resp = client.post(
        "/api/chat",
        json={
            "message": "Explain repository setup",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"] == "REPO_CONTEXT_EMPTY"
    assert resp.json()["error_code"] == "REPO_CONTEXT_EMPTY"
    assert resp.json()["retrieved_count"] == 0
    assert resp.json()["repo_count"] == 1
    assert "total_chunks" in resp.json()
    assert resp.json()["retrieved_chunks"] == 0


def test_chat_large_repo_grounding_and_debug_metadata(client: TestClient, monkeypatch):
    repo_id = _seed_repo_with_many_chunks(conversation_id=str(uuid.uuid4()), file_count=200)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    def _fake_openai(user_message, key, history=None, system_prompt=None):  # noqa: ARG001
        assert system_prompt is not None
        assert "REPO_ID:" in system_prompt
        assert "FILE:" in system_prompt
        for line in system_prompt.splitlines():
            if line.startswith("FILE: "):
                return f"Grounded via {line.removeprefix('FILE: ')}"
        return "RETRIEVAL_FAILURE"

    monkeypatch.setattr(cr, "_call_openai_chat", _fake_openai)

    resp = client.post(
        "/api/chat",
        json={
            "message": "where is inventory entry defined",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repo_count"] == 1
    assert body["total_chunks"] >= 200
    assert body["retrieved_chunks"] > 0
    assert body["error_code"] is None
    assert "file_" in body["reply"]


def test_chat_flags_retrieval_failure_when_reply_not_grounded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    import backend.app.chat_routes as cr

    monkeypatch.setattr(
        cr,
        "_call_openai_chat",
        lambda *args, **kwargs: "This answer does not reference repository data.",
    )

    resp = client.post(
        "/api/chat",
        json={
            "message": "Where is the answer function?",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieved_chunks"] > 0
    assert body["error_code"] == "RETRIEVAL_FAILURE"
    assert body["reply"] == "RETRIEVAL_FAILURE"


def test_repo_files_endpoint_is_paginated(client: TestClient):
    repo_id = _seed_repo_with_many_chunks(conversation_id=str(uuid.uuid4()), file_count=200)
    resp = client.get(f"/repos/{repo_id}/files?page=1&per_page=20", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file_count"] == 200
    assert len(body["files"]) == 20
    assert body["has_more"] is True


def test_repo_retrieve_blocks_when_chunk_count_below_threshold(client: TestClient):
    repo_id = _seed_repo_with_chunks(conversation_id=str(uuid.uuid4()))
    import backend.app.database as db_module
    from backend.app.models import RepoIndexRegistry

    with Session(db_module.get_engine()) as session:
        session.add(
            RepoIndexRegistry(
                repo_id=uuid.UUID(repo_id),
                total_files=2,
                total_chunks=2,
                indexed=True,
                status="indexed",
            )
        )
        session.commit()
    resp = client.post(
        f"/repos/{repo_id}/retrieve",
        json={"query": "answer function", "top_k": 12},
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "INSUFFICIENT_CONTEXT"


def test_chat_global_query_without_repo_binding_returns_no_context(client: TestClient):
    resp = client.post(
        "/api/chat",
        json={
            "message": "how many files are in this repository?",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_code"] == "NO_CONTEXT"
    assert body["reply"] == "No repository bound to this conversation"


def test_chat_global_query_with_partial_context_returns_data_incomplete(client: TestClient):
    repo_id = _seed_repo_with_partial_surface(conversation_id=str(uuid.uuid4()), total_files=20)
    resp = client.post(
        "/api/chat",
        json={
            "message": "summarize repo",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_code"] == "DATA_INCOMPLETE"
    assert body["details"]["total_files"] == 20
    assert body["details"]["retrieved_files"] == 2


def test_chat_local_query_succeeds_with_partial_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_partial_surface(conversation_id=str(uuid.uuid4()), total_files=20)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_call_openai_chat", lambda *args, **kwargs: "From src/alpha.py")
    resp = client.post(
        "/api/chat",
        json={
            "message": "explain alpha function behavior",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_code"] is None
    assert "alpha.py" in body["reply"]


def test_chat_global_query_with_full_context_returns_structural_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_many_chunks(conversation_id=str(uuid.uuid4()), file_count=179)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import backend.app.chat_routes as cr
    import backend.app.database as db_module
    from backend.app.models import RepoChunk

    with Session(db_module.get_engine()) as session:
        chunks = session.exec(
            select(RepoChunk).where(RepoChunk.repo_id == uuid.UUID(repo_id))
        ).all()
    monkeypatch.setattr(
        cr,
        "_run_retrieval_query",
        lambda *args, **kwargs: chunks,
    )
    resp = client.post(
        "/api/chat",
        json={
            "message": "how many files are in this repository?",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "STRUCTURAL_QUERY_RESULT"
    assert body["file_count"] == 179


def test_chat_blocks_llm_overclaim_when_context_not_full(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    repo_id = _seed_repo_with_partial_surface(conversation_id=str(uuid.uuid4()), total_files=20)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import backend.app.chat_routes as cr

    monkeypatch.setattr(
        cr,
        "_call_openai_chat",
        lambda *args, **kwargs: "I checked all files in the entire repo and here is the answer.",
    )
    resp = client.post(
        "/api/chat",
        json={
            "message": "explain architecture",
            "conversation_id": str(uuid.uuid4()),
            "agent_mode": False,
                    "alignment_confirmed": True,
            "context": {"repos": [repo_id]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_code"] == "CONTEXT_VIOLATION"
    assert body["reply"].startswith("CONTEXT VIOLATION:")
