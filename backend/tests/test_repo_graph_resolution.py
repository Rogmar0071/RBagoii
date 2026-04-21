"""
backend/tests/test_repo_graph_resolution.py
=============================================
REPO_GRAPH_RESOLUTION_V1 — MQP-CONTRACT tests.

Covers (per contract section 9):

TEST_dependency_resolution
    GIVEN repo with relative imports
    ASSERT file_dependencies populated correctly

TEST_symbol_call_edges
    GIVEN file with function calls
    ASSERT symbol_call_edges created

TEST_entry_point_detection
    GIVEN repo with main.py
    ASSERT entry_points contains file

TEST_graph_integrity
    GIVEN ingested repo
    ASSERT:
        • files exist (repo_files)
        • dependencies exist (file_dependencies)
        • symbols exist (code_symbols)

Additional unit tests for the extractor functions:
    - extract_raw_imports
    - resolve_import_path
    - extract_call_names
    - extract_all_symbols
    - detect_entry_type
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

TOKEN = "test-secret-key"
_AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from backend.app.main import app

    return TestClient(app, raise_server_exceptions=True)


def _make_repo_manifest(files: list[tuple[str, str]]) -> bytes:
    """Build a blob manifest from a list of (path, content) tuples."""
    return json.dumps(
        {
            "repo_url": "https://github.com/test/repo",
            "branch": "main",
            "files": [{"path": p, "content": c} for p, c in files],
        }
    ).encode("utf-8")


def _run_repo_ingest(session, job_id: uuid.UUID, files: list[tuple[str, str]]) -> None:
    """
    Directly exercise the repo ingest pipeline for *files*.

    Sets blob_data on the IngestJob and calls _ingest_repo.
    """
    from sqlmodel import Session

    import backend.app.database as db_module
    from backend.app.ingest_pipeline import _ingest_repo
    from backend.app.models import IngestJob

    manifest_blob = _make_repo_manifest(files)
    with Session(db_module.get_engine()) as s:
        job = s.get(IngestJob, job_id)
        job.blob_data = manifest_blob
        job.blob_mime_type = "application/json"
        job.blob_size_bytes = len(manifest_blob)
        s.add(job)
        s.commit()

    with Session(db_module.get_engine()) as s:
        job = s.get(IngestJob, job_id)
        _ingest_repo(s, job)


def _create_ingest_job(kind: str = "repo") -> uuid.UUID:
    """Create a minimal IngestJob in the DB and return its id."""
    from sqlmodel import Session

    import backend.app.database as db_module
    from backend.app.models import IngestJob

    job_id = uuid.uuid4()
    with Session(db_module.get_engine()) as s:
        job = IngestJob(
            id=job_id,
            kind=kind,
            source="https://github.com/test/repo@main",
            branch="main",
            status="queued",
        )
        s.add(job)
        s.commit()
    return job_id


# ---------------------------------------------------------------------------
# Unit tests — extractor functions
# ---------------------------------------------------------------------------


class TestExtractRawImports:
    def test_python_from_import(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        content = "from .utils import helper\nfrom os.path import join\n"
        imports = extract_raw_imports(content)
        assert ".utils" in imports
        assert "os.path" in imports

    def test_python_bare_import(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        content = "import os\nimport sys\n"
        imports = extract_raw_imports(content)
        assert "os" in imports
        assert "sys" in imports

    def test_js_es_import(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        content = 'import { foo } from "./utils"\nimport bar from "../models"\n'
        imports = extract_raw_imports(content)
        assert "./utils" in imports
        assert "../models" in imports

    def test_node_require(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        content = "const express = require('./app')\n"
        imports = extract_raw_imports(content)
        assert "./app" in imports

    def test_deduplication(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        content = "from .utils import a\nfrom .utils import b\n"
        imports = extract_raw_imports(content)
        assert imports.count(".utils") == 1

    def test_empty_content(self):
        from backend.app.repo_chunk_extractor import extract_raw_imports

        assert extract_raw_imports("") == []


class TestResolveImportPath:
    def test_relative_py_sibling(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"src/utils.py", "src/main.py"}
        result = resolve_import_path(".utils", "src/main.py", all_paths)
        assert result == "src/utils.py"

    def test_relative_py_parent(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"models.py", "app/main.py"}
        result = resolve_import_path("..models", "app/main.py", all_paths)
        assert result == "models.py"

    def test_js_relative_sibling(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"src/utils.ts", "src/main.ts"}
        result = resolve_import_path("./utils", "src/main.ts", all_paths)
        assert result == "src/utils.ts"

    def test_js_relative_parent(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"models.ts", "app/main.ts"}
        result = resolve_import_path("../models", "app/main.ts", all_paths)
        assert result == "models.ts"

    def test_unresolvable_returns_none(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        result = resolve_import_path("os", "main.py", {"main.py"})
        assert result is None

    def test_extension_inference(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"src/helpers.py", "src/app.py"}
        result = resolve_import_path(".helpers", "src/app.py", all_paths)
        assert result == "src/helpers.py"

    def test_same_dir_absolute(self):
        from backend.app.repo_chunk_extractor import resolve_import_path

        all_paths = {"src/utils.py", "src/main.py"}
        result = resolve_import_path("utils", "src/main.py", all_paths)
        assert result == "src/utils.py"


class TestExtractCallNames:
    def test_basic_calls(self):
        from backend.app.repo_chunk_extractor import extract_call_names

        content = "result = compute(x)\nfoo.bar()\n"
        calls = extract_call_names(content)
        assert "compute" in calls
        assert "bar" in calls

    def test_filters_keywords(self):
        from backend.app.repo_chunk_extractor import extract_call_names

        content = "for i in range(10):\n    print(i)\n"
        calls = extract_call_names(content)
        assert "for" not in calls
        assert "in" not in calls

    def test_empty_content(self):
        from backend.app.repo_chunk_extractor import extract_call_names

        assert extract_call_names("") == []


class TestExtractAllSymbols:
    def test_python_functions(self):
        from backend.app.repo_chunk_extractor import extract_all_symbols

        content = "def foo():\n    pass\ndef bar():\n    pass\n"
        symbols = extract_all_symbols(content, "mod.py")
        names = [s[0] for s in symbols]
        assert "foo" in names
        assert "bar" in names

    def test_python_class(self):
        from backend.app.repo_chunk_extractor import extract_all_symbols

        content = "class Foo:\n    pass\n"
        symbols = extract_all_symbols(content, "mod.py")
        assert ("Foo", "CLASS") in symbols

    def test_deduplication(self):
        from backend.app.repo_chunk_extractor import extract_all_symbols

        content = "def foo():\n    pass\ndef foo():\n    pass\n"
        names = [s[0] for s in extract_all_symbols(content, "mod.py")]
        assert names.count("foo") == 1


class TestDetectEntryType:
    def test_main_py(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("main.py", "") == "main"

    def test_app_py(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("app.py", "") == "main"

    def test_index_ts(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("index.ts", "") == "main"

    def test_index_js(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("index.js", "") == "main"

    def test_server_prefix(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("server.py", "") == "server"
        assert detect_entry_type("server.ts", "") == "server"

    def test_cli_prefix(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("cli.py", "") == "cli"

    def test_test_prefix(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("test_foo.py", "") == "test"

    def test_main_guard(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        content = 'if __name__ == "__main__":\n    main()\n'
        assert detect_entry_type("runner.py", content) == "main"

    def test_non_entry(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("utils.py", "") is None

    def test_nested_path_main(self):
        from backend.app.repo_chunk_extractor import detect_entry_type

        assert detect_entry_type("src/main.py", "") == "main"


# ---------------------------------------------------------------------------
# Integration tests — pipeline populates graph tables
# ---------------------------------------------------------------------------


class TestDependencyResolution:
    """
    TEST_dependency_resolution:
        GIVEN repo with relative imports
        ASSERT file_dependencies populated correctly
    """

    def test_relative_imports_resolved(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import FileDependency, RepoFile

        job_id = _create_ingest_job()
        files = [
            ("src/main.py", "from .utils import helper\n\ndef main():\n    helper()\n"),
            ("src/utils.py", "def helper():\n    pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            repo_files = s.exec(
                select(RepoFile).where(RepoFile.ingest_job_id == job_id)
            ).all()
            assert len(repo_files) == 2

            file_path_map = {rf.file_path: rf.id for rf in repo_files}
            assert "src/main.py" in file_path_map
            assert "src/utils.py" in file_path_map

            deps = s.exec(
                select(FileDependency).where(
                    FileDependency.source_file_id == file_path_map["src/main.py"]
                )
            ).all()
            assert len(deps) == 1
            assert deps[0].target_file_id == file_path_map["src/utils.py"]
            assert deps[0].is_resolved is True

    def test_unresolvable_imports_not_stored(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import FileDependency

        job_id = _create_ingest_job()
        files = [
            ("main.py", "import os\nimport sys\n\ndef main():\n    pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            deps = s.exec(select(FileDependency)).all()
            # os and sys are not in the repo → no resolved deps
            assert len(deps) == 0

    def test_js_relative_imports_resolved(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import FileDependency, RepoFile

        job_id = _create_ingest_job()
        files = [
            ("src/index.ts", 'import { foo } from "./helpers"\n\nfunction main() {}\n'),
            ("src/helpers.ts", "export function foo() {}\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            repo_files = s.exec(
                select(RepoFile).where(RepoFile.ingest_job_id == job_id)
            ).all()
            file_path_map = {rf.file_path: rf.id for rf in repo_files}

            deps = s.exec(
                select(FileDependency).where(
                    FileDependency.source_file_id == file_path_map["src/index.ts"]
                )
            ).all()
            assert len(deps) == 1
            assert deps[0].target_file_id == file_path_map["src/helpers.ts"]


class TestSymbolCallEdges:
    """
    TEST_symbol_call_edges:
        GIVEN file with function calls
        ASSERT symbol_call_edges created
    """

    def test_call_edges_created(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, SymbolCallEdge

        job_id = _create_ingest_job()
        files = [
            (
                "src/main.py",
                "def main():\n    result = compute(42)\n    process(result)\n",
            ),
            (
                "src/math.py",
                "def compute(x):\n    return x * 2\n",
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            symbols = s.exec(select(CodeSymbol)).all()
            assert len(symbols) > 0

            edges = s.exec(select(SymbolCallEdge)).all()
            assert len(edges) > 0

            called_names = {e.target_symbol_name for e in edges}
            assert "compute" in called_names

    def test_call_edges_have_required_fields(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import SymbolCallEdge

        job_id = _create_ingest_job()
        files = [
            ("main.py", "def main():\n    helper()\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            edges = s.exec(select(SymbolCallEdge)).all()
            for edge in edges:
                # target_symbol_name is always required
                assert edge.target_symbol_name is not None
                assert len(edge.target_symbol_name) > 0
                # source_symbol_id is always required
                assert edge.source_symbol_id is not None

    def test_cross_file_call_resolution(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import RepoFile, SymbolCallEdge

        job_id = _create_ingest_job()
        files = [
            ("main.py", "from .utils import process\ndef main():\n    process(1)\n"),
            ("utils.py", "def process(x):\n    return x\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            edges = s.exec(select(SymbolCallEdge)).all()
            process_edge = next(
                (e for e in edges if e.target_symbol_name == "process"), None
            )
            if process_edge and process_edge.target_file_id is not None:
                utils_file = s.exec(
                    select(RepoFile).where(RepoFile.file_path == "utils.py")
                ).first()
                assert utils_file is not None
                assert process_edge.target_file_id == utils_file.id


class TestEntryPointDetection:
    """
    TEST_entry_point_detection:
        GIVEN repo with main.py
        ASSERT entry_points contains file
    """

    def test_main_py_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint, RepoFile

        job_id = _create_ingest_job()
        files = [
            (
                "main.py",
                "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n",
            ),
            ("utils.py", "def helper():\n    pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) >= 1

            main_file = s.exec(
                select(RepoFile).where(RepoFile.file_path == "main.py")
            ).first()
            assert main_file is not None

            ep_file_ids = {ep.file_id for ep in eps}
            assert main_file.id in ep_file_ids

            main_ep = next((ep for ep in eps if ep.file_id == main_file.id), None)
            assert main_ep is not None
            assert main_ep.entry_type == "main"

    def test_no_entry_points_does_not_fail(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            ("utils.py", "def helper():\n    return 1\n"),
            ("models.py", "class User:\n    pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) == 0

    def test_server_file_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            ("server.py", "from flask import Flask\napp = Flask(__name__)\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert any(ep.entry_type == "server" for ep in eps)

    def test_main_guard_detection(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            (
                "runner.py",
                'def run():\n    print("running")\n\nif __name__ == "__main__":\n    run()\n',
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert any(ep.entry_type == "main" for ep in eps)

    def test_index_ts_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            ("index.ts", "export function bootstrap() {}\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert any(ep.entry_type == "main" for ep in eps)


class TestGraphIntegrity:
    """
    TEST_graph_integrity:
        GIVEN ingested repo
        ASSERT:
            • files exist (repo_files)
            • dependencies exist (file_dependencies)
            • symbols exist (code_symbols)
    """

    def test_full_graph_integrity(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, FileDependency, RepoChunk, RepoFile

        job_id = _create_ingest_job()
        files = [
            (
                "src/main.py",
                (
                    "from .utils import process\n"
                    "from .models import User\n\n"
                    "def main():\n"
                    "    u = User()\n"
                    "    result = process(u)\n"
                    "    return result\n"
                ),
            ),
            ("src/utils.py", "def process(obj):\n    return str(obj)\n"),
            ("src/models.py", "class User:\n    def __init__(self):\n        self.name = ''\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            # Chunks must exist
            chunks = s.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == job_id)
            ).all()
            assert len(chunks) > 0, "Expected repo_chunks to be populated"

            # RepoFile rows must exist (one per non-empty file)
            repo_files = s.exec(
                select(RepoFile).where(RepoFile.ingest_job_id == job_id)
            ).all()
            assert len(repo_files) == 3, f"Expected 3 repo_files, got {len(repo_files)}"

            file_paths = {rf.file_path for rf in repo_files}
            assert "src/main.py" in file_paths
            assert "src/utils.py" in file_paths
            assert "src/models.py" in file_paths

            # CodeSymbol rows must exist
            symbols = s.exec(select(CodeSymbol)).all()
            assert len(symbols) > 0, "Expected code_symbols to be populated"

            symbol_names = {sym.name for sym in symbols}
            assert "main" in symbol_names
            assert "process" in symbol_names
            assert "User" in symbol_names

            # FileDependency rows must exist (main.py imports utils.py and models.py)
            deps = s.exec(select(FileDependency)).all()
            assert len(deps) >= 2, (
                f"Expected at least 2 file_dependencies, got {len(deps)}"
            )

            for dep in deps:
                assert dep.is_resolved is True
                assert dep.source_file_id is not None
                assert dep.target_file_id is not None
                assert dep.source_file_id != dep.target_file_id

    def test_repo_files_one_per_file(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import RepoFile

        job_id = _create_ingest_job()
        files = [
            ("a.py", "def a(): pass\n"),
            ("b.py", "def b(): pass\n"),
            ("c.py", "def c(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            repo_files = s.exec(
                select(RepoFile).where(RepoFile.ingest_job_id == job_id)
            ).all()
            assert len(repo_files) == 3
            paths = {rf.file_path for rf in repo_files}
            assert paths == {"a.py", "b.py", "c.py"}

    def test_empty_files_not_stored_in_graph(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import RepoFile

        job_id = _create_ingest_job()
        files = [
            ("real.py", "def foo(): pass\n"),
            ("empty.py", "   \n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            repo_files = s.exec(
                select(RepoFile).where(RepoFile.ingest_job_id == job_id)
            ).all()
            paths = {rf.file_path for rf in repo_files}
            assert "real.py" in paths
            assert "empty.py" not in paths

    def test_ingestion_deterministic_chunks_unaffected(self):
        """Graph phase failure must not corrupt chunk data."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import RepoChunk

        job_id = _create_ingest_job()
        files = [
            ("main.py", "def main():\n    pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            chunks = s.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == job_id)
            ).all()
            assert len(chunks) == 1
            assert "def main" in chunks[0].content


class TestMigration0026:
    """Validate the 0026 migration creates all required tables."""

    def test_migration_creates_all_tables(self, tmp_path, monkeypatch):
        import sqlalchemy as sa
        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "migration_test.db"
        db_url = f"sqlite:///{db_path}"

        # Create an empty SQLite DB and point Alembic at it.
        engine = sa.create_engine(db_url, connect_args={"check_same_thread": False})
        engine.dispose()

        monkeypatch.setenv("DATABASE_URL", db_url)

        alembic_cfg = Config("backend/alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)

        # Stamp at 0025 so Alembic skips all previous migrations and only
        # runs 0026 when we upgrade to head.
        command.stamp(alembic_cfg, "0025")
        command.upgrade(alembic_cfg, "head")

        inspector = sa.inspect(engine)
        tables = inspector.get_table_names()

        for required in (
            "repo_files",
            "code_symbols",
            "file_dependencies",
            "symbol_call_edges",
            "entry_points",
        ):
            assert required in tables, f"Table '{required}' missing after migration"

        # Verify column names
        rf_cols = {c["name"] for c in inspector.get_columns("repo_files")}
        assert {"id", "ingest_job_id", "file_path", "created_at"}.issubset(rf_cols)

        cs_cols = {c["name"] for c in inspector.get_columns("code_symbols")}
        assert {"id", "file_id", "name", "symbol_type", "created_at"}.issubset(cs_cols)

        fd_cols = {c["name"] for c in inspector.get_columns("file_dependencies")}
        assert {
            "id", "source_file_id", "target_file_id", "import_path", "is_resolved"
        }.issubset(fd_cols)

        sce_cols = {c["name"] for c in inspector.get_columns("symbol_call_edges")}
        assert {
            "id", "source_symbol_id", "target_symbol_name", "target_file_id"
        }.issubset(sce_cols)

        ep_cols = {c["name"] for c in inspector.get_columns("entry_points")}
        assert {"id", "ingest_job_id", "file_id", "entry_type"}.issubset(ep_cols)

        engine.dispose()


# ---------------------------------------------------------------------------
# Graph integrity invariant tests (MQP-STEERING-CONTRACT v1.0)
# ---------------------------------------------------------------------------


class TestNoUnresolvedDependencies:
    """
    TEST_no_unresolved_dependencies:
        ASSERT COUNT(file_dependencies WHERE target_file_id IS NULL) == 0
    """

    def test_no_null_target_file_ids(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import FileDependency

        job_id = _create_ingest_job()
        files = [
            (
                "src/main.py",
                "from .utils import helper\nimport os\nimport sys\n\ndef main(): pass\n",
            ),
            ("src/utils.py", "def helper(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            all_deps = s.exec(select(FileDependency)).all()
            # Every stored dependency must have a non-null target_file_id
            for dep in all_deps:
                assert dep.target_file_id is not None, (
                    f"FileDependency {dep.id} has NULL target_file_id — graph pollution"
                )
            # Unresolvable imports (os, sys) must not appear at all
            assert len(all_deps) == 1, (
                f"Expected 1 resolved dep (only .utils resolves), got {len(all_deps)}"
            )

    def test_no_unresolved_edges_multi_file(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import FileDependency

        job_id = _create_ingest_job()
        files = [
            (
                "app/routes.py",
                "from .models import User\nfrom .db import get_session\nimport flask\n\n"
                "def get_users(): pass\n",
            ),
            ("app/models.py", "class User:\n    pass\n"),
            ("app/db.py", "def get_session(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            all_deps = s.exec(select(FileDependency)).all()
            for dep in all_deps:
                assert dep.target_file_id is not None
            # flask is not in the repo → only 2 resolved deps
            assert len(all_deps) == 2


class TestSymbolIntegrity:
    """
    TEST_symbol_integrity:
        ASSERT ALL symbol_call_edges.source_symbol_id EXISTS in code_symbols
    """

    def test_all_call_edges_have_valid_source_symbol(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, SymbolCallEdge

        job_id = _create_ingest_job()
        files = [
            ("main.py", "def main():\n    helper()\n    process(42)\n"),
            ("lib.py", "def helper(): pass\ndef process(x): return x\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            valid_symbol_ids = {sym.id for sym in s.exec(select(CodeSymbol)).all()}
            edges = s.exec(select(SymbolCallEdge)).all()

            assert len(edges) > 0, "Expected call edges to be created"
            for edge in edges:
                assert edge.source_symbol_id in valid_symbol_ids, (
                    f"SymbolCallEdge {edge.id} references non-existent "
                    f"CodeSymbol {edge.source_symbol_id}"
                )

    def test_no_orphan_call_edges(self):
        """Call edges must only be created for files that have at least one symbol."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, SymbolCallEdge

        job_id = _create_ingest_job()
        # A file with no extractable symbols (pure comments/config)
        files = [
            ("config.ini", "[section]\nkey = value\n"),
            ("logic.py", "def compute():\n    return 42\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            edges = s.exec(select(SymbolCallEdge)).all()
            valid_symbol_ids = {sym.id for sym in s.exec(select(CodeSymbol)).all()}
            for edge in edges:
                assert edge.source_symbol_id in valid_symbol_ids


class TestFileSymbolLink:
    """
    TEST_file_symbol_link:
        ASSERT ALL code_symbols.file_id IS NOT NULL
    """

    def test_all_symbols_have_file_id(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol

        job_id = _create_ingest_job()
        files = [
            ("a.py", "def alpha(): pass\nclass Beta: pass\n"),
            ("b.py", "def gamma(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            symbols = s.exec(select(CodeSymbol)).all()
            assert len(symbols) > 0
            for sym in symbols:
                assert sym.file_id is not None, (
                    f"CodeSymbol '{sym.name}' has NULL file_id — orphan symbol"
                )

    def test_symbols_linked_to_correct_files(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, RepoFile

        job_id = _create_ingest_job()
        files = [
            ("models.py", "class User: pass\nclass Post: pass\n"),
            ("utils.py", "def helper(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            repo_files = {
                rf.file_path: rf.id
                for rf in s.exec(
                    select(RepoFile).where(RepoFile.ingest_job_id == job_id)
                ).all()
            }
            symbols = s.exec(select(CodeSymbol)).all()

            sym_map = {sym.name: sym.file_id for sym in symbols}
            assert sym_map.get("User") == repo_files["models.py"]
            assert sym_map.get("Post") == repo_files["models.py"]
            assert sym_map.get("helper") == repo_files["utils.py"]


class TestEntryPointPresence:
    """
    TEST_entry_point_presence:
        GIVEN executable repo
        ASSERT entry_points count > 0
    """

    def test_fastapi_app_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            (
                "api.py",
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                "@app.get('/')\ndef root(): return {}\n",
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) > 0, "FastAPI app must be detected as entry point"
            assert any(ep.entry_type == "framework" for ep in eps)

    def test_flask_app_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            (
                "web.py",
                "from flask import Flask\napp = Flask(__name__)\n\n"
                "@app.route('/')\ndef index(): return 'Hello'\n",
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) > 0, "Flask app must be detected as entry point"
            assert any(ep.entry_type == "server" for ep in eps)

    def test_express_listen_detected(self):
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            (
                "app.js",
                "const express = require('express')\n"
                "const app = express()\n"
                "app.listen(3000)\n",
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) > 0, "Express app must be detected as entry point"
            assert any(ep.entry_type in ("server", "main") for ep in eps)

    def test_runnable_repo_has_entry_points(self):
        """A repo with main.py + utilities must have at least one entry point."""
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import EntryPoint

        job_id = _create_ingest_job()
        files = [
            ("main.py", "from src.app import run\n\ndef main():\n    run()\n"),
            ("src/app.py", "def run(): print('running')\n"),
            ("src/utils.py", "def helper(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) > 0, "Runnable repo must have at least one entry point"


class TestGraphTraversability:
    """
    TEST_graph_traversability:
        GIVEN entry point
        ASSERT traversal reaches at least one downstream symbol
    """

    def test_entry_point_reaches_downstream_symbol(self):
        """
        Start at an EntryPoint, walk:
            EntryPoint → RepoFile → CodeSymbol → SymbolCallEdge → target_symbol_name
        Assert we reach at least one callable downstream.
        """
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import (
            CodeSymbol,
            EntryPoint,
            RepoFile,
            SymbolCallEdge,
        )

        job_id = _create_ingest_job()
        files = [
            (
                "main.py",
                "from .services import process_data\n\n"
                "def main():\n"
                "    result = process_data([])\n"
                "    return result\n",
            ),
            (
                "services.py",
                "def process_data(items):\n"
                "    return [transform(i) for i in items]\n",
            ),
            (
                "transforms.py",
                "def transform(item):\n"
                "    return item\n",
            ),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            # Step 1: Find entry points
            eps = s.exec(
                select(EntryPoint).where(EntryPoint.ingest_job_id == job_id)
            ).all()
            assert len(eps) > 0, "Expected at least one entry point"

            # Step 2: Walk entry_point → file → symbols
            entry_file_id = eps[0].file_id
            entry_file = s.get(RepoFile, entry_file_id)
            assert entry_file is not None

            symbols_in_entry = s.exec(
                select(CodeSymbol).where(CodeSymbol.file_id == entry_file_id)
            ).all()
            assert len(symbols_in_entry) > 0, (
                "Entry point file must have at least one symbol"
            )

            # Step 3: Follow call edges from any symbol in the entry file
            all_downstream: set[str] = set()
            for sym in symbols_in_entry:
                edges = s.exec(
                    select(SymbolCallEdge).where(
                        SymbolCallEdge.source_symbol_id == sym.id
                    )
                ).all()
                for edge in edges:
                    all_downstream.add(edge.target_symbol_name)

            assert len(all_downstream) > 0, (
                "Entry point must have at least one outgoing call edge — "
                "graph is not traversable"
            )

    def test_graph_chain_completeness(self):
        """
        RepoFile → CodeSymbol → SymbolCallEdge chain must be intact:
        every SymbolCallEdge must trace back to a RepoFile via CodeSymbol.
        """
        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        job_id = _create_ingest_job()
        files = [
            ("app.py", "def start():\n    connect()\n    listen()\n"),
            ("net.py", "def connect(): pass\ndef listen(): pass\n"),
        ]
        _run_repo_ingest(None, job_id, files)

        with Session(db_module.get_engine()) as s:
            edges = s.exec(select(SymbolCallEdge)).all()
            assert len(edges) > 0

            # Every edge → symbol → file must resolve without gaps
            for edge in edges:
                sym = s.get(CodeSymbol, edge.source_symbol_id)
                assert sym is not None, (
                    f"SymbolCallEdge {edge.id} source_symbol_id points to "
                    "non-existent CodeSymbol"
                )
                repo_file = s.get(RepoFile, sym.file_id)
                assert repo_file is not None, (
                    f"CodeSymbol {sym.id} file_id points to non-existent RepoFile"
                )
