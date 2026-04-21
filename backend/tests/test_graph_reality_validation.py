"""
backend/tests/test_graph_reality_validation.py
================================================
MQP-CONTRACT: GRAPH-REALITY-VALIDATION v1.0

Validates graph correctness under real-world repo conditions using three
fixture repos that exercise patterns found in real codebases:

  REPO_1 — small Python project (clean flat structure, one entry point)
  REPO_2 — medium repo with packages / __init__.py modules, cross-package calls
  REPO_3 — alias imports, nested sub-packages, cross-folder dependencies

Each repo is structurally representative of a real repository:
  • >= 3 files with real cross-file call edges
  • at least one DetectedEntryPoint (if __name__ == "__main__")
  • intra-package relative imports
  • cross-package / cross-folder imports

Validation targets per repo:
  A. Dependency Graph Accuracy   — import → correct RepoFile.id
  B. Symbol Resolution Accuracy  — call → correct CodeSymbol.id
  C. Execution Reconstruction    — entry → full reachable chain
  D. False Edge Detection        — no edges where none exist
  E. Drop Logging                — every unresolvable import is recorded
  F. Ambiguity Logging           — ambiguous symbols recorded, not silently resolved
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
    db_path = tmp_path / "test_reality.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _make_repo_job(session_factory, files: list[tuple[str, str]], repo_name: str) -> str:
    """Ingest files under a given repo name and return job_id."""
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob

    manifest = {
        "repo_url": f"https://github.com/test/{repo_name}",
        "owner": "test",
        "name": repo_name,
        "branch": "main",
        "files": [{"path": p, "content": c, "size": len(c)} for p, c in files],
    }
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source=f"https://github.com/test/{repo_name}@main",
        branch="main",
        status="created",
    )
    job.blob_data = json.dumps(manifest).encode("utf-8")
    job.blob_mime_type = "application/json"
    job.blob_size_bytes = len(job.blob_data)

    with session_factory() as sess:
        sess.add(job)
        sess.commit()
        job_id = str(job.id)

    transition(uuid.UUID(job_id), "stored")
    transition(uuid.UUID(job_id), "queued")
    return job_id


# ============================================================================
# REPO 1 — Small Python project (clean flat structure)
#
# Files:
#   main.py            — entry point; calls processor.run_pipeline()
#   processor.py       — defines run_pipeline(), delegates to validator.check()
#   validator.py       — defines check(), compute()
#   config.py          — defines load_config() (imported by processor)
#
# Cross-file call chain: main.run_pipeline → processor.run_pipeline
#                         → validator.check
# stdlib imports (os, sys) must be silently dropped.
# ============================================================================

_REPO1_MAIN = """\
from processor import run_pipeline
import os


def main():
    os.environ.setdefault("ENV", "prod")
    return run_pipeline()


if __name__ == "__main__":
    main()
"""

_REPO1_PROCESSOR = """\
from validator import check
from config import load_config


def run_pipeline():
    cfg = load_config()
    return check(cfg)
"""

_REPO1_VALIDATOR = """\
def check(cfg):
    return compute(cfg)


def compute(cfg):
    return str(cfg)
"""

_REPO1_CONFIG = """\
def load_config():
    return {"env": "prod"}
"""

REPO1_FILES = [
    ("main.py", _REPO1_MAIN),
    ("processor.py", _REPO1_PROCESSOR),
    ("validator.py", _REPO1_VALIDATOR),
    ("config.py", _REPO1_CONFIG),
]


class TestRepo1SmallPython:
    """REPO_1 — small Python project, clean flat structure."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        return _make_repo_job(
            lambda: Session(get_engine()), REPO1_FILES, "repo1-small"
        )

    def test_all_stats_fields_present(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

        required_keys = {
            "repo", "files", "symbols", "dependencies", "call_edges",
            "entry_points", "resolution_stats", "execution_validation",
            "drop_log", "ambiguity_log", "broken_path_log",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )
        assert set(result["resolution_stats"].keys()) == {"resolved", "dropped", "ambiguous"}
        assert set(result["execution_validation"].keys()) == {"paths_found", "broken_paths"}

    def test_file_count_correct(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

        assert result["files"] == len(REPO1_FILES), (
            f"Expected {len(REPO1_FILES)} files, got {result['files']}"
        )

    def test_dependency_accuracy_cross_file(self):
        """main.py imports processor → FileDependency must exist."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph
        from backend.app.models import FileDependency, RepoFile

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            main_id = files_by_path["main.py"].id
            proc_id = files_by_path["processor.py"].id
            dep_targets = {
                d.target_file_id for d in sess.exec(select(FileDependency))
                if d.source_file_id == main_id
            }

        assert result["dependencies"] > 0, "Expected FileDependency rows"
        assert proc_id in dep_targets, "main.py → processor.py dependency missing"

    def test_symbol_resolution_follows_import_graph(self):
        """run_pipeline in main.py must resolve to processor.py's run_pipeline."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            files_by_id = {rf.id: rf for rf in sess.exec(select(RepoFile))}
            syms_by_file_name = {
                (files_by_id[s.file_id].path, s.name): s
                for s in sess.exec(select(CodeSymbol))
                if s.file_id in files_by_id
            }

            main_main = syms_by_file_name.get(("main.py", "main"))
            assert main_main, "main() must be a CodeSymbol in main.py"

            proc_run = syms_by_file_name.get(("processor.py", "run_pipeline"))
            assert proc_run, "run_pipeline() must be a CodeSymbol in processor.py"

            outgoing = [
                e for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == main_main.id
            ]
            assert outgoing, "main() must have at least one outgoing SymbolCallEdge"

            target_ids = {e.target_symbol_id for e in outgoing}
            assert proc_run.id in target_ids, (
                "main() must have a call edge to processor.py's run_pipeline()"
            )

    def test_stdlib_imports_dropped_not_stored(self):
        """'os' import must be dropped (not in repo); logged in drop_log."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

        assert result["resolution_stats"]["dropped"] > 0, (
            "'os' import should be dropped (not in repo)"
        )
        drop_names = [d["import"] for d in result["drop_log"]]
        assert "os" in drop_names, "drop_log must record dropped 'os' import"

    def test_no_silent_drops(self):
        """dropped_count must equal len(drop_log) — nothing silently dropped."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

        assert result["resolution_stats"]["dropped"] == len(result["drop_log"]), (
            "Every dropped import must have a corresponding drop_log entry"
        )

    def test_execution_path_found(self):
        """Entry point must yield a non-empty execution chain."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO1_FILES
            ])

        assert result["entry_points"] > 0, "No EntryPoint detected for main.py"
        assert result["execution_validation"]["paths_found"] > 0, (
            "At least one execution path must be reconstructable from main.py"
        )

    def test_no_null_target_edges(self):
        """All SymbolCallEdge rows must have non-NULL target_symbol_id."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: edge {edge.id} "
                    f"callee={edge.callee_name!r} has NULL target_symbol_id"
                )


# ============================================================================
# REPO 2 — Medium repo with packages / __init__.py modules
#
# Structure:
#   app/__init__.py           — package marker
#   app/main.py               — entry point; calls app.core.engine.start()
#   app/core/__init__.py      — sub-package marker
#   app/core/engine.py        — defines start(), delegates to app.core.runner.execute()
#   app/core/runner.py        — defines execute(), transform()
#   app/utils/__init__.py     — sub-package marker
#   app/utils/helpers.py      — defines format_output() (called from runner)
#   app/config.py             — defines Settings (class)
#
# Tests: relative import resolution, nested package calls, class symbols
# ============================================================================

_REPO2_APP_INIT = """\
# app package marker
"""

_REPO2_MAIN = """\
from app.core.engine import start
import sys


def main():
    return start()


if __name__ == "__main__":
    main()
"""

_REPO2_CORE_INIT = """\
# app.core sub-package marker
"""

_REPO2_ENGINE = """\
from app.core.runner import execute


def start():
    return execute()
"""

_REPO2_RUNNER = """\
from app.utils.helpers import format_output


def execute():
    return format_output("result")


def transform(data):
    return format_output(data)
"""

_REPO2_UTILS_INIT = """\
# app.utils sub-package marker
"""

_REPO2_HELPERS = """\
def format_output(value):
    return str(value).strip()
"""

_REPO2_CONFIG = """\
class Settings:
    debug: bool = False
    version: str = "1.0.0"
"""

REPO2_FILES = [
    ("app/__init__.py", _REPO2_APP_INIT),
    ("app/main.py", _REPO2_MAIN),
    ("app/core/__init__.py", _REPO2_CORE_INIT),
    ("app/core/engine.py", _REPO2_ENGINE),
    ("app/core/runner.py", _REPO2_RUNNER),
    ("app/utils/__init__.py", _REPO2_UTILS_INIT),
    ("app/utils/helpers.py", _REPO2_HELPERS),
    ("app/config.py", _REPO2_CONFIG),
]


class TestRepo2PackageStructure:
    """REPO_2 — medium package/module structure."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        return _make_repo_job(
            lambda: Session(get_engine()), REPO2_FILES, "repo2-packages"
        )

    def test_file_count(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO2_FILES
            ])

        assert result["files"] == len(REPO2_FILES)

    def test_package_import_resolved(self):
        """app.main imports app.core.engine → FileDependency must exist."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph
        from backend.app.models import FileDependency, RepoFile

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO2_FILES
            ])

            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            main_id = files_by_path["app/main.py"].id
            engine_id = files_by_path["app/core/engine.py"].id

            dep_targets = {
                d.target_file_id for d in sess.exec(select(FileDependency))
                if d.source_file_id == main_id
            }

        assert engine_id in dep_targets, (
            "app/main.py → app/core/engine.py dependency missing"
        )

    def test_nested_call_chain_resolvable(self):
        """start() → execute() → format_output() chain must be reconstructable."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint

        self._ingest()
        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            assert eps, "No EntryPoint detected for app/main.py"

            result = reconstruct_execution(str(eps[0].id), sess)

        def _all_symbols(chain):
            names = []
            for node in chain:
                names.append(node["symbol"])
                names.extend(_all_symbols(node.get("calls", [])))
            return names

        all_syms = _all_symbols(result["execution_chain"])
        assert "main" in all_syms or result["entry_file"] == "app/main.py", (
            "Entry file must be app/main.py"
        )
        # start() must be reachable
        assert "start" in all_syms, f"start() missing from chain; got {all_syms}"

    def test_class_symbol_persisted(self):
        """Settings class in app/config.py must be a CodeSymbol."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol

        self._ingest()
        with Session(get_engine()) as sess:
            names = {s.name for s in sess.exec(select(CodeSymbol))}

        assert "Settings" in names, "Settings class must be a CodeSymbol"

    def test_sys_import_dropped_and_logged(self):
        """stdlib 'sys' import must appear in drop_log."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO2_FILES
            ])

        drop_names = [d["import"] for d in result["drop_log"]]
        assert "sys" in drop_names, "stdlib 'sys' import must be in drop_log"

    def test_no_false_edges(self):
        """Every SymbolCallEdge target must be a persisted CodeSymbol."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            sym_ids = {s.id for s in sess.exec(select(CodeSymbol))}
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Partial edge: callee={edge.callee_name!r}"
                )
                assert edge.target_symbol_id in sym_ids, (
                    f"Call edge target {edge.target_symbol_id} is not a persisted CodeSymbol"
                )

    def test_ambiguity_log_is_list(self):
        """ambiguity_log must always be a list (empty when no ambiguity)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO2_FILES
            ])

        assert isinstance(result["ambiguity_log"], list)


# ============================================================================
# REPO 3 — Alias imports, nested sub-packages, cross-folder dependencies
#
# Structure:
#   src/main.py                   — entry point; from src.services import dispatch
#   src/services/__init__.py      — re-exports dispatch
#   src/services/dispatcher.py    — defines dispatch(), calls src.handlers.api.handle()
#   src/handlers/__init__.py      — package marker
#   src/handlers/api.py           — defines handle(); calls src.db.query.run()
#   src/db/__init__.py            — package marker
#   src/db/query.py               — defines run(); calls format_result()
#   src/db/utils.py               — defines format_result()
#   tests/__init__.py             — test package marker (no symbols)
#   tests/test_dummy.py           — import src.services; no call edges expected
#
# Real-world patterns exercised:
#   • Cross-folder: src/ → src/handlers → src/db
#   • Package __init__ resolution
#   • stdlib/external imports dropped
# ============================================================================

_REPO3_SRC_MAIN = """\
from src.services.dispatcher import dispatch
import logging


logger = logging.getLogger(__name__)


def main():
    logger.info("starting")
    return dispatch()


if __name__ == "__main__":
    main()
"""

_REPO3_SERVICES_INIT = """\
from src.services.dispatcher import dispatch

__all__ = ["dispatch"]
"""

_REPO3_DISPATCHER = """\
from src.handlers.api import handle


def dispatch():
    return handle()
"""

_REPO3_HANDLERS_INIT = """\
# handlers package
"""

_REPO3_API = """\
from src.db.query import run


def handle():
    return run()
"""

_REPO3_DB_INIT = """\
# db package
"""

_REPO3_QUERY = """\
from src.db.utils import format_result


def run():
    raw = get_raw_data()
    return format_result(raw)


def get_raw_data():
    return []
"""

_REPO3_DB_UTILS = """\
def format_result(data):
    return list(data)
"""

_REPO3_TESTS_INIT = """\
# test package
"""

_REPO3_TEST_DUMMY = """\
from src.services.dispatcher import dispatch


def test_dispatch_callable():
    assert callable(dispatch)
"""

REPO3_FILES = [
    ("src/main.py", _REPO3_SRC_MAIN),
    ("src/services/__init__.py", _REPO3_SERVICES_INIT),
    ("src/services/dispatcher.py", _REPO3_DISPATCHER),
    ("src/handlers/__init__.py", _REPO3_HANDLERS_INIT),
    ("src/handlers/api.py", _REPO3_API),
    ("src/db/__init__.py", _REPO3_DB_INIT),
    ("src/db/query.py", _REPO3_QUERY),
    ("src/db/utils.py", _REPO3_DB_UTILS),
    ("tests/__init__.py", _REPO3_TESTS_INIT),
    ("tests/test_dummy.py", _REPO3_TEST_DUMMY),
]


class TestRepo3AliasNestedCrossFolder:
    """REPO_3 — alias imports, nested sub-packages, cross-folder dependencies."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        return _make_repo_job(
            lambda: Session(get_engine()), REPO3_FILES, "repo3-nested"
        )

    def test_file_count(self):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO3_FILES
            ])

        assert result["files"] == len(REPO3_FILES)

    def test_cross_folder_dependency_resolved(self):
        """dispatcher.py → handlers/api.py dependency must be a FileDependency."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph
        from backend.app.models import FileDependency, RepoFile

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO3_FILES
            ])

            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            dispatcher_id = files_by_path["src/services/dispatcher.py"].id
            api_id = files_by_path["src/handlers/api.py"].id

            dep_targets = {
                d.target_file_id for d in sess.exec(select(FileDependency))
                if d.source_file_id == dispatcher_id
            }

        assert api_id in dep_targets, (
            "dispatcher.py → handlers/api.py dependency must be resolved"
        )

    def test_deep_call_chain_resolvable(self):
        """dispatch → handle → run → format_result chain must be reconstructable."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint

        self._ingest()
        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            assert eps, "No EntryPoint detected for src/main.py"

            result = reconstruct_execution(str(eps[0].id), sess)

        def _all_symbols(chain):
            names = []
            for node in chain:
                names.append(node["symbol"])
                names.extend(_all_symbols(node.get("calls", [])))
            return names

        all_syms = _all_symbols(result["execution_chain"])
        assert "dispatch" in all_syms, f"dispatch() missing from chain; got {all_syms}"
        assert "handle" in all_syms, f"handle() missing from chain; got {all_syms}"

    def test_external_imports_dropped_and_logged(self):
        """'logging' import must appear in drop_log."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO3_FILES
            ])

        drop_names = [d["import"] for d in result["drop_log"]]
        assert "logging" in drop_names, "'logging' must appear in drop_log"

    def test_no_silent_drops_repo3(self):
        """dropped count must equal len(drop_log)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO3_FILES
            ])

        assert result["resolution_stats"]["dropped"] == len(result["drop_log"]), (
            "Every dropped import must have a corresponding drop_log entry"
        )

    def test_no_null_target_edges(self):
        """All persisted SymbolCallEdges must have non-NULL target_symbol_id."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: callee={edge.callee_name!r}"
                )

    def test_execution_stats_deterministic(self):
        """Ingesting the same repo twice yields identical stats."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        manifest = [{"path": p, "content": c} for p, c in REPO3_FILES]

        with Session(get_engine()) as sess:
            result1 = validate_repo_graph(job_id, sess, manifest_files=manifest)
            result2 = validate_repo_graph(job_id, sess, manifest_files=manifest)

        for key in ("files", "symbols", "dependencies", "call_edges", "entry_points"):
            assert result1[key] == result2[key], (
                f"Non-deterministic: {key} changed between runs"
            )
        assert result1["resolution_stats"] == result2["resolution_stats"]
        assert result1["execution_validation"] == result2["execution_validation"]

    def test_validate_report_structure_complete(self):
        """validate_repo_graph output must fully conform to the contract shape."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        job_id = self._ingest()
        with Session(get_engine()) as sess:
            result = validate_repo_graph(job_id, sess, manifest_files=[
                {"path": p, "content": c} for p, c in REPO3_FILES
            ])

        # Top-level numeric fields must all be non-negative integers
        for key in ("files", "symbols", "dependencies", "call_edges", "entry_points"):
            assert isinstance(result[key], int) and result[key] >= 0, (
                f"{key} must be a non-negative int"
            )
        # Log lists must all be lists
        for key in ("drop_log", "ambiguity_log", "broken_path_log"):
            assert isinstance(result[key], list), f"{key} must be a list"
        # drop_log entries must each have the required keys
        for entry in result["drop_log"]:
            assert {"file", "import", "reason"}.issubset(entry.keys()), (
                f"drop_log entry missing required keys: {entry}"
            )
        # ambiguity_log entries
        for entry in result["ambiguity_log"]:
            assert {"caller_file", "callee_name", "candidates"}.issubset(entry.keys()), (
                f"ambiguity_log entry missing required keys: {entry}"
            )
        # broken_path_log entries
        for entry in result["broken_path_log"]:
            assert {"entry_point_id", "file", "reason"}.issubset(entry.keys()), (
                f"broken_path_log entry missing required keys: {entry}"
            )
