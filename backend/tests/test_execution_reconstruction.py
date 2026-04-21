"""
backend/tests/test_execution_reconstruction.py
================================================
MQP-CONTRACT: GRAPH-EXECUTION-VALIDATION v1.0

Mandatory validation tests for reconstruct_execution():

  TEST_execution_reconstruction_basic
  TEST_execution_reconstruction_depth
  TEST_execution_reconstruction_integrity
  TEST_no_fake_edges
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
    db_path = tmp_path / "test_exec_recon.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


# ---------------------------------------------------------------------------
# Shared source fixtures
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

# Nested three levels: A → B → C
_A_PY = """\
from b import b_func


def a_func():
    return b_func()


if __name__ == "__main__":
    a_func()
"""

_B_PY = """\
from c import c_func


def b_func():
    return c_func()
"""

_C_PY = """\
def c_func():
    return 42
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo_job(session_factory, files: list[tuple[str, str]]) -> str:
    """Ingest files and return job_id."""
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob

    manifest = {
        "repo_url": "https://github.com/test/exec-recon",
        "owner": "test",
        "name": "exec-recon",
        "branch": "main",
        "files": [{"path": p, "content": c, "size": len(c)} for p, c in files],
    }
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/test/exec-recon@main",
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


def _get_entry_point_ids(session) -> list[str]:
    from sqlmodel import select

    from backend.app.models import EntryPoint

    return [str(ep.id) for ep in session.exec(select(EntryPoint))]


def _collect_symbols(chain_node: dict) -> list[str]:
    """Flatten all symbol names from a chain node and its calls recursively."""
    names = [chain_node["symbol"]]
    for call in chain_node.get("calls", []):
        names.extend(_collect_symbols(call))
    return names


def _collect_all_chain_symbols(result: dict) -> list[str]:
    names = []
    for node in result["execution_chain"]:
        names.extend(_collect_symbols(node))
    return names


# ---------------------------------------------------------------------------
# TEST_execution_reconstruction_basic
# ---------------------------------------------------------------------------


class TestExecutionReconstructionBasic:
    """
    GIVEN a repo with main.py calling utils.process_data()
    ASSERT the chain includes main (run) → process_data
    """

    def test_chain_includes_main_to_process(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
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
            assert eps, "No EntryPoint found — pipeline must detect if __name__=='__main__'"

            # Reconstruct from the first entry point
            result = reconstruct_execution(str(eps[0].id), sess)

        assert result["entry_file"] == "main.py"
        assert result["execution_chain"], "execution_chain must not be empty"

        all_symbols = _collect_all_chain_symbols(result)
        assert "run" in all_symbols, (
            f"'run' missing from chain; got {all_symbols}"
        )
        assert "process_data" in all_symbols, (
            f"'process_data' missing from chain; got {all_symbols}"
        )

    def test_entry_file_is_correct(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
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
            result = reconstruct_execution(str(eps[0].id), sess)

        assert result["entry_file"] == "main.py"
        assert result["entry_symbol"] is not None


# ---------------------------------------------------------------------------
# TEST_execution_reconstruction_depth
# ---------------------------------------------------------------------------


class TestExecutionReconstructionDepth:
    """
    GIVEN a repo with nested calls A → B → C
    ASSERT depth traversal is correct and no links are missing.
    """

    def test_nested_chain_depth(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("a.py", _A_PY),
                ("b.py", _B_PY),
                ("c.py", _C_PY),
            ],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            assert eps, "No EntryPoint found for a.py"

            result = reconstruct_execution(str(eps[0].id), sess)

        all_syms = _collect_all_chain_symbols(result)
        assert "a_func" in all_syms, f"a_func missing; got {all_syms}"
        assert "b_func" in all_syms, f"b_func missing from depth chain; got {all_syms}"
        assert "c_func" in all_syms, f"c_func missing from depth chain; got {all_syms}"

    def test_chain_is_ordered_depth_first(self):
        """b_func must appear as a call *under* a_func (not as a sibling)."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("a.py", _A_PY),
                ("b.py", _B_PY),
                ("c.py", _C_PY),
            ],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            result = reconstruct_execution(str(eps[0].id), sess)

        # a_func must be in the chain at top level or reachable from it
        chain = result["execution_chain"]
        a_node = next((n for n in chain if n["symbol"] == "a_func"), None)
        assert a_node is not None, "a_func must be in execution_chain"

        # b_func must appear in a_func's calls (DFS depth-first)
        a_callees = _collect_symbols(a_node)
        assert "b_func" in a_callees, (
            f"b_func must be a descendant of a_func; callees={a_callees}"
        )

    def test_no_missing_links_in_depth_traversal(self):
        """Every intermediate symbol must appear in the chain."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint

        _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("a.py", _A_PY),
                ("b.py", _B_PY),
                ("c.py", _C_PY),
            ],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(select(EntryPoint)))
            result = reconstruct_execution(str(eps[0].id), sess)

        all_syms = _collect_all_chain_symbols(result)
        # The full chain a_func → b_func → c_func must have no gaps
        for expected in ("a_func", "b_func", "c_func"):
            assert expected in all_syms, f"{expected} missing — link broken; chain={all_syms}"


# ---------------------------------------------------------------------------
# TEST_execution_reconstruction_integrity
# ---------------------------------------------------------------------------


class TestExecutionReconstructionIntegrity:
    """
    ASSERT every node in the chain:
      - has a valid RepoFile
      - has a valid CodeSymbol
    """

    def _collect_nodes(self, chain: list) -> list[dict]:
        nodes = []
        for node in chain:
            nodes.append(node)
            nodes.extend(self._collect_nodes(node.get("calls", [])))
        return nodes

    def test_every_node_has_valid_repo_file(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import EntryPoint, RepoFile

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
            result = reconstruct_execution(str(eps[0].id), sess)

            all_file_paths = {rf.path for rf in sess.exec(select(RepoFile))}

        nodes = self._collect_nodes(result["execution_chain"])
        assert nodes, "execution_chain must not be empty"
        for node in nodes:
            assert node["file"] in all_file_paths, (
                f"Node symbol={node['symbol']!r} has file={node['file']!r} "
                f"which is NOT a persisted RepoFile"
            )

    def test_every_node_has_valid_code_symbol(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import CodeSymbol, EntryPoint

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
            result = reconstruct_execution(str(eps[0].id), sess)

            all_symbol_names = {s.name for s in sess.exec(select(CodeSymbol))}

        nodes = self._collect_nodes(result["execution_chain"])
        assert nodes, "execution_chain must not be empty"
        for node in nodes:
            assert node["symbol"] in all_symbol_names, (
                f"Node symbol={node['symbol']!r} is NOT a persisted CodeSymbol"
            )


# ---------------------------------------------------------------------------
# TEST_no_fake_edges
# ---------------------------------------------------------------------------


class TestNoFakeEdges:
    """
    ASSERT no symbol appears in chain unless backed by a SymbolCallEdge.
    """

    def _collect_call_pairs(self, chain: list, parent: str | None = None) -> list[tuple]:
        """Return (caller_name, callee_name) for every edge in the tree."""
        pairs = []
        for node in chain:
            if parent is not None:
                pairs.append((parent, node["symbol"]))
            pairs.extend(self._collect_call_pairs(node.get("calls", []), node["symbol"]))
        return pairs

    def test_all_call_edges_backed_by_symbol_call_edge(self):
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import CodeSymbol, EntryPoint, SymbolCallEdge

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
            result = reconstruct_execution(str(eps[0].id), sess)

            # Build the set of (caller_name, callee_name) from the DB
            sym_by_id = {s.id: s.name for s in sess.exec(select(CodeSymbol))}
            db_edges: set[tuple[str, str]] = set()
            for edge in sess.exec(select(SymbolCallEdge)):
                if edge.target_symbol_id is not None:
                    caller = sym_by_id.get(edge.source_symbol_id)
                    callee = sym_by_id.get(edge.target_symbol_id)
                    if caller and callee:
                        db_edges.add((caller, callee))

        # Every parent→child relationship in the chain must be in db_edges
        # The chain root nodes have no parent (they come from the entry file directly)
        def check_subtree(node: dict) -> None:
            for call in node.get("calls", []):
                pair = (node["symbol"], call["symbol"])
                assert pair in db_edges, (
                    f"Fake edge detected: {pair[0]!r} → {pair[1]!r} "
                    f"has no backing SymbolCallEdge in the DB"
                )
                check_subtree(call)

        for node in result["execution_chain"]:
            check_subtree(node)

    def test_entry_file_symbols_not_inferred(self):
        """
        All symbols at the root of execution_chain must be persisted CodeSymbols
        in the entry file — not inferred from string matching.
        """
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.execution_reconstruction import reconstruct_execution
        from backend.app.models import CodeSymbol, EntryPoint, RepoFile

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
            result = reconstruct_execution(str(eps[0].id), sess)

            entry_file = sess.exec(
                select(RepoFile).where(RepoFile.path == result["entry_file"])
            ).first()
            assert entry_file is not None

            db_entry_symbols = {
                s.name
                for s in sess.exec(
                    select(CodeSymbol).where(CodeSymbol.file_id == entry_file.id)
                )
            }

        root_names = {n["symbol"] for n in result["execution_chain"]}
        for name in root_names:
            assert name in db_entry_symbols, (
                f"Root symbol {name!r} is not backed by a CodeSymbol in the entry file"
            )
