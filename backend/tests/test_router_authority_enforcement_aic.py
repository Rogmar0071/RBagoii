from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from backend.app.query_classifier import QueryType
from backend.app.query_router import (
    ExecutionTrace,
    RuntimeViolationError,
    execute_query,
    verify_execution_trace,
)

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_router_enforcement")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"
AUTH = {"Authorization": "Bearer " + TOKEN}
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_router_enforcement.db"
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


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import backend.app.chat_routes as cr

    monkeypatch.setattr(cr, "_run_chat_llm", lambda *args, **kwargs: "stub")


def _seed_ingest_context(conversation_id: str) -> str:
    import backend.app.database as db_module
    from backend.app.models import CodeSymbol, EntryPoint, IngestJob, RepoChunk, RepoFile

    job_id = uuid.uuid4()
    file_id = uuid.uuid4()
    with Session(db_module.get_engine()) as db:
        db.add(
            IngestJob(
                id=job_id,
                kind="repo",
                source="https://github.com/acme/repo-router-enforcement",
                branch="main",
                status="success",
                conversation_id=conversation_id,
                file_count=1,
                chunk_count=1,
            )
        )
        db.add(
            RepoFile(
                id=file_id,
                repo_id=job_id,
                path="src/file_0.py",
                language="python",
                size_bytes=10,
            )
        )
        db.add(
            CodeSymbol(
                file_id=file_id,
                name="main",
                symbol_type="function",
                start_line=1,
                end_line=1,
            )
        )
        db.add(EntryPoint(file_id=file_id, entry_type="main", line=1))
        db.add(
            RepoChunk(
                ingest_job_id=job_id,
                file_id=file_id,
                file_path="src/file_0.py",
                content="# file 0\n",
                chunk_index=0,
                token_estimate=4,
            )
        )
        db.commit()
    return str(job_id)


def _seed_repo(file_count: int = 200) -> str:
    import backend.app.database as db_module
    from backend.app.models import Repo, RepoChunk, RepoFile, RepoIndexRegistry

    repo_id = uuid.uuid4()
    with Session(db_module.get_engine()) as session:
        session.add(
            Repo(
                id=repo_id,
                repo_url="https://github.com/acme/repo-router-enforcement",
                owner="acme",
                name="repo-router-enforcement",
                branch="main",
                ingestion_status="success",
                total_files=file_count,
                total_chunks=file_count,
            )
        )
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


def _chat(client: TestClient, *, message: str, repo_id: str | None = None):
    conversation_id = str(uuid.uuid4())
    _seed_ingest_context(conversation_id)
    context = {"repos": [repo_id]} if repo_id else {}
    return client.post(
        "/api/chat",
        json={
            "message": message,
            "conversation_id": conversation_id,
            "context": context,
            "agent_mode": False,
            "alignment_confirmed": True,
        },
        headers=AUTH,
    )


def _called_function_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def test_s1_chat_endpoint_has_no_direct_forbidden_calls() -> None:
    path = str(REPO_ROOT / "backend/app/chat_routes.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    chat_fn = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat":
            chat_fn = node
            break
    assert chat_fn is not None

    called_names = _called_function_names(chat_fn)
    assert "handle_structural_query" not in called_names
    assert "retrieve_relevant_chunks" not in called_names
    assert "_call_openai_chat" not in called_names


def test_s2_router_module_contains_llm_interface() -> None:
    path = str(REPO_ROOT / "backend/app/query_router.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "def call_llm(" in text
    assert "call_llm(" in text


def test_s3_structural_hard_lock_runtime_trace(client: TestClient) -> None:
    repo_id = _seed_repo(200)
    resp = _chat(client, repo_id=repo_id, message="how many files are in the repository")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reply" in body
    trace = body["execution_trace"]
    assert trace["classification"] in ("UNKNOWN", "STRUCTURAL")
    assert isinstance(trace["execution_path"], list)
    assert isinstance(trace["structural_called"], bool)
    assert trace["retrieval_called"] is False
    assert trace["llm_called"] is False


def test_s4_structural_no_repo_context_does_not_call_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.chat_routes as cr

    monkeypatch.setattr(
        cr,
        "_run_chat_llm",
        lambda *args, **kwargs: pytest.fail("llm should not run for structural query"),
    )
    resp = _chat(client, message="how many files are in the repository")
    assert resp.status_code == 200, resp.text
    assert resp.json()["execution_trace"]["llm_called"] is False


def test_s5_verify_execution_trace_invariants() -> None:
    verify_execution_trace(
        ExecutionTrace(
            classification="STRUCTURAL",
            execution_path=["chat", "route_query", "execute_query", "structural_handler"],
            structural_called=True,
            retrieval_called=False,
            llm_called=False,
        )
    )

    with pytest.raises(RuntimeViolationError):
        verify_execution_trace(
            ExecutionTrace(
                classification="STRUCTURAL",
                execution_path=["chat", "route_query", "execute_query", "call_llm"],
                structural_called=True,
                retrieval_called=False,
                llm_called=True,
            )
        )


def test_semantic_empty_retrieval_blocks_llm_in_router() -> None:
    llm_called = False

    def _structural(_: str) -> dict:
        return {
            "type": "structural",
            "file_count": 1,
            "files": ["x.py"],
            "source": "index_registry",
        }

    def _retrieval(_: str) -> dict:
        return {"retrieved_chunks": 0}

    def _llm(_: str, __: dict) -> str:
        nonlocal llm_called
        llm_called = True
        return "never"

    result, runtime = execute_query(
        classification=QueryType.SEMANTIC,
        query="what does this repo do",
        structural_handler=_structural,
        retrieval_handler=_retrieval,
        llm_handler=_llm,
    )
    assert result == {"error_code": "INSUFFICIENT_CONTEXT"}
    assert runtime.llm_called is False
    assert llm_called is False
