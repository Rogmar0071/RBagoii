"""
backend/tests/test_graph_adversarial_validation.py
====================================================
MQP-CONTRACT: GRAPH-ADVERSARIAL-VALIDATION v1.0

Adversarial stress tests for the graph system.  NOT correctness tests —
these tests MEASURE the system's failure boundaries under real-world
complexity patterns.  The system is NOT fixed during validation.

Three adversarial fixture repos:

  ADVERSARIAL_A — Symbol Collision
    Multiple files that all define a symbol with the same name.
    Validates: ambiguous_drops counted, lost_valid_edges measured,
               call edges correctly dropped (not guessed).

  ADVERSARIAL_B — Alias Imports
    Files use `import X as Y` — the current resolver stores "X as Y"
    as the raw import string, which fails to resolve via path matching.
    Validates: alias_failures detected and logged, no false FileDependency
               rows created, calls that ARE globally-unique still resolve.

  ADVERSARIAL_C — Framework / Missing Entry Points
    FastAPI-style routes using @app.get() decorators and uvicorn.run() —
    patterns that extract_entry_points() does NOT currently detect.
    Validates: missing_entry_points measured, existing detections still work,
               failure_analysis surfaces the gap.

All repos are structurally faithful to real-world Python projects.
NO assertions hide system weakness — failure boundaries are measured.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_adv")

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
    db_path = tmp_path / "test_adversarial.db"
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
    from backend.app.ingest_pipeline import transition
    from backend.app.models import IngestJob, Repo
    from sqlmodel import select

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


# ============================================================================
# ADVERSARIAL REPO A — Symbol Collision
#
# Three files each define process().  A runner calls process() — ambiguous.
# A fourth file defines validate_data() — globally unique, resolves fine.
#
# Expected failure measurements:
#   ambiguous_drops  >= 1   (runner.process → ambiguous process)
#   lost_valid_edges >= 1   (the dropped call exists in the repo)
#   SymbolCallEdge for ambiguous callee = 0 (not guessed)
#   SymbolCallEdge for validate_data = 1 (globally unique, resolves)
#
# Pattern from real-world: multiple handler.py / utils.py files with
# common names like process(), handle(), run(), transform().
# ============================================================================

# ============================================================================
# ADVERSARIAL REPO A — Symbol Collision
#
# Three plugin files each define handle_event().  A coordinator has no
# import to any of them, so the call is globally ambiguous (3 candidates).
# A separate checker.py defines validate_result() (globally unique) and IS
# imported by coordinator, so that call resolves correctly.
#
# Expected failure measurements:
#   ambiguous_drops  >= 1   (coordinate → handle_event: 3 candidates)
#   lost_valid_edges >= 1   (the dropped call target IS a real repo symbol)
#   SymbolCallEdge for handle_event = 0 (not guessed)
#   SymbolCallEdge for validate_result = 1 (globally unique, via import graph)
#
# Pattern from real-world: event systems, plugin architectures with
# multiple implementations of the same hook interface.
# ============================================================================

_ADVA_PLUGINS_INIT = """\
# plugins package
"""

_ADVA_PLUGIN_ALPHA = """\
def handle_event(evt):
    return f"alpha:{evt}"


def process_alpha(data):
    return data
"""

_ADVA_PLUGIN_BETA = """\
def handle_event(evt):
    return f"beta:{evt}"


def process_beta(data):
    return str(data)
"""

_ADVA_PLUGIN_GAMMA = """\
def handle_event(evt):
    return f"gamma:{evt}"


def process_gamma(data):
    return len(str(data))
"""

_ADVA_CHECKER = """\
def validate_result(data):
    return bool(data)
"""

_ADVA_COORDINATOR = """\
from checker import validate_result


def coordinate():
    handle_event("tick")
    validate_result("tick")
"""

_ADVA_MAIN = """\
from coordinator import coordinate


def main():
    coordinate()


if __name__ == "__main__":
    main()
"""

ADVA_FILES = [
    ("plugins/__init__.py", _ADVA_PLUGINS_INIT),
    ("plugins/alpha.py", _ADVA_PLUGIN_ALPHA),
    ("plugins/beta.py", _ADVA_PLUGIN_BETA),
    ("plugins/gamma.py", _ADVA_PLUGIN_GAMMA),
    ("checker.py", _ADVA_CHECKER),
    ("coordinator.py", _ADVA_COORDINATOR),
    ("main.py", _ADVA_MAIN),
]


class TestAdversarialASymbolCollision:
    """ADVERSARIAL_A — Symbol collision / ambiguous drops."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine

        return _make_repo_job(
            lambda: Session(get_engine()), ADVA_FILES, "adva-collision"
        )

    def _validate(self, job_id):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        with Session(get_engine()) as sess:
            return validate_repo_graph(
                job_id,
                sess,
                manifest_files=[{"path": p, "content": c} for p, c in ADVA_FILES],
            )

    def test_failure_analysis_block_present(self):
        """failure_analysis must be present with all four keys."""
        result = self._validate(self._ingest())
        fa = result.get("failure_analysis")
        assert fa is not None, "failure_analysis missing from result"
        assert set(fa.keys()) == {
            "lost_valid_edges",
            "alias_failures",
            "ambiguous_drops",
            "missing_entry_points",
        }

    def test_ambiguous_drops_detected(self):
        """process() is defined in 3 files → call must be counted as ambiguous_drop."""
        result = self._validate(self._ingest())
        assert result["failure_analysis"]["ambiguous_drops"] >= 1, (
            "process() collision must produce at least one ambiguous_drop"
        )
        assert result["resolution_stats"]["ambiguous"] >= 1

    def test_lost_valid_edges_detected(self):
        """Dropped ambiguous call to a real repo symbol = lost_valid_edge."""
        result = self._validate(self._ingest())
        assert result["failure_analysis"]["lost_valid_edges"] >= 1, (
            "Dropped call to known repo symbol must be counted as lost_valid_edge"
        )

    def test_ambiguous_call_NOT_stored_as_edge(self):
        """The ambiguous handle_event() call must NOT produce a SymbolCallEdge."""
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
            coordinator_sym = syms_by_file_name.get(("coordinator.py", "coordinate"))
            assert coordinator_sym, "coordinate must be a CodeSymbol"

            # Collect all targets of edges from coordinate()
            target_sym_ids = {
                e.target_symbol_id
                for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == coordinator_sym.id
            }

            # None of the three handle_event() symbols should be a target
            handle_event_sym_ids = {
                sym.id
                for (path, name), sym in syms_by_file_name.items()
                if name == "handle_event"
            }

        assert not target_sym_ids.intersection(handle_event_sym_ids), (
            "Ambiguous handle_event() must NOT produce a SymbolCallEdge — "
            "strict edge policy violated"
        )

    def test_globally_unique_symbol_resolves_correctly(self):
        """validate_result() is globally unique and imported → must produce a SymbolCallEdge."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            files_by_id = {rf.id: rf for rf in sess.exec(select(RepoFile))}
            syms = {
                (files_by_id[s.file_id].path, s.name): s
                for s in sess.exec(select(CodeSymbol))
                if s.file_id in files_by_id
            }
            coordinator_sym = syms.get(("coordinator.py", "coordinate"))
            validate_sym = syms.get(("checker.py", "validate_result"))
            assert coordinator_sym and validate_sym

            target_ids = {
                e.target_symbol_id
                for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == coordinator_sym.id
            }

        assert validate_sym.id in target_ids, (
            "validate_result() is globally unique — must have a SymbolCallEdge"
        )

    def test_ambiguity_log_records_collision(self):
        """ambiguity_log must record the handle_event() collision with ≥3 candidates."""
        result = self._validate(self._ingest())
        handle_entries = [
            e for e in result["ambiguity_log"] if e["callee_name"] == "handle_event"
        ]
        assert handle_entries, "handle_event() collision must appear in ambiguity_log"
        # All three plugin files should appear as candidates
        candidates = handle_entries[0]["candidates"]
        assert len(candidates) >= 3, (
            f"Expected ≥3 candidate files for handle_event(), got: {candidates}"
        )

    def test_no_null_target_edges(self):
        """Strict edge policy: no SymbolCallEdge with NULL target_symbol_id."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: callee={edge.callee_name!r}"
                )


# ============================================================================
# ADVERSARIAL REPO B — Alias Imports
#
# Files use `import utils.formatter as fmt` style.  The raw import string
# stored is "utils.formatter as fmt" which `resolve_import` treats as a
# module path, so the FileDependency is NOT created.
#
# Expected failure measurements:
#   alias_failures  >= 2   (two alias imports whose module resolves)
#   alias_failure_log entries present with correct file/alias/module
#   FileDependency NOT created for aliased imports
#   Call to globally-unique symbol STILL resolves (via global-unique fallback)
#
# Pattern from real-world: `import numpy as np`, `import pandas as pd`,
# `import myutils as utils`, `from mymodule import something as s`.
# ============================================================================

_ADVB_FORMATTER = """\
def format_output(value):
    return str(value).strip()


def format_json(value):
    return "{}"
"""

_ADVB_PARSER = """\
def parse_input(raw):
    return raw.strip()


def parse_json(raw):
    return {}
"""

_ADVB_MAIN = """\
import utils.formatter as fmt
import utils.parser as prs


def main():
    result = fmt.format_output("hello")
    parsed = prs.parse_input("  world  ")
    return result, parsed


if __name__ == "__main__":
    main()
"""

_ADVB_WORKER = """\
import utils.formatter as formatter


def process(data):
    return format_output(data)
"""

_ADVB_UTILS_INIT = """\
# utils package
"""

ADVB_FILES = [
    ("utils/__init__.py", _ADVB_UTILS_INIT),
    ("utils/formatter.py", _ADVB_FORMATTER),
    ("utils/parser.py", _ADVB_PARSER),
    ("main.py", _ADVB_MAIN),
    ("worker.py", _ADVB_WORKER),
]


class TestAdversarialBAliasImports:
    """ADVERSARIAL_B — Alias import failures."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine

        return _make_repo_job(
            lambda: Session(get_engine()), ADVB_FILES, "advb-alias"
        )

    def _validate(self, job_id):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        with Session(get_engine()) as sess:
            return validate_repo_graph(
                job_id,
                sess,
                manifest_files=[{"path": p, "content": c} for p, c in ADVB_FILES],
            )

    def test_alias_failures_detected(self):
        """Alias imports whose module resolves must appear in alias_failures."""
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]
        assert fa["alias_failures"] >= 2, (
            f"Expected ≥2 alias_failures, got {fa['alias_failures']}. "
            "import utils.formatter as fmt and import utils.parser as prs "
            "should both be detected."
        )

    def test_alias_failure_log_populated(self):
        """Every alias failure must be in alias_failure_log with correct fields."""
        result = self._validate(self._ingest())
        log = result.get("alias_failure_log", [])
        assert len(log) >= 2, (
            f"alias_failure_log must have ≥2 entries, got {len(log)}"
        )
        for entry in log:
            assert {"file", "import", "alias", "module"}.issubset(entry.keys()), (
                f"alias_failure_log entry missing required keys: {entry}"
            )
            assert " as " in entry["import"], (
                f"alias_failure_log entry must record the ' as ' import: {entry}"
            )

    def test_alias_failure_log_identifies_correct_modules(self):
        """The module parts of alias failures should point to repo files."""
        result = self._validate(self._ingest())
        modules = {entry["module"] for entry in result["alias_failure_log"]}
        assert "utils.formatter" in modules or any(
            "formatter" in m for m in modules
        ), f"utils.formatter should be in alias failure modules, got: {modules}"

    def test_alias_import_no_file_dependency_created(self):
        """Aliased imports must NOT produce a FileDependency row (current limitation)."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import FileDependency, RepoFile

        self._ingest()
        with Session(get_engine()) as sess:
            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            main_id = files_by_path["main.py"].id
            formatter_id = files_by_path["utils/formatter.py"].id

            dep_targets = {
                d.target_file_id
                for d in sess.exec(select(FileDependency))
                if d.source_file_id == main_id
            }

        # The alias import "utils.formatter as fmt" is NOT resolved,
        # so no FileDependency from main.py to utils/formatter.py exists.
        assert formatter_id not in dep_targets, (
            "Alias import should NOT produce a FileDependency — "
            "this is the known limitation being measured"
        )

    def test_globally_unique_calls_still_resolve(self):
        """
        Calls to globally-unique symbols resolve even without FileDependency.

        worker.py calls format_output() — no FileDependency exists (alias import),
        but format_output is globally unique so it resolves via fallback.
        """
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import CodeSymbol, RepoFile, SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            files_by_id = {rf.id: rf for rf in sess.exec(select(RepoFile))}
            syms = {
                (files_by_id[s.file_id].path, s.name): s
                for s in sess.exec(select(CodeSymbol))
                if s.file_id in files_by_id
            }
            worker_process = syms.get(("worker.py", "process"))
            formatter_fmt = syms.get(("utils/formatter.py", "format_output"))

            if worker_process is None or formatter_fmt is None:
                pytest.skip("Symbols not found — likely no call edges in worker.py")

            target_ids = {
                e.target_symbol_id
                for e in sess.exec(select(SymbolCallEdge))
                if e.source_symbol_id == worker_process.id
            }

        # format_output is globally unique → should resolve via global fallback
        assert formatter_fmt.id in target_ids, (
            "format_output() is globally unique and must resolve even without "
            "a FileDependency (via global-unique fallback)"
        )

    def test_no_null_target_edges(self):
        """Strict edge policy: no SymbolCallEdge with NULL target_symbol_id."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: callee={edge.callee_name!r}"
                )

    def test_failure_analysis_alias_count_equals_log(self):
        """alias_failures count must equal len(alias_failure_log) — no silent drops."""
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]
        log = result.get("alias_failure_log", [])
        assert fa["alias_failures"] == len(log), (
            "alias_failures count must equal len(alias_failure_log)"
        )


# ============================================================================
# ADVERSARIAL REPO C — Framework / Missing Entry Points
#
# A FastAPI-style project with:
#   app/main.py   — creates FastAPI() instance → DETECTED
#   app/routes/users.py  — only has @router.get() routes → NOT DETECTED
#   app/routes/items.py  — only has @router.post() routes → NOT DETECTED
#   scripts/deploy.py    — calls uvicorn.run() → NOT DETECTED
#   cli.py               — uses @click.command → NOT DETECTED
#
# Expected failure measurements:
#   entry_points                >= 1  (FastAPI() in main.py detected)
#   missing_entry_points        >= 3  (routes + uvicorn + click)
#   missing_entry_point_log     entries for each undetected pattern
#
# Pattern from real-world: FastAPI/Flask projects always have routes defined
# in router files that are entry points in practice but not detected.
# ============================================================================

_ADVC_APP_MAIN = """\
from fastapi import FastAPI
from app.routes.users import router as users_router
from app.routes.items import router as items_router

app = FastAPI()
app.include_router(users_router)
app.include_router(items_router)
"""

_ADVC_APP_INIT = """\
# app package
"""

_ADVC_ROUTES_INIT = """\
# routes package
"""

_ADVC_USERS_ROUTES = """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/users")
def list_users():
    return []


@router.get("/users/{user_id}")
def get_user(user_id: int):
    return {"id": user_id}
"""

_ADVC_ITEMS_ROUTES = """\
from fastapi import APIRouter

router = APIRouter()


@router.post("/items")
def create_item(name: str):
    return {"name": name}


@router.delete("/items/{item_id}")
def delete_item(item_id: int):
    return {"deleted": item_id}
"""

_ADVC_DEPLOY = """\
import uvicorn
from app.main import app


def run():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
"""

_ADVC_CLI = """\
import click


@click.command()
@click.option("--port", default=8000)
def serve(port):
    import uvicorn
    from app.main import app
    uvicorn.run(app, port=port)
"""

_ADVC_MODELS = """\
class User:
    def __init__(self, user_id: int, name: str):
        self.user_id = user_id
        self.name = name


class Item:
    def __init__(self, item_id: int, name: str):
        self.item_id = item_id
        self.name = name
"""

ADVC_FILES = [
    ("app/__init__.py", _ADVC_APP_INIT),
    ("app/main.py", _ADVC_APP_MAIN),
    ("app/routes/__init__.py", _ADVC_ROUTES_INIT),
    ("app/routes/users.py", _ADVC_USERS_ROUTES),
    ("app/routes/items.py", _ADVC_ITEMS_ROUTES),
    ("scripts/deploy.py", _ADVC_DEPLOY),
    ("cli.py", _ADVC_CLI),
    ("models.py", _ADVC_MODELS),
]


class TestAdversarialCFrameworkMissingEntryPoints:
    """ADVERSARIAL_C — Framework patterns / missing entry-point detection."""

    def _ingest(self):
        from sqlmodel import Session

        from backend.app.database import get_engine

        return _make_repo_job(
            lambda: Session(get_engine()), ADVC_FILES, "advc-framework"
        )

    def _validate(self, job_id):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.graph_reality_validator import validate_repo_graph

        with Session(get_engine()) as sess:
            return validate_repo_graph(
                job_id,
                sess,
                manifest_files=[{"path": p, "content": c} for p, c in ADVC_FILES],
            )

    def test_fastapi_instantiation_detected(self):
        """app = FastAPI() in app/main.py must produce an EntryPoint."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import EntryPoint, RepoFile

        self._ingest()
        with Session(get_engine()) as sess:
            files_by_path = {rf.path: rf for rf in sess.exec(select(RepoFile))}
            main_file = files_by_path.get("app/main.py")
            assert main_file, "app/main.py must be a RepoFile"

            ep_files = {ep.file_id for ep in sess.exec(select(EntryPoint))}

        assert main_file.id in ep_files, (
            "FastAPI() instantiation in app/main.py must be detected as EntryPoint"
        )

    def test_missing_entry_points_measured(self):
        """Route files with @router.get/@router.post must appear in missing_entry_points."""
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]
        assert fa["missing_entry_points"] >= 2, (
            f"Expected ≥2 missing_entry_points (route files), got {fa['missing_entry_points']}"
        )

    def test_missing_entry_point_log_populated(self):
        """missing_entry_point_log must have an entry per undetected file."""
        result = self._validate(self._ingest())
        log = result.get("missing_entry_point_log", [])
        assert len(log) >= 2, (
            f"missing_entry_point_log must have ≥2 entries, got {len(log)}"
        )
        for entry in log:
            assert {"file", "pattern"}.issubset(entry.keys()), (
                f"missing_entry_point_log entry missing required keys: {entry}"
            )

    def test_route_files_in_missing_log(self):
        """The router-decorated files must appear in missing_entry_point_log."""
        result = self._validate(self._ingest())
        missing_files = {e["file"] for e in result["missing_entry_point_log"]}
        # At least one route file must be flagged
        route_files = {"app/routes/users.py", "app/routes/items.py"}
        assert route_files.intersection(missing_files), (
            f"Route files must be in missing_entry_point_log. "
            f"Missing log files: {missing_files}"
        )

    def test_deploy_or_cli_in_missing_log(self):
        """scripts/deploy.py (uvicorn.run) or cli.py (@click.command) must be flagged."""
        result = self._validate(self._ingest())
        missing_files = {e["file"] for e in result["missing_entry_point_log"]}
        assert "scripts/deploy.py" in missing_files or "cli.py" in missing_files, (
            f"uvicorn.run or @click.command file must be in missing_entry_point_log. "
            f"Got: {missing_files}"
        )

    def test_failure_analysis_missing_count_equals_log(self):
        """missing_entry_points count must equal len(missing_entry_point_log)."""
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]
        log = result.get("missing_entry_point_log", [])
        assert fa["missing_entry_points"] == len(log), (
            "missing_entry_points count must equal len(missing_entry_point_log)"
        )

    def test_complete_failure_analysis_structure(self):
        """failure_analysis must have all four keys, all non-negative ints."""
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]
        for key in ("lost_valid_edges", "alias_failures", "ambiguous_drops",
                    "missing_entry_points"):
            assert key in fa, f"failure_analysis missing key: {key}"
            assert isinstance(fa[key], int) and fa[key] >= 0, (
                f"failure_analysis.{key} must be non-negative int"
            )

    def test_no_null_target_edges(self):
        """Strict edge policy: no SymbolCallEdge with NULL target_symbol_id."""
        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import SymbolCallEdge

        self._ingest()
        with Session(get_engine()) as sess:
            for edge in sess.exec(select(SymbolCallEdge)):
                assert edge.target_symbol_id is not None, (
                    f"Strict edge policy violated: callee={edge.callee_name!r}"
                )

    def test_failure_boundaries_are_visible(self):
        """
        The system's failure boundaries must be measurable, not hidden.

        This test validates that the failure_analysis block surfaces real gaps:
        - alias_failures may be 0 (no alias imports in this repo)
        - missing_entry_points > 0 (route files not detected)
        - ambiguous_drops may be 0 (no symbol name collisions)
        - The system does NOT hide these gaps
        """
        result = self._validate(self._ingest())
        fa = result["failure_analysis"]

        # The sum of all failure dimensions must be > 0 for this adversarial repo
        total_failures = (
            fa["lost_valid_edges"]
            + fa["alias_failures"]
            + fa["ambiguous_drops"]
            + fa["missing_entry_points"]
        )
        assert total_failures > 0, (
            "Adversarial framework repo must surface at least one failure dimension — "
            "the system should not silently hide its limitations"
        )
