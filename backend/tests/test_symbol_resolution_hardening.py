"""
backend/tests/test_symbol_resolution_hardening.py
===================================================
MQP-CONTRACT: SYMBOL-RESOLUTION-HARDENING v1.0

Mandatory validation tests:

  TEST_duplicate_symbol_names    — same-file resolution wins over cross-file
  TEST_import_based_resolution   — import-graph resolution wins over global
  TEST_ambiguous_symbol_dropped  — ambiguous cross-file callee produces no edge
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
    db_path = tmp_path / "test_sym_hardening.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_repo_job(session_factory, files: list[tuple[str, str]]) -> str:
    """Ingest files and return job_id."""
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob

    manifest = {
        "repo_url": "https://github.com/test/sym-hardening",
        "owner": "test",
        "name": "sym-hardening",
        "branch": "main",
        "files": [{"path": p, "content": c, "size": len(c)} for p, c in files],
    }
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/test/sym-hardening@main",
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


# ---------------------------------------------------------------------------
# TEST_duplicate_symbol_names
#
# GIVEN: file_a.py defines helper(); file_b.py also defines helper()
#        file_a.py has caller() that calls helper()
#        file_a does NOT import file_b
# ASSERT: the SymbolCallEdge from caller → helper resolves to FILE A's helper,
#         NOT file B's helper.
# ---------------------------------------------------------------------------

_FILE_A_PY = """\
def helper():
    return "A"


def caller():
    return helper()
"""

_FILE_B_PY = """\
def helper():
    return "B"
"""


class TestDuplicateSymbolNames:
    def test_same_file_helper_wins(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("file_a.py", _FILE_A_PY),
                ("file_b.py", _FILE_B_PY),
            ],
        )

        with Session(get_engine()) as sess:
            # Find the two helper symbols
            helpers = [s for s in sess.exec(select(CodeSymbol)) if s.name == "helper"]
            assert len(helpers) == 2, f"Expected 2 'helper' symbols, got {len(helpers)}"

            files_by_id = {rf.id: rf for rf in sess.exec(select(RepoFile))}
            a_helper = next(s for s in helpers if files_by_id[s.file_id].path == "file_a.py")
            b_helper = next(s for s in helpers if files_by_id[s.file_id].path == "file_b.py")

            # Find caller symbol (only in file_a.py)
            callers = [s for s in sess.exec(select(CodeSymbol)) if s.name == "caller"]
            assert callers, "caller() must be a CodeSymbol"
            caller_sym = callers[0]

            # Find the call edge from caller
            edges = [
                e for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == caller_sym.id
            ]
            assert edges, "caller() must have at least one outgoing SymbolCallEdge"

            for edge in edges:
                assert edge.target_symbol_id is not None, (
                    "Strict edge policy: target_symbol_id must never be NULL"
                )
                assert edge.target_symbol_id == a_helper.id, (
                    f"caller() in file_a.py must resolve helper() to FILE A's helper "
                    f"(id={a_helper.id}), got {edge.target_symbol_id} "
                    f"(file B helper id={b_helper.id})"
                )

    def test_no_cross_file_helper_edge_without_import(self):
        """
        With no import from file_a to file_b, no edge should point at b_helper.
        """
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("file_a.py", _FILE_A_PY),
                ("file_b.py", _FILE_B_PY),
            ],
        )

        with Session(get_engine()) as sess:
            helpers = [s for s in sess.exec(select(CodeSymbol)) if s.name == "helper"]
            files_by_id = {rf.id: rf for rf in sess.exec(select(RepoFile))}
            b_helper = next(s for s in helpers if files_by_id[s.file_id].path == "file_b.py")

            target_ids = {
                e.target_symbol_id
                for e in sess.exec(select(SymbolCallEdge))
                if e.target_symbol_id is not None
            }
            assert b_helper.id not in target_ids, (
                "file_b.py's helper must NOT appear as a call-edge target "
                "when file_a does not import file_b"
            )


# ---------------------------------------------------------------------------
# TEST_import_based_resolution
#
# GIVEN: main.py imports utils; utils.py defines helper(); main.py has
#        runner() that calls helper(); there is ALSO a standalone.py that
#        defines helper() but is NOT imported by main.py.
# ASSERT: runner → helper resolves to UTILS helper (via import graph),
#         NOT standalone's helper.
# ---------------------------------------------------------------------------

_MAIN_WITH_IMPORT_PY = """\
from utils import helper


def runner():
    return helper()
"""

_UTILS_WITH_HELPER_PY = """\
def helper():
    return "from utils"
"""

_STANDALONE_PY = """\
def helper():
    return "standalone"
"""


class TestImportBasedResolution:
    def test_call_resolves_to_imported_file_symbol(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_WITH_IMPORT_PY),
                ("utils.py", _UTILS_WITH_HELPER_PY),
                ("standalone.py", _STANDALONE_PY),
            ],
        )

        with Session(get_engine()) as sess:
            files_by_path = {
                rf.path: rf for rf in sess.exec(select(RepoFile))
            }
            syms_by_file: dict = {}
            for sym in sess.exec(select(CodeSymbol)):
                syms_by_file.setdefault(sym.file_id, []).append(sym)

            utils_file = files_by_path.get("utils.py")
            assert utils_file, "utils.py must be a RepoFile"

            utils_helper = next(
                (s for s in syms_by_file.get(utils_file.id, []) if s.name == "helper"),
                None,
            )
            assert utils_helper, "utils.py must have a 'helper' CodeSymbol"

            standalone_file = files_by_path.get("standalone.py")
            assert standalone_file, "standalone.py must be a RepoFile"

            standalone_helper = next(
                (s for s in syms_by_file.get(standalone_file.id, []) if s.name == "helper"),
                None,
            )
            assert standalone_helper, "standalone.py must have a 'helper' CodeSymbol"

            # Find runner's outgoing call edges
            runner_sym = next(
                (s for syms in syms_by_file.values() for s in syms if s.name == "runner"),
                None,
            )
            assert runner_sym, "runner() must be a CodeSymbol"

            edges = [
                e for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == runner_sym.id
            ]
            assert edges, "runner() must have at least one outgoing SymbolCallEdge"

            for edge in edges:
                assert edge.target_symbol_id is not None, (
                    "Strict edge policy: target_symbol_id must never be NULL"
                )
                assert edge.target_symbol_id == utils_helper.id, (
                    f"runner() must resolve helper() to utils.py's helper "
                    f"(id={utils_helper.id}) via import graph, "
                    f"not standalone.py's helper (id={standalone_helper.id})"
                )

    def test_standalone_helper_not_targeted(self):
        """standalone.py's helper must never be a call-edge target here."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_WITH_IMPORT_PY),
                ("utils.py", _UTILS_WITH_HELPER_PY),
                ("standalone.py", _STANDALONE_PY),
            ],
        )

        with Session(get_engine()) as sess:
            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            standalone_file = files_by_path["standalone.py"]
            standalone_helpers = [
                s for s in sess.exec(select(CodeSymbol))
                if s.file_id == standalone_file.id and s.name == "helper"
            ]
            assert standalone_helpers
            standalone_helper_id = standalone_helpers[0].id

            all_target_ids = {
                e.target_symbol_id
                for e in sess.exec(select(SymbolCallEdge))
                if e.target_symbol_id is not None
            }
            assert standalone_helper_id not in all_target_ids, (
                "standalone.py helper must not appear as a call-edge target "
                "when no file imports it"
            )


# ---------------------------------------------------------------------------
# TEST_ambiguous_symbol_dropped
#
# GIVEN: caller.py defines invoker() that calls helper(); there is lib_a.py
#        and lib_b.py each defining helper(); caller.py does NOT import
#        either lib_a or lib_b.
# ASSERT: NO SymbolCallEdge is created from invoker to any helper (ambiguous
#         → drop, per the resolution contract).
# ---------------------------------------------------------------------------

_CALLER_PY = """\
def invoker():
    return helper()
"""

_LIB_A_PY = """\
def helper():
    return "lib_a"
"""

_LIB_B_PY = """\
def helper():
    return "lib_b"
"""


class TestAmbiguousSymbolDropped:
    def test_no_edge_created_for_ambiguous_callee(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("caller.py", _CALLER_PY),
                ("lib_a.py", _LIB_A_PY),
                ("lib_b.py", _LIB_B_PY),
            ],
        )

        with Session(get_engine()) as sess:
            invoker = next(
                (s for s in sess.exec(select(CodeSymbol)) if s.name == "invoker"),
                None,
            )
            assert invoker, "invoker() must be a CodeSymbol"

            edges = [
                e for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == invoker.id
            ]
            assert len(edges) == 0, (
                f"Expected NO SymbolCallEdge from invoker() because helper() is ambiguous "
                f"(defined in both lib_a.py and lib_b.py with no import), "
                f"but got {len(edges)} edge(s)"
            )

    def test_all_existing_edges_have_non_null_target(self):
        """
        Strict edge policy: every persisted SymbolCallEdge must have a
        non-NULL target_symbol_id regardless of the ambiguity scenario.
        """
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("caller.py", _CALLER_PY),
                ("lib_a.py", _LIB_A_PY),
                ("lib_b.py", _LIB_B_PY),
            ],
        )

        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: SymbolCallEdge {edge.id} has "
                    f"NULL target_symbol_id (callee={edge.callee_name!r})"
                )
