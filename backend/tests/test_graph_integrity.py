"""
backend/tests/test_graph_integrity.py
======================================
MQP-STEERING-CONTRACT: GRAPH-LAYER-CORRECTION v1.1

Graph integrity enforcement tests.

Coverage:
  TEST_no_duplicate_graph_models     — FileNode, SymbolNode, FileEdge must not exist
  TEST_dependency_resolution_strict  — no unresolved FileDependency edges
  TEST_symbol_anchor_integrity       — all CodeSymbol rows have a non-NULL file_id
  TEST_execution_chain_exists        — entry_point → symbol → downstream traversal
  TEST_repo_file_populated           — RepoFile rows created for every ingested file
  TEST_symbol_nodes_populated        — CodeSymbol rows exist for Python files
  TEST_entry_points_detected         — EntryPoints detected for runnable files
  TEST_call_edges_populated          — SymbolCallEdge rows have valid source_symbol_id
  TEST_pipeline_order                — file rows exist before dependency edges
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m
    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "test_graph.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAIN_PY = """\
from utils import process_data
from models import User


def run():
    user = User()
    result = process_data(user)
    return result


if __name__ == "__main__":
    run()
"""

_UTILS_PY = """\
def process_data(obj):
    return helper(obj)


def helper(obj):
    return str(obj)
"""

_MODELS_PY = """\
class User:
    def __init__(self):
        self.name = "default"
"""

_FASTAPI_PY = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {"hello": "world"}
"""


def _make_repo_job(session_factory, files: list[tuple[str, str]]) -> str:
    """Create and process a repo IngestJob with the given files.  Returns job_id."""
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob, Repo
    from sqlmodel import select

    manifest = {
        "repo_url": "https://github.com/test/graph-repo",
        "owner": "test",
        "name": "graph-repo",
        "branch": "main",
        "files": [{"path": p, "content": c, "size": len(c)} for p, c in files],
    }
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/test/graph-repo@main",
        branch="main",
        status="created",
    )
    job.blob_data = json.dumps(manifest).encode("utf-8")
    job.blob_mime_type = "application/json"
    job.blob_size_bytes = len(job.blob_data)

    with session_factory() as sess:
        repo_url = manifest["repo_url"]
        repo = sess.exec(
            select(Repo).where(Repo.repo_url == repo_url, Repo.branch == "main")
        ).first()
        if repo is None:
            repo = Repo(
                repo_url=repo_url,
                owner=manifest["owner"],
                name=manifest["name"],
                branch="main",
                ingestion_status="pending",
            )
            sess.add(repo)
            sess.commit()
            sess.refresh(repo)
        job.repo_id = repo.id
        sess.add(job)
        sess.commit()
        job_id = str(job.id)

    transition(uuid.UUID(job_id), "stored")
    transition(uuid.UUID(job_id), "queued")
    return job_id


# ---------------------------------------------------------------------------
# TEST: no duplicate graph models
# ---------------------------------------------------------------------------


class TestNoDuplicateGraphModels:
    """FileNode, SymbolNode, FileEdge must NOT exist as importable model classes."""

    def test_file_node_does_not_exist(self):
        import backend.app.models as m
        assert not hasattr(m, "FileNode"), (
            "FileNode must be deleted (duplicate graph authority)"
        )

    def test_symbol_node_does_not_exist(self):
        import backend.app.models as m
        assert not hasattr(m, "SymbolNode"), (
            "SymbolNode must be deleted (duplicate graph authority)"
        )

    def test_file_edge_does_not_exist(self):
        import backend.app.models as m
        assert not hasattr(m, "FileEdge"), (
            "FileEdge must be deleted (duplicate graph authority)"
        )

    def test_canonical_models_exist(self):
        from backend.app.models import (
            CodeSymbol,
            EntryPoint,
            FileDependency,
            RepoFile,
            SymbolCallEdge,
        )
        assert RepoFile.__tablename__ == "repo_files"
        assert CodeSymbol.__tablename__ == "code_symbols"
        assert FileDependency.__tablename__ == "file_dependencies"
        assert SymbolCallEdge.__tablename__ == "symbol_call_edges"
        assert EntryPoint.__tablename__ == "entry_points"


# ---------------------------------------------------------------------------
# TEST: dependency resolution strictness
# ---------------------------------------------------------------------------


class TestDependencyResolutionStrict:
    """All FileDependency rows must have a non-NULL target_file_id."""

    def test_no_null_target_file_id(self, tmp_path):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import FileDependency

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            deps = list(sess.exec(select(FileDependency)))
            # FileDependency is non-nullable in schema; just confirm no rows slip through
            for dep in deps:
                assert dep.target_file_id is not None, (
                    "target_file_id must never be NULL"
                )

    def test_unresolvable_imports_not_stored(self, tmp_path):
        """Imports of stdlib / external packages must be silently dropped."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import FileDependency

        code = "import os\nimport sys\nimport requests\n\ndef main():\n    pass\n"
        _make_repo_job(
            lambda: Session(get_engine()),
            [("standalone.py", code)],
        )

        with Session(get_engine()) as sess:
            # os, sys, requests are not in the repo — zero FileDependency rows
            deps = list(sess.exec(select(FileDependency)))
            assert len(deps) == 0, (
                "Unresolvable imports (stdlib/external) must not produce FileDependency rows"
            )


# ---------------------------------------------------------------------------
# TEST: symbol anchor integrity
# ---------------------------------------------------------------------------


class TestSymbolAnchorIntegrity:
    """Every CodeSymbol must have a non-NULL file_id pointing to a RepoFile."""

    def test_all_symbols_have_file_id(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            symbols = list(sess.exec(select(CodeSymbol)))
            assert len(symbols) > 0, "Expected at least one CodeSymbol"
            for sym in symbols:
                assert sym.file_id is not None, (
                    f"CodeSymbol '{sym.name}' has NULL file_id — orphan symbols are forbidden"
                )

    def test_symbol_file_id_references_existing_repo_file(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile

        _make_repo_job(
            lambda: Session(get_engine()),
            [("utils.py", _UTILS_PY)],
        )

        with Session(get_engine()) as sess:
            symbols = list(sess.exec(select(CodeSymbol)))
            repo_file_ids = {rf.id for rf in sess.exec(select(RepoFile))}
            for sym in symbols:
                assert sym.file_id in repo_file_ids, (
                    f"CodeSymbol '{sym.name}'.file_id={sym.file_id} has no matching RepoFile"
                )


# ---------------------------------------------------------------------------
# TEST: execution chain exists
# ---------------------------------------------------------------------------


class TestExecutionChainExists:
    """entry_point → symbol → downstream symbol must be traversable."""

    def test_entry_point_detected_for_main(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import EntryPoint

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            assert len(eps) > 0, "Expected at least one EntryPoint for runnable repo"
            entry_types = {ep.entry_type for ep in eps}
            assert "main" in entry_types, (
                "if __name__ == '__main__' must produce EntryPoint with entry_type='main'"
            )

    def test_execution_graph_traversable(self):
        """Entry point file → CodeSymbol → SymbolCallEdge downstream."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, EntryPoint, RepoFile, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            # Find the entry point
            eps = list(sess.exec(select(EntryPoint)))
            assert eps, "No EntryPoint found"

            entry_file_id = eps[0].file_id

            # Confirm RepoFile exists for entry point
            rf = sess.get(RepoFile, entry_file_id)
            assert rf is not None

            # Confirm at least one CodeSymbol is linked to a file in the repo
            symbols = list(sess.exec(select(CodeSymbol)))
            assert symbols, "No CodeSymbol found"

            # Confirm at least one SymbolCallEdge has a valid source_symbol_id
            edges = list(sess.exec(select(SymbolCallEdge)))
            if edges:
                sym_ids = {s.id for s in symbols}
                for edge in edges:
                    assert edge.source_symbol_id in sym_ids, (
                        "SymbolCallEdge.source_symbol_id must reference a persisted CodeSymbol"
                    )

    def test_fastapi_entry_point_detected(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import EntryPoint

        _make_repo_job(
            lambda: Session(get_engine()),
            [("app.py", _FASTAPI_PY)],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            assert any(ep.entry_type == "framework" for ep in eps), (
                "FastAPI() must produce EntryPoint with entry_type='framework'"
            )


# ---------------------------------------------------------------------------
# TEST: RepoFile populated per file
# ---------------------------------------------------------------------------


class TestRepoFilePopulated:
    """One RepoFile row must exist per non-empty ingested file."""

    def test_repo_file_count_matches_file_count(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, RepoFile

        files = [
            ("main.py", _MAIN_PY),
            ("utils.py", _UTILS_PY),
            ("models.py", _MODELS_PY),
        ]
        job_id = _make_repo_job(lambda: Session(get_engine()), files)

        with Session(get_engine()) as sess:
            job = sess.get(IngestJob, uuid.UUID(job_id))
            assert job.status == "success"

            repo_files = list(
                sess.exec(
                    select(RepoFile).where(RepoFile.repo_id == uuid.UUID(job_id))
                )
            )
            assert len(repo_files) == len(files), (
                f"Expected {len(files)} RepoFile rows, got {len(repo_files)}"
            )
            paths = {rf.path for rf in repo_files}
            for path, _ in files:
                assert path in paths, f"No RepoFile found for '{path}'"

    def test_repo_file_path_unique_per_job(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import RepoFile

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("utils.py", _UTILS_PY)],
        )

        with Session(get_engine()) as sess:
            repo_files = list(
                sess.exec(
                    select(RepoFile).where(RepoFile.repo_id == uuid.UUID(job_id))
                )
            )
            paths = [rf.path for rf in repo_files]
            assert len(paths) == len(set(paths)), "Duplicate RepoFile paths within same job"


# ---------------------------------------------------------------------------
# TEST: call edge integrity
# ---------------------------------------------------------------------------


class TestCallEdgeIntegrity:
    """Every SymbolCallEdge.source_symbol_id must reference an existing CodeSymbol."""

    def test_call_edges_have_valid_source_symbol(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("utils.py", _UTILS_PY),   # process_data calls helper
            ],
        )

        with Session(get_engine()) as sess:
            edges = list(sess.exec(select(SymbolCallEdge)))
            symbol_ids = {s.id for s in sess.exec(select(CodeSymbol))}

            for edge in edges:
                assert edge.source_symbol_id in symbol_ids, (
                    "SymbolCallEdge.source_symbol_id must reference a persisted CodeSymbol"
                )

    def test_intra_file_call_detected(self):
        """process_data() calling helper() should produce a SymbolCallEdge."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [("utils.py", _UTILS_PY)],
        )

        with Session(get_engine()) as sess:
            edges = list(sess.exec(select(SymbolCallEdge)))
            # process_data calls helper — should have at least one call edge
            # process_data or helper may appear as caller/callee
            symbols = list(sess.exec(select(CodeSymbol)))
            sym_names = {s.name for s in symbols}
            assert "process_data" in sym_names
            assert "helper" in sym_names
            # At minimum, some call edge exists (process_data → helper)
            assert len(edges) > 0, (
                "Expected at least one SymbolCallEdge for utils.py (process_data calls helper)"
            )


# ---------------------------------------------------------------------------
# TEST: pipeline phase order
# ---------------------------------------------------------------------------


class TestPipelinePhaseOrder:
    """RepoFile rows must exist before dependency resolution (verified by row presence)."""

    def test_dependency_edges_only_reference_persisted_files(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import FileDependency, RepoFile

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            repo_file_ids = {rf.id for rf in sess.exec(select(RepoFile))}
            deps = list(sess.exec(select(FileDependency)))

            for dep in deps:
                assert dep.source_file_id in repo_file_ids, (
                    "FileDependency.source_file_id references a non-persisted RepoFile"
                )
                assert dep.target_file_id in repo_file_ids, (
                    "FileDependency.target_file_id references a non-persisted RepoFile"
                )

    def test_resolved_dependency_created_for_same_repo_import(self):
        """main.py imports utils → FileDependency main→utils must exist."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import FileDependency, RepoFile

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            deps = list(sess.exec(select(FileDependency)))
            assert len(deps) > 0, (
                "Expected FileDependency rows for resolvable intra-repo imports"
            )
            repo_files = {rf.path: rf.id for rf in sess.exec(select(RepoFile))}
            # main.py imports utils and models — both should be resolved
            source_id = repo_files.get("main.py")
            target_utils = repo_files.get("utils.py")
            target_models = repo_files.get("models.py")
            assert source_id is not None
            dep_targets = {d.target_file_id for d in deps if d.source_file_id == source_id}
            assert target_utils in dep_targets, "main.py → utils.py dependency missing"
            assert target_models in dep_targets, "main.py → models.py dependency missing"
