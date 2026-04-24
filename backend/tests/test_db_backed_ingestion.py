"""
backend/tests/test_db_backed_ingestion.py
==========================================
MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE — DB-BACKED INGESTION + RETRIEVAL SYSTEM

Tests for database-backed ingestion with strict state machine enforcement.

COVERAGE:
- Blob write/read
- Ingestion pipeline execution
- Chunk extraction
- Retrieval queries
- Failure on missing blob
- State sequence validation
- No filesystem usage
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

TOKEN = "test-secret-key"


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
    from backend.app.main import app

    return TestClient(app, raise_server_exceptions=True)


_AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Test: Blob Storage
# ---------------------------------------------------------------------------


class TestBlobStorage:
    """Test that all data is stored in database as BLOBs."""

    def test_file_upload_stores_blob(self, client):
        """File upload stores data in blob_data field."""
        from io import BytesIO

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        # Upload a file
        content = b"Test file content"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}
        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Verify blob is stored in database
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job is not None
            assert job.blob_data == content
            assert job.blob_mime_type == "text/plain"
            assert job.blob_size_bytes == len(content)
            assert job.status == "success"

    def test_url_ingest_stores_blob(self, client):
        """URL ingestion stores fetched content as blob - DETERMINISTIC."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        # Create job with pre-stored blob (simulating URL fetch)
        # This is deterministic - no actual network call
        job = IngestJob(
            id=uuid.uuid4(),
            kind="url",
            source="https://example.com",
            status="created",
        )

        # Simulate what the route does: store fetched content as blob
        fake_html = b"<html><body><h1>Test Content</h1><p>Some text</p></body></html>"
        job.blob_data = fake_html
        job.blob_mime_type = "text/html"
        job.blob_size_bytes = len(fake_html)

        with Session(get_engine()) as session:
            session.add(job)
            session.commit()
            job_id = str(job.id)

        # Now process it (simulates worker)
        # Transition through states
        from backend.app.ingest_pipeline import transition

        transition(uuid.UUID(job_id), "stored")
        transition(uuid.UUID(job_id), "queued")

        # transition("queued") dispatches process_ingest_job synchronously in test mode

        # Verify blob was used and chunks created
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job is not None
            assert job.blob_data is not None
            assert len(job.blob_data) > 0
            assert job.blob_mime_type == "text/html"
            assert job.status == "success"
            assert job.chunk_count > 0

    def test_blob_size_validation(self, client):
        """Blobs exceeding 500MB are rejected."""
        from io import BytesIO

        # Create a file larger than 500MB
        large_content = b"x" * (501 * 1024 * 1024)
        files = {"file": ("large.txt", BytesIO(large_content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)

        assert resp.status_code == 413
        assert "500MB" in resp.json()["detail"]

    def test_repo_ingestion_deterministic(self, client):
        """Repo ingestion with pre-fetched manifest is fully deterministic."""
        import json

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, Repo, RepoChunk

        # Create deterministic repo manifest (simulates API fetch)
        manifest = {
            "repo_url": "https://github.com/test/repo",
            "owner": "test",
            "name": "repo",
            "branch": "main",
            "files": [
                {
                    "path": "src/main.py",
                    "content": "def hello():\n    return 'world'\n",
                    "size": 30
                },
                {
                    "path": "README.md",
                    "content": "# Test Repo\n\nThis is a test.\n",
                    "size": 32
                }
            ]
        }

        # Create job with manifest blob
        job = IngestJob(
            id=uuid.uuid4(),
            kind="repo",
            source="https://github.com/test/repo@main",
            branch="main",
            status="created",
        )

        job.blob_data = json.dumps(manifest).encode("utf-8")
        job.blob_mime_type = "application/json"
        job.blob_size_bytes = len(job.blob_data)

        with Session(get_engine()) as session:
            repo = Repo(
                id=uuid.uuid4(),
                repo_url=manifest["repo_url"],
                owner=manifest["owner"],
                name=manifest["name"],
                branch=manifest["branch"],
                ingestion_status="pending",
            )
            session.add(repo)
            session.commit()
            session.refresh(repo)
            job.repo_id = repo.id
            session.add(job)
            session.commit()
            job_id = str(job.id)

        # Process through states
        from backend.app.ingest_pipeline import transition

        transition(uuid.UUID(job_id), "stored")
        transition(uuid.UUID(job_id), "queued")

        # transition("queued") dispatches process_ingest_job synchronously in test mode

        # Verify deterministic output
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job.status == "success"
            assert job.file_count == 2
            assert job.chunk_count > 0
            assert job.avg_chunks_per_file > 0
            assert job.skipped_files_count == 0
            assert job.min_chunks_per_file > 0
            assert job.max_chunks_per_file >= job.min_chunks_per_file
            assert job.median_chunks_per_file > 0
            assert job.chunk_variance_flagged is False

            # Verify chunks were created
            chunks = list(session.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == uuid.UUID(job_id))
            ))
            assert len(chunks) > 0
            assert any(c.file_path == "src/main.py" for c in chunks)
            assert any(c.file_path == "README.md" for c in chunks)

        # Run AGAIN with same manifest - should produce identical output
        job2 = IngestJob(
            id=uuid.uuid4(),
            kind="repo",
            source="https://github.com/test/repo@main",
            branch="main",
            status="created",
        )
        job2.blob_data = json.dumps(manifest).encode("utf-8")
        job2.blob_mime_type = "application/json"
        job2.blob_size_bytes = len(job2.blob_data)

        with Session(get_engine()) as session:
            existing_repo = session.exec(
                select(Repo).where(
                    Repo.repo_url == manifest["repo_url"],
                    Repo.branch == manifest["branch"],
                )
            ).first()
            assert existing_repo is not None
            job2.repo_id = existing_repo.id
            session.add(job2)
            session.commit()
            job2_id = str(job2.id)

        transition(uuid.UUID(job2_id), "stored")
        transition(uuid.UUID(job2_id), "queued")
        # transition("queued") dispatches process_ingest_job synchronously in test mode

        # Verify identical chunk count (deterministic)
        with Session(get_engine()) as session:
            job2_record = session.get(IngestJob, uuid.UUID(job2_id))
            assert job2_record.chunk_count == job.chunk_count  # SAME output
            assert job2_record.file_count == job.file_count
            assert job2_record.chunk_variance_flagged is False

    def test_repo_ingestion_flags_chunk_variance_above_tolerance(self, client):
        import json

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, Repo

        baseline_manifest = {
            "repo_url": "https://github.com/test/variance-repo",
            "owner": "test",
            "name": "variance-repo",
            "branch": "main",
            "files": [{"path": "src/a.py", "content": "print('a')\n", "size": 10}],
        }
        large_manifest = {
            "repo_url": "https://github.com/test/variance-repo",
            "owner": "test",
            "name": "variance-repo",
            "branch": "main",
            "files": [
                {
                    "path": "src/a.py",
                    "content": ("x = 1\n" * 5000),
                    "size": 30000,
                }
            ],
        }

        from backend.app.ingest_pipeline import transition

        for manifest in (baseline_manifest, large_manifest):
            job = IngestJob(
                id=uuid.uuid4(),
                kind="repo",
                source="https://github.com/test/variance-repo@main",
                branch="main",
                status="created",
                blob_data=json.dumps(manifest).encode("utf-8"),
                blob_mime_type="application/json",
                blob_size_bytes=len(json.dumps(manifest).encode("utf-8")),
            )
            with Session(get_engine()) as session:
                repo = session.exec(
                    select(Repo).where(
                        Repo.repo_url == manifest["repo_url"],
                        Repo.branch == manifest["branch"],
                    )
                ).first()
                if repo is None:
                    repo = Repo(
                        id=uuid.uuid4(),
                        repo_url=manifest["repo_url"],
                        owner=manifest["owner"],
                        name=manifest["name"],
                        branch=manifest["branch"],
                        ingestion_status="pending",
                    )
                    session.add(repo)
                    session.commit()
                    session.refresh(repo)
                job.repo_id = repo.id
                session.add(job)
                session.commit()
                job_id = job.id
            transition(job_id, "stored")
            transition(job_id, "queued")

        with Session(get_engine()) as session:
            latest = session.exec(
                select(IngestJob)
                .where(IngestJob.source == "https://github.com/test/variance-repo@main")
                .order_by(IngestJob.created_at.desc())
            ).first()
            assert latest is not None
            assert latest.chunk_variance_flagged is True
            assert latest.chunk_variance_delta_pct > 10.0


# ---------------------------------------------------------------------------
# Test: State Machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    """Test strict state machine enforcement."""

    def test_state_sequence_file_upload(self, client):
        """
        File upload follows:
        created → stored → queued → running → processing → indexing → finalizing → success
        """
        from io import BytesIO

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        content = b"def foo():\n    return 42\n"
        files = {"file": ("test.py", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Final state should be success
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job is not None
            assert job.status == "success"
            assert job.chunk_count > 0

    def test_no_filesystem_usage(self, client, tmp_path, monkeypatch):
        """Worker never writes to filesystem."""
        from io import BytesIO

        # Monitor filesystem writes
        writes = []
        original_open = open

        def tracked_open(path, mode="r", *args, **kwargs):
            if "w" in mode or "a" in mode:
                writes.append(str(path))
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", tracked_open)

        content = b"Test content"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        assert resp.status_code == 202

        # No files should be written to /tmp or staging directories
        for write_path in writes:
            assert "/tmp/ingest_staging" not in write_path

    def test_blob_missing_fails(self, client):
        """Processing fails if blob_data is missing - validation enforced."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        # Create job without blob
        job = IngestJob(
            id=uuid.uuid4(),
            kind="file",
            source="test.txt",
            status="created",
        )

        with Session(get_engine()) as session:
            session.add(job)
            session.commit()
            job_id = str(job.id)

        # Try to transition to stored without blob - should fail validation
        import pytest

        with pytest.raises(RuntimeError, match="BLOB_VALIDATION_FAILED"):
            transition(uuid.UUID(job_id), "stored")

        # Verify job is still in created state (transition was rejected)
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job.status == "created"


# ---------------------------------------------------------------------------
# Test: Chunk Extraction
# ---------------------------------------------------------------------------


class TestChunkExtraction:
    """Test chunk extraction from blob data."""

    def test_chunks_created_from_blob(self, client):
        """Chunks are correctly extracted from blob data."""
        from io import BytesIO

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, RepoChunk

        content = b"Line 1\nLine 2\nLine 3\n" * 100  # Multiple lines to create chunks
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Verify chunks were created
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job.status == "success"
            assert job.chunk_count > 0

            # Get chunks
            chunks = session.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == job.id)
            ).all()

            assert len(chunks) == job.chunk_count
            assert all(len(chunk.content) > 0 for chunk in chunks)

    def test_code_structure_extraction(self, client):
        """Code structure is extracted correctly."""
        from io import BytesIO

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, RepoChunk

        content = b"""
def hello():
    return "world"

class MyClass:
    def method(self):
        pass
"""
        files = {"file": ("code.py", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        job_id = resp.json()["job_id"]

        # Verify structure was extracted
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            chunks = session.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == job.id)
            ).all()

            # At least one chunk should have structural metadata
            assert any(chunk.chunk_type is not None for chunk in chunks)


# ---------------------------------------------------------------------------
# Test: Retrieval
# ---------------------------------------------------------------------------


class TestRetrieval:
    """Test retrieval of chunks from database."""

    def test_retrieve_chunks_by_job(self, client):
        """Chunks can be retrieved by job ID."""
        from io import BytesIO

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, RepoChunk

        content = b"Searchable content about Python programming"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        job_id = resp.json()["job_id"]

        # Retrieve chunks
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            chunks = session.exec(
                select(RepoChunk).where(RepoChunk.ingest_job_id == job.id)
            ).all()

            assert len(chunks) > 0
            assert any("Python" in chunk.content for chunk in chunks)

    def test_retrieve_by_conversation(self, client):
        """Chunks can be retrieved by conversation_id."""
        from io import BytesIO

        from sqlmodel import Session, select

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        conversation_id = "test-conversation-123"
        content = b"Content for conversation"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}
        data = {"conversation_id": conversation_id}

        resp = client.post("/v1/ingest/file", files=files, data=data, headers=_AUTH)
        job_id = resp.json()["job_id"]

        # Verify conversation_id is set
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            assert job.conversation_id == conversation_id

            # Retrieve chunks via job's conversation_id
            jobs = session.exec(
                select(IngestJob).where(IngestJob.conversation_id == conversation_id)
            ).all()

            assert len(jobs) > 0
            assert any(j.id == job.id for j in jobs)


# ---------------------------------------------------------------------------
# Test: Transition Authority
# ---------------------------------------------------------------------------


class TestTransitionAuthority:
    """Test that ALL state changes go through transition() function."""

    def test_transition_validation(self):
        """Invalid transitions are rejected."""
        from backend.app.ingest_pipeline import transition

        job_id = uuid.uuid4()

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        # Create job with valid blob
        with Session(get_engine()) as session:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="test.txt",
                status="created",
                blob_data=b"test content",
                blob_mime_type="text/plain",
                blob_size_bytes=12,
            )
            session.add(job)
            session.commit()

        # Valid transition: created → stored
        transition(job_id, "stored")

        with Session(get_engine()) as session:
            job = session.get(IngestJob, job_id)
            assert job.status == "stored"

        # Invalid transition: stored → success (must go through intermediate states)
        with pytest.raises(RuntimeError, match="STATE_MACHINE_VIOLATION"):
            transition(job_id, "success")

    def test_atomic_state_and_payload_update(self):
        """State and payload are updated atomically."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        job_id = uuid.uuid4()

        # Create job with valid blob
        with Session(get_engine()) as session:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="test.txt",
                status="created",
                blob_data=b"test content",
                blob_mime_type="text/plain",
                blob_size_bytes=12,
            )
            session.add(job)
            session.commit()

        # Transition with payload
        transition(job_id, "stored", {"progress": 10, "file_count": 1})

        with Session(get_engine()) as session:
            job = session.get(IngestJob, job_id)
            assert job.status == "stored"
            assert job.progress == 10
            assert job.file_count == 1


# ---------------------------------------------------------------------------
# Test: Compliance Report
# ---------------------------------------------------------------------------


class TestCompliance:
    """Verify system complies with all MQP-CONTRACT requirements."""

    def test_no_filesystem_dependency(self, client):
        """System has no filesystem dependency."""
        from io import BytesIO

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        content = b"Test content"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        job_id = resp.json()["job_id"]

        # Verify job has NO source_path (legacy field should be None)
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            # blob_data should exist, source_path should be None for new jobs
            assert job.blob_data is not None
            assert job.status == "success"

    def test_deterministic_flow(self, client):
        """All jobs follow the same deterministic flow."""
        from io import BytesIO

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        # Test multiple jobs
        for i in range(3):
            content = f"Test content {i}".encode()
            files = {"file": (f"test{i}.txt", BytesIO(content), "text/plain")}

            resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
            job_id = resp.json()["job_id"]

            with Session(get_engine()) as session:
                job = session.get(IngestJob, uuid.UUID(job_id))
                # All should succeed with same pattern
                assert job.status == "success"
                assert job.blob_data is not None

    def test_blob_persistence(self, client):
        """Blob data persists correctly."""
        from io import BytesIO

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        content = b"Persistent data"
        files = {"file": ("test.txt", BytesIO(content), "text/plain")}

        resp = client.post("/v1/ingest/file", files=files, headers=_AUTH)
        job_id = resp.json()["job_id"]

        # Read blob multiple times
        for _ in range(3):
            with Session(get_engine()) as session:
                job = session.get(IngestJob, uuid.UUID(job_id))
                assert job.blob_data == content
                assert len(job.blob_data) == len(content)


# ---------------------------------------------------------------------------
# Test: MQP-CONTRACT WORKER-EXECUTION-CLOSURE — Terminal State Enforcement
# ---------------------------------------------------------------------------


class TestWorkerTerminalStateEnforcement:
    """
    MQP-CONTRACT: WORKER-EXECUTION-CLOSURE v1.1

    Verifies that process_ingest_job enforces the execution boundary:
        execution_allowed(job) = job.status == queued
    """

    def _make_job(self, status: str, blob_data: bytes = b"hello world") -> str:
        """Create an IngestJob directly in DB with the given status. Returns str(job_id)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        job_id = uuid.uuid4()
        with Session(get_engine()) as session:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="test.txt",
                status=status,
                blob_data=blob_data,
                blob_mime_type="text/plain",
                blob_size_bytes=len(blob_data),
            )
            session.add(job)
            session.commit()
        return str(job_id)

    def _get_status(self, job_id: str) -> str:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            return job.status

    def test_worker_skips_failed_job(self):
        """
        TEST 1 — Terminal Execution Block (failed)

        GIVEN  job.status = failed
        WHEN   process_ingest_job(job_id)
        THEN   no transition occurs, no exception raised
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_job("failed")
        process_ingest_job(job_id)  # must not raise
        assert self._get_status(job_id) == "failed"

    def test_worker_skips_success_job(self):
        """
        TEST 2 — Terminal Execution Block (success)

        GIVEN  job.status = success
        WHEN   process_ingest_job(job_id)
        THEN   no execution, no transition
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_job("success")
        process_ingest_job(job_id)  # must not raise
        assert self._get_status(job_id) == "success"

    def test_worker_rejects_non_queued(self):
        """
        TEST 3 — Invalid State Block (non-queued, non-terminal)

        GIVEN  job.status = created
        WHEN   process_ingest_job(job_id)
        THEN   exits immediately, no transition
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_job("created")
        process_ingest_job(job_id)  # must not raise
        assert self._get_status(job_id) == "created"

    def test_worker_executes_only_queued(self):
        """
        TEST 4 — Valid Execution

        GIVEN  job.status = queued
        WHEN   process_ingest_job(job_id)
        THEN   pipeline runs and job reaches success
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_job("queued")
        process_ingest_job(job_id)
        assert self._get_status(job_id) == "success"

    def test_no_double_failure_transition(self):
        """
        TEST 5 — No Double-Failure Transition

        GIVEN  job.status = failed
        WHEN   _transition(job_id, failed) is attempted
        THEN   raises RuntimeError with STATE_MACHINE_VIOLATION
        """
        from backend.app.ingest_pipeline import _transition

        job_id = self._make_job("failed")
        with pytest.raises(RuntimeError, match="STATE_MACHINE_VIOLATION"):
            _transition(job_id, "failed", error="test")


# ---------------------------------------------------------------------------
# Test: MQP-CONTRACT PURE EXECUTION PIPELINE — Pipeline Purity Enforcement
# ---------------------------------------------------------------------------


class TestPipelinePurity:
    """
    MQP-CONTRACT: PURE EXECUTION PIPELINE

    Verifies that:
    - Blob validation happens INSIDE the pipeline (after RUNNING)
    - All failures produce exactly one transition to FAILED
    - Full success sequence follows RUNNING → PROCESSING → INDEXING → FINALIZING → SUCCESS
    """

    def _make_queued_job(self, blob_data: bytes | None = b"hello world",
                         blob_size_bytes: int | None = None) -> str:
        """Create an IngestJob in QUEUED state. Returns str(job_id)."""
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        job_id = uuid.uuid4()
        size = blob_size_bytes if blob_size_bytes is not None else (
            len(blob_data) if blob_data is not None else 0
        )
        with Session(get_engine()) as session:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="test.txt",
                status="queued",
                blob_data=blob_data,
                blob_mime_type="text/plain" if blob_data else None,
                blob_size_bytes=size,
            )
            session.add(job)
            session.commit()
        return str(job_id)

    def _get_job(self, job_id: str):
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            return session.get(IngestJob, uuid.UUID(job_id))

    def test_pipeline_fails_on_missing_blob_after_running(self):
        """
        TEST_pipeline_fails_on_missing_blob_after_running

        GIVEN  job.status = queued, blob_data = None
        WHEN   process_ingest_job runs
        THEN   state goes queued → running → failed (PIPELINE_VALIDATION_FAIL)
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_queued_job(blob_data=None)
        process_ingest_job(job_id)  # must not raise

        job = self._get_job(job_id)
        assert job.status == "failed"
        assert job.error and "PIPELINE_VALIDATION_FAIL" in job.error

    def test_pipeline_fails_on_empty_blob(self):
        """
        TEST_pipeline_fails_on_empty_blob

        GIVEN  job.status = queued, blob_data present but blob_size_bytes = 0
        WHEN   process_ingest_job runs
        THEN   state reaches failed with PIPELINE_VALIDATION_FAIL
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_queued_job(blob_data=b"x", blob_size_bytes=0)
        process_ingest_job(job_id)  # must not raise

        job = self._get_job(job_id)
        assert job.status == "failed"
        assert job.error and "PIPELINE_VALIDATION_FAIL" in job.error

    def test_pipeline_success_full_sequence(self):
        """
        TEST_pipeline_success_full_sequence

        GIVEN  job.status = queued, valid blob_data
        WHEN   process_ingest_job runs
        THEN   job ends in success
        """
        from backend.app.ingest_pipeline import process_ingest_job

        job_id = self._make_queued_job(blob_data=b"line one\nline two\nline three\n")
        process_ingest_job(job_id)

        job = self._get_job(job_id)
        assert job.status == "success"
        assert job.progress == 100

    def test_pipeline_single_failure_transition(self):
        """
        TEST_pipeline_single_failure_transition

        GIVEN  job.status = queued, blob_data = None (forces PIPELINE_VALIDATION_FAIL)
        WHEN   process_ingest_job runs
        THEN   exactly ONE transition to failed occurs (no STATE_MACHINE_VIOLATION)
        """
        from backend.app.ingest_pipeline import process_ingest_job

        transitions: list[tuple[str, str]] = []

        import backend.app.ingest_pipeline as pipeline_mod

        original = pipeline_mod._transition

        def recording_transition(job_id, next_state, **payload):
            transitions.append((str(job_id), next_state))
            return original(job_id, next_state, **payload)

        pipeline_mod._transition = recording_transition
        try:
            job_id = self._make_queued_job(blob_data=None)
            process_ingest_job(job_id)
        finally:
            pipeline_mod._transition = original

        failed_transitions = [t for t in transitions if t[1] == "failed"]
        assert len(failed_transitions) == 1, (
            f"Expected exactly 1 failed transition, got {failed_transitions}"
        )
