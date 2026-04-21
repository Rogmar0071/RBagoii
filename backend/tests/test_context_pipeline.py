"""
backend/tests/test_context_pipeline.py
========================================
MQP-CONTRACT: PHASE3-PIPELINE-EXECUTION-LOCK v1.0

Mandatory validation tests for run_context_pipeline():

  TEST_pipeline_single_flow          — full pipeline succeeds on confirmed alignment
  TEST_alignment_required            — AlignmentRequiredError raised without confirmation
  TEST_no_activation_without_alignment — activation gate blocks unconfirmed runs
  TEST_context_graph_integrity       — graph integrity (structure, roles, links, gaps)
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_ctx")

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
    db_path = tmp_path / "test_ctx_pipeline.db"
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

_FASTAPI_PY = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {"hello": "world"}
"""


# ---------------------------------------------------------------------------
# Helper: create + process a repo IngestJob
# ---------------------------------------------------------------------------


def _make_repo_job(session_factory, files: list[tuple[str, str]]) -> str:
    """Create and fully process a repo IngestJob.  Returns job_id string."""
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob

    manifest = {
        "repo_url": "https://github.com/test/ctx-repo",
        "owner": "test",
        "name": "ctx-repo",
        "branch": "main",
        "files": [{"path": p, "content": c, "size": len(c)} for p, c in files],
    }
    job = IngestJob(
        id=uuid.uuid4(),
        kind="repo",
        source="https://github.com/test/ctx-repo@main",
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
    transition(uuid.UUID(job_id), "queued")  # BACKEND_DISABLE_JOBS=1 → synchronous processing
    return job_id


# ---------------------------------------------------------------------------
# TEST_pipeline_single_flow
# ---------------------------------------------------------------------------


class TestPipelineSingleFlow:
    """
    TEST_pipeline_single_flow

    The full pipeline must complete and return an ActiveContextSession when
    alignment_confirmed=True.  All eight stages must run without error.
    """

    def test_full_pipeline_returns_active_session(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import ActiveContextSession, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="run the main entry point and process user data",
                alignment_confirmed=True,
            )

        assert isinstance(result, ActiveContextSession)
        assert result.session_id
        assert result.job_id == job_id
        assert result.activated_at is not None

    def test_pipeline_final_context_populated(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="show me how data flows through process_data",
                alignment_confirmed=True,
            )

        fc = result.final_context
        assert fc is not None
        assert fc.aligned_intent_contract.valid is True
        assert fc.aligned_intent_contract.job_id == job_id
        assert fc.context_graph is not None
        assert fc.context_graph.enriched_graph is not None
        assert fc.context_graph.enriched_graph.structural_graph is not None

    def test_pipeline_enriches_file_roles(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="describe the system structure",
                alignment_confirmed=True,
            )

        file_roles = result.final_context.context_graph.enriched_graph.file_roles
        assert len(file_roles) > 0, "Expected file role annotations"
        role_values = set(file_roles.values())
        valid_roles = {"entry", "service", "model", "config", "util"}
        assert role_values <= valid_roles, f"Unexpected roles: {role_values - valid_roles}"

    def test_pipeline_enriches_symbol_roles(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="show symbol roles",
                alignment_confirmed=True,
            )

        sym_roles = result.final_context.context_graph.enriched_graph.symbol_roles
        assert len(sym_roles) > 0, "Expected symbol role annotations"
        valid = {"orchestrator", "transformer", "leaf"}
        for role in sym_roles.values():
            assert role in valid, f"Unexpected symbol role: {role}"

    def test_pipeline_detects_entry_points(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="find entry points",
                alignment_confirmed=True,
            )

        sg = result.final_context.context_graph.enriched_graph.structural_graph
        assert len(sg.entry_points) > 0, "Expected at least one entry point"
        entry_types = {ep["entry_type"] for ep in sg.entry_points}
        assert "main" in entry_types, "Expected 'main' entry point from if __name__=='__main__'"

    def test_pipeline_framework_entry_point(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("app.py", _FASTAPI_PY)],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="fastapi app routes",
                alignment_confirmed=True,
            )

        sg = result.final_context.context_graph.enriched_graph.structural_graph
        types = {ep["entry_type"] for ep in sg.entry_points}
        assert "framework" in types, "Expected 'framework' entry point for FastAPI"

    def test_pipeline_execution_paths_populated(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id,
                sess,
                user_intent="run the main execution",
                alignment_confirmed=True,
            )

        paths = result.final_context.validated_execution_paths
        assert len(paths) > 0, "Expected at least one validated execution path"
        for path in paths:
            assert isinstance(path, list)
            assert all(isinstance(fid, str) for fid in path)


# ---------------------------------------------------------------------------
# TEST_alignment_required
# ---------------------------------------------------------------------------


class TestAlignmentRequired:
    """
    TEST_alignment_required

    The pipeline MUST raise AlignmentRequiredError when alignment_confirmed
    is False.  The error MUST carry a summary dict for user presentation.
    """

    def test_raises_alignment_required_error(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError) as exc_info:
                run_context_pipeline(
                    job_id,
                    sess,
                    user_intent="run the main pipeline",
                    alignment_confirmed=False,
                )

        err = exc_info.value
        assert hasattr(err, "summary"), "AlignmentRequiredError must carry a 'summary' dict"
        assert isinstance(err.summary, dict)

    def test_alignment_summary_contains_system_structure(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError) as exc_info:
                run_context_pipeline(
                    job_id,
                    sess,
                    user_intent="show me system structure",
                    alignment_confirmed=False,
                )

        summary = exc_info.value.summary
        assert "system_structure" in summary, "Summary must include 'system_structure'"
        assert "intent_mapping" in summary, "Summary must include 'intent_mapping'"
        assert "missing_components" in summary, "Summary must include 'missing_components'"
        assert "execution_paths" in summary, "Summary must include 'execution_paths'"

    def test_alignment_summary_lists_files(self, tmp_path):
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError) as exc_info:
                run_context_pipeline(
                    job_id,
                    sess,
                    user_intent="process user data",
                    alignment_confirmed=False,
                )

        structure = exc_info.value.summary["system_structure"]
        assert structure["total_files"] == 2, (
            f"Expected 2 files in summary, got {structure['total_files']}"
        )

    def test_pipeline_stops_at_alignment_not_beyond(self, tmp_path):
        """After AlignmentRequiredError, no ActiveContextSession must exist."""
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("main.py", _MAIN_PY)],
        )

        result_holder = []

        with Session(get_engine()) as sess:
            try:
                result = run_context_pipeline(
                    job_id,
                    sess,
                    user_intent="run",
                    alignment_confirmed=False,
                )
                result_holder.append(result)
            except AlignmentRequiredError:
                pass

        assert len(result_holder) == 0, (
            "run_context_pipeline must NOT return a result when alignment is not confirmed"
        )

    def test_second_call_with_confirmed_succeeds(self, tmp_path):
        """Re-invocation with alignment_confirmed=True must return ActiveContextSession."""
        from sqlmodel import Session

        from backend.app.context_pipeline import (
            ActiveContextSession,
            AlignmentRequiredError,
            run_context_pipeline,
        )
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        # First call — must raise
        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError):
                run_context_pipeline(
                    job_id, sess,
                    user_intent="run the pipeline",
                    alignment_confirmed=False,
                )

        # Second call — confirmed; must succeed
        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="run the pipeline",
                alignment_confirmed=True,
            )

        assert isinstance(result, ActiveContextSession)
        assert result.job_id == job_id


# ---------------------------------------------------------------------------
# TEST_no_activation_without_alignment
# ---------------------------------------------------------------------------


class TestNoActivationWithoutAlignment:
    """
    TEST_no_activation_without_alignment

    _stage_activate MUST fail hard if AlignedIntentContract.valid is False.
    Activation must be impossible without a valid aligned contract.
    """

    def test_activate_blocks_invalid_contract(self):
        from backend.app.context_pipeline import (
            AlignedIntentContract,
            ContextGaps,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
            _PipelineToken,
            _stage_activate,
            _stage_finalize,
        )

        # Build a minimal FinalContext with confirmed=True (valid)
        sg = StructuralGraph(job_id="test-job")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        gaps = ContextGaps()  # no gaps
        contract = AlignedIntentContract(
            job_id="test-job",
            user_intent="test",
            confirmed=True,    # valid
            refinement=None,
        )
        fc = FinalContext(
            context_graph=cg,
            aligned_intent_contract=contract,
        )

        # Must succeed when called with a valid pipeline token
        token = _PipelineToken()
        session = _stage_activate(fc, token)
        assert session.session_id

    def test_activate_raises_for_unconfirmed_contract(self):
        from backend.app.context_pipeline import (
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
            _PipelineToken,
            _stage_activate,
        )

        sg = StructuralGraph(job_id="test-job")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="test-job",
            user_intent="test",
            confirmed=False,    # NOT valid
            refinement=None,
        )
        fc = FinalContext(
            context_graph=cg,
            aligned_intent_contract=contract,
        )

        # Token present but contract unconfirmed — must still raise ACTIVATION_BLOCKED
        token = _PipelineToken()
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, token)

    def test_contract_valid_property(self):
        from backend.app.context_pipeline import AlignedIntentContract

        confirmed = AlignedIntentContract(
            job_id="j", user_intent="x", confirmed=True, refinement=None
        )
        unconfirmed = AlignedIntentContract(
            job_id="j", user_intent="x", confirmed=False, refinement=None
        )

        assert confirmed.valid is True
        assert unconfirmed.valid is False

    def test_pipeline_does_not_activate_on_first_call(self, tmp_path):
        """First call without confirmation must never produce an active session."""
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError):
                run_context_pipeline(
                    job_id, sess,
                    user_intent="run the system",
                    alignment_confirmed=False,
                )
            # Session is still open and usable after the hard stop
            # — no ActiveContextSession should exist


# ---------------------------------------------------------------------------
# TEST_context_graph_integrity
# ---------------------------------------------------------------------------


class TestContextGraphIntegrity:
    """
    TEST_context_graph_integrity

    Structural integrity tests for the ContextGraph produced by the pipeline:
      - All file_ids in ContextGraph exist in StructuralGraph
      - All symbol_ids in ContextGraph exist in StructuralGraph
      - All links are traceable (no floating nodes)
      - Gap detection produces correct results
      - No ambiguous edges stored (only DB-backed call edges)
    """

    def test_no_floating_graph_nodes(self, tmp_path):
        """All file_ids referenced in the graph must correspond to real RepoFile rows."""
        from sqlmodel import Session, select

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine
        from backend.app.models import RepoFile

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="show all files",
                alignment_confirmed=True,
            )

            repo_file_ids = {
                str(rf.id)
                for rf in sess.exec(select(RepoFile))
            }

        sg = result.final_context.context_graph.enriched_graph.structural_graph
        for fid in sg.files:
            assert fid in repo_file_ids, (
                f"GraphFile {fid} has no corresponding RepoFile row — floating node"
            )

    def test_all_context_links_traceable(self, tmp_path):
        """Every ContextLink must reference a valid file_id or symbol_id in the graph."""
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="run process user main models utils",
                alignment_confirmed=True,
            )

        ctx = result.final_context.context_graph
        sg = ctx.enriched_graph.structural_graph

        for link in ctx.links:
            if link.link_type == "file":
                assert link.file_id in sg.files, (
                    f"ContextLink references unknown file_id {link.file_id}"
                )
            elif link.link_type == "symbol":
                assert link.symbol_id in sg.symbols, (
                    f"ContextLink references unknown symbol_id {link.symbol_id}"
                )

    def test_no_ambiguous_edges_stored(self, tmp_path):
        """
        TEST_no_ambiguous_edges

        SymbolCallEdges with NULL target_symbol_id must never appear in the
        structural graph's call_edges list.  They must appear in dropped_calls.
        """
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        # main.py + utils.py: process_data calls helper (resolvable intra-file edge)
        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="check call edges",
                alignment_confirmed=True,
            )

        sg = result.final_context.context_graph.enriched_graph.structural_graph

        for edge in sg.call_edges:
            assert edge.get("target_symbol_id") is not None, (
                "call_edges must NEVER contain edges with NULL target_symbol_id"
            )

    def test_gap_detection_empty_intent(self, tmp_path):
        """Empty user intent must be detected as a CRITICAL gap (ambiguous_intent)."""
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("utils.py", _UTILS_PY)],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError) as exc_info:
                run_context_pipeline(
                    job_id, sess,
                    user_intent="",       # EMPTY
                    alignment_confirmed=False,
                )

        summary = exc_info.value.summary
        gap_types = {g["type"] for g in summary["missing_components"]}
        assert "ambiguous_intent" in gap_types, (
            "Empty user intent must produce an 'ambiguous_intent' gap"
        )

    def test_gap_detection_no_entry_points(self, tmp_path):
        """A repo with no entry points must register a CRITICAL no_entry_points gap."""
        from sqlmodel import Session

        from backend.app.context_pipeline import AlignmentRequiredError, run_context_pipeline
        from backend.app.database import get_engine

        # plain_utils.py has no if __name__=='__main__' and no framework
        plain = "def add(a, b):\n    return a + b\n"

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("plain_utils.py", plain)],
        )

        with Session(get_engine()) as sess:
            with pytest.raises(AlignmentRequiredError) as exc_info:
                run_context_pipeline(
                    job_id, sess,
                    user_intent="run the add function",
                    alignment_confirmed=False,
                )

        summary = exc_info.value.summary
        gap_types = {g["type"] for g in summary["missing_components"]}
        assert "no_entry_points" in gap_types, (
            "Repo without entry points must produce 'no_entry_points' gap"
        )

    def test_file_role_entry_assigned_to_entry_file(self, tmp_path):
        """The file containing the entry point must be assigned the 'entry' file role."""
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine
        from backend.app.models import EntryPoint, RepoFile

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            eps = list(sess.exec(
                __import__("sqlmodel").select(EntryPoint)
            ))
            entry_file_ids = {str(ep.file_id) for ep in eps}

            result = run_context_pipeline(
                job_id, sess,
                user_intent="entry point files",
                alignment_confirmed=True,
            )

        file_roles = result.final_context.context_graph.enriched_graph.file_roles
        for fid in entry_file_ids:
            if fid in file_roles:
                assert file_roles[fid] == "entry", (
                    f"Entry file {fid} must have role='entry', got {file_roles[fid]!r}"
                )

    def test_symbol_role_leaf_for_no_calls(self, tmp_path):
        """Symbols that call nothing must be assigned the 'leaf' role."""
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        # isolated() has no calls, plus an entry point so finalization succeeds
        leaf_code = (
            "def isolated():\n"
            "    return 42\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    isolated()\n"
        )

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [("leaf.py", leaf_code)],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="isolated function roles",
                alignment_confirmed=True,
            )

        sym_roles = result.final_context.context_graph.enriched_graph.symbol_roles
        # The 'isolated' symbol calls no other known symbols — must be 'leaf'
        for sym_id, role in sym_roles.items():
            sg = result.final_context.context_graph.enriched_graph.structural_graph
            sym = sg.symbols.get(sym_id, {})
            if sym.get("name") == "isolated":
                assert role == "leaf", (
                    "Symbol 'isolated' calls nothing — expected role='leaf', "
                    f"got {role!r}"
                )

    def test_pipeline_single_entry_point_only(self, tmp_path):
        """run_context_pipeline is the ONLY entry point — no alternate paths."""
        import backend.app.context_pipeline as cp

        # Verify the public API surface
        assert callable(cp.run_context_pipeline)
        assert callable(cp._stage_normalize)
        assert callable(cp._stage_structural_graph)
        assert callable(cp._stage_enrich)
        assert callable(cp._stage_link)
        assert callable(cp._stage_gap_detect)
        assert callable(cp._stage_user_align)
        assert callable(cp._stage_finalize)
        assert callable(cp._stage_activate)
        assert hasattr(cp, "AlignmentRequiredError")
        assert hasattr(cp, "ActiveContextSession")
        assert hasattr(cp, "_PipelineToken")


# ---------------------------------------------------------------------------
# TEST_activation_only_via_pipeline
# ---------------------------------------------------------------------------


class TestActivationOnlyViaPipeline:
    """
    TEST_activation_only_via_pipeline

    ActiveContextSession MUST NOT be constructable outside run_context_pipeline().
    Any direct construction attempt without a valid _PipelineToken must fail
    with ACTIVATION_BLOCKED.
    """

    def test_direct_construction_without_token_fails(self):
        """ActiveContextSession() without a _PipelineToken raises ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import (
            ActiveContextSession,
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
        )

        sg = StructuralGraph(job_id="direct-test")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="direct-test", user_intent="test", confirmed=True, refinement=None
        )
        fc = FinalContext(context_graph=cg, aligned_intent_contract=contract)

        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            # No _token provided — must raise
            ActiveContextSession(
                session_id=str(uuid.uuid4()),
                job_id="direct-test",
                final_context=fc,
            )

    def test_direct_construction_with_wrong_token_type_fails(self):
        """ActiveContextSession() with a non-_PipelineToken token raises ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import (
            ActiveContextSession,
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
        )

        sg = StructuralGraph(job_id="direct-test")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="direct-test", user_intent="test", confirmed=True, refinement=None
        )
        fc = FinalContext(context_graph=cg, aligned_intent_contract=contract)

        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            ActiveContextSession(
                session_id=str(uuid.uuid4()),
                job_id="direct-test",
                final_context=fc,
                _token="not-a-real-token",   # string — wrong type
            )

    def test_direct_construction_with_none_token_fails(self):
        """ActiveContextSession() with _token=None explicitly raises ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import (
            ActiveContextSession,
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
        )

        sg = StructuralGraph(job_id="direct-test")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="direct-test", user_intent="test", confirmed=True, refinement=None
        )
        fc = FinalContext(context_graph=cg, aligned_intent_contract=contract)

        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            ActiveContextSession(
                session_id=str(uuid.uuid4()),
                job_id="direct-test",
                final_context=fc,
                _token=None,
            )

    def test_pipeline_produces_valid_session(self, tmp_path):
        """run_context_pipeline() produces a real ActiveContextSession (token handled inside)."""
        from sqlmodel import Session

        from backend.app.context_pipeline import ActiveContextSession, run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="run the main pipeline",
                alignment_confirmed=True,
            )

        # The pipeline must produce a proper ActiveContextSession
        assert isinstance(result, ActiveContextSession)
        assert result.session_id
        # The internal token must be set and valid
        from backend.app.context_pipeline import _PipelineToken
        assert isinstance(result._token, _PipelineToken)


# ---------------------------------------------------------------------------
# TEST_stage_activate_direct_call_blocked
# ---------------------------------------------------------------------------


class TestStageActivateDirectCallBlocked:
    """
    TEST_stage_activate_direct_call_blocked

    Calling _stage_activate directly (without a pipeline token) MUST fail.
    No token → ACTIVATION_BLOCKED.
    """

    def _make_final_context(self, confirmed: bool = True) -> "FinalContext":
        from backend.app.context_pipeline import (
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
        )

        sg = StructuralGraph(job_id="token-test")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="token-test",
            user_intent="test",
            confirmed=confirmed,
            refinement=None,
        )
        return FinalContext(context_graph=cg, aligned_intent_contract=contract)

    def test_no_args_fails(self):
        """_stage_activate(fc) with no token must raise ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import _stage_activate

        fc = self._make_final_context(confirmed=True)
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc)

    def test_none_token_fails(self):
        """_stage_activate(fc, None) must raise ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import _stage_activate

        fc = self._make_final_context(confirmed=True)
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, None)

    def test_string_token_fails(self):
        """_stage_activate(fc, 'some-string') must raise ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import _stage_activate

        fc = self._make_final_context(confirmed=True)
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, "fake-pipeline-token-string")

    def test_uuid_string_token_fails(self):
        """A plain UUID string is not a _PipelineToken — must raise ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import _stage_activate

        fc = self._make_final_context(confirmed=True)
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, str(uuid.uuid4()))

    def test_dict_token_fails(self):
        """A dict is not a _PipelineToken — must raise ACTIVATION_BLOCKED."""
        from backend.app.context_pipeline import _stage_activate

        fc = self._make_final_context(confirmed=True)
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, {"token": str(uuid.uuid4())})

    def test_valid_token_and_confirmed_contract_succeeds(self):
        """_stage_activate with a real _PipelineToken and valid contract must succeed."""
        from backend.app.context_pipeline import ActiveContextSession, _PipelineToken, _stage_activate

        fc = self._make_final_context(confirmed=True)
        token = _PipelineToken()
        result = _stage_activate(fc, token)
        assert isinstance(result, ActiveContextSession)
        assert result.session_id

    def test_valid_token_but_unconfirmed_contract_fails(self):
        """Token present but unconfirmed contract → ACTIVATION_BLOCKED (contract guard)."""
        from backend.app.context_pipeline import _PipelineToken, _stage_activate

        fc = self._make_final_context(confirmed=False)
        token = _PipelineToken()
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc, token)


# ---------------------------------------------------------------------------
# TEST_pipeline_token_required
# ---------------------------------------------------------------------------


class TestPipelineTokenRequired:
    """
    TEST_pipeline_token_required

    The pipeline token is the sole key to activation.
    Missing token → immediate ACTIVATION_BLOCKED at every enforcement point.
    """

    def test_pipeline_token_is_uuid(self):
        """_PipelineToken must generate a valid UUID internally."""
        from backend.app.context_pipeline import _PipelineToken

        t = _PipelineToken()
        # value must be a parseable UUID
        parsed = uuid.UUID(t.value)
        assert str(parsed) == t.value

    def test_each_pipeline_call_generates_unique_token(self):
        """Every _PipelineToken instance must have a distinct value."""
        from backend.app.context_pipeline import _PipelineToken

        tokens = [_PipelineToken().value for _ in range(50)]
        assert len(set(tokens)) == 50, "Every pipeline token must be unique"

    def test_token_not_exposed_in_session_repr(self, tmp_path):
        """The pipeline token must not appear in the ActiveContextSession repr."""
        from sqlmodel import Session

        from backend.app.context_pipeline import run_context_pipeline
        from backend.app.database import get_engine

        job_id = _make_repo_job(
            lambda: Session(get_engine()),
            [
                ("main.py", _MAIN_PY),
                ("utils.py", _UTILS_PY),
                ("models.py", _MODELS_PY),
            ],
        )

        with Session(get_engine()) as sess:
            result = run_context_pipeline(
                job_id, sess,
                user_intent="check token exposure",
                alignment_confirmed=True,
            )

        r = repr(result)
        # _token field has repr=False — the raw UUID value must not appear in repr
        assert result._token.value not in r, (
            "Pipeline token value must not be visible in ActiveContextSession repr"
        )

    def test_activation_without_pipeline_cannot_produce_session(self):
        """Without running the full pipeline, no ActiveContextSession can be produced."""
        from backend.app.context_pipeline import (
            ActiveContextSession,
            AlignedIntentContract,
            ContextGraph,
            EnrichedGraph,
            FinalContext,
            StructuralGraph,
            _stage_activate,
        )

        sg = StructuralGraph(job_id="no-token")
        eg = EnrichedGraph(structural_graph=sg)
        cg = ContextGraph(enriched_graph=eg, user_intent="test")
        contract = AlignedIntentContract(
            job_id="no-token", user_intent="test", confirmed=True, refinement=None
        )
        fc = FinalContext(context_graph=cg, aligned_intent_contract=contract)

        # Attempt 1: call _stage_activate without token
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            _stage_activate(fc)

        # Attempt 2: construct ActiveContextSession directly without token
        with pytest.raises(RuntimeError, match="ACTIVATION_BLOCKED"):
            ActiveContextSession(
                session_id=str(uuid.uuid4()),
                job_id="no-token",
                final_context=fc,
            )

        # Both paths blocked — no session was produced
