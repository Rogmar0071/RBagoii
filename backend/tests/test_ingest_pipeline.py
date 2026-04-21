"""
backend/tests/test_ingest_pipeline.py
======================================
Tests for the unified ingestion pipeline.

Covers:
- Text extraction (plain text, HTML, CSV, ZIP, missing PDF/DOCX libs)
- Chunking with overlap
- IngestJob model creation and DB-backed state transitions
- API endpoints: POST /v1/ingest/{file,url,repo}, GET, DELETE
- Deduplication for repo jobs
- Invariant enforcement: blob presence before enqueue, worker purity
- Static structural scans: no filesystem, no network in worker
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile

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


def mock_github_fetch(files=None):
    """
    Create a context manager that mocks GitHub fetch functions for testing.
    Usage: with mock_github_fetch([("file.py", "content")]):
    """
    if files is None:
        files = [("README.md", "# Hello")]

    def mock_fetch_tree(owner, repo, branch, token):
        return [{"path": path} for path, _ in files]

    def mock_fetch_file(owner, repo, branch, path, client):
        for file_path, content in files:
            if file_path == path:
                return content.encode("utf-8")
        return None

    from unittest.mock import patch
    return patch.multiple(
        "backend.app.ingest_pipeline",
        _fetch_github_tree=mock_fetch_tree,
        _fetch_raw_file=mock_fetch_file,
    )


_AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Unit: extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_plain_text(self):
        from backend.app.ingest_pipeline import extract_text

        data = b"Hello, world!"
        result = extract_text(data, "text/plain", "test.txt")
        assert result == "Hello, world!"

    def test_code_file_by_extension(self):
        from backend.app.ingest_pipeline import extract_text

        data = b"def foo():\n    return 1\n"
        result = extract_text(data, "application/octet-stream", "script.py")
        assert "def foo" in result

    def test_html_strips_tags(self):
        from backend.app.ingest_pipeline import extract_text

        html = b"<html><head><title>T</title></head><body><p>Hello <b>world</b></p></body></html>"
        result = extract_text(html, "text/html", "page.html")
        assert result is not None
        assert "Hello" in result
        assert "world" in result
        assert "<p>" not in result

    def test_html_skips_script_content(self):
        from backend.app.ingest_pipeline import extract_text

        html = b"<html><body><script>var x=1;</script><p>Visible</p></body></html>"
        result = extract_text(html, "text/html", "page.html")
        assert "Visible" in result
        assert "var x" not in result

    def test_csv_extraction(self):
        from backend.app.ingest_pipeline import extract_text

        csv_data = b"name,age\nAlice,30\nBob,25\n"
        result = extract_text(csv_data, "text/csv", "data.csv")
        assert result is not None
        assert "Alice" in result
        assert "Bob" in result

    def test_unknown_binary_returns_none(self):
        from backend.app.ingest_pipeline import extract_text

        result = extract_text(b"\x00\x01\x02\x03", "application/octet-stream", "file.bin")
        assert result is None

    def test_json_extraction(self):
        from backend.app.ingest_pipeline import extract_text

        data = b'{"key": "value"}'
        result = extract_text(data, "application/json", "data.json")
        assert result is not None
        assert "value" in result

    def test_zip_extraction(self):
        from backend.app.ingest_pipeline import extract_text

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello.py", "print('hello')\n")
            zf.writestr("readme.md", "# My Project\nDescription here.\n")
        zip_bytes = buf.getvalue()

        result = extract_text(zip_bytes, "application/zip", "project.zip")
        assert result is not None
        assert "hello.py" in result
        assert "readme.md" in result
        assert "print" in result

    def test_zip_skips_binary_members(self):
        from backend.app.ingest_pipeline import extract_text

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("code.py", "x = 1\n")
            zf.writestr("image.png", b"\x89PNG\r\n\x1a\n")
        zip_bytes = buf.getvalue()

        result = extract_text(zip_bytes, "application/zip", "mixed.zip")
        assert result is not None
        assert "code.py" in result
        # .png is not in INGESTIBLE_EXTENSIONS, so it should be skipped
        assert "image.png" not in result

    def test_pdf_without_library_returns_none(self, monkeypatch):
        """When pypdf is not installed, PDF extraction returns None gracefully."""
        import sys
        from unittest.mock import patch

        # Simulate pypdf not being importable
        with patch.dict(sys.modules, {"pypdf": None}):
            # Re-import the function to pick up the patched modules dict
            import importlib

            import backend.app.ingest_pipeline as pipeline
            importlib.reload(pipeline)
            result = pipeline._extract_pdf(b"%PDF-1.4", "test.pdf")
            assert result is None


# ---------------------------------------------------------------------------
# Unit: split_with_overlap
# ---------------------------------------------------------------------------


class TestSplitWithOverlap:
    def test_short_text_single_chunk(self):
        from backend.app.ingest_pipeline import split_with_overlap

        text = "short text"
        chunks = split_with_overlap(text, chunk_size=1000, overlap=100)
        assert chunks == ["short text"]

    def test_splits_long_text(self):
        from backend.app.ingest_pipeline import split_with_overlap

        # 10 lines of 100 chars each = 1000 chars total
        lines = [f"Line {i:02d}: " + "x" * 90 + "\n" for i in range(10)]
        text = "".join(lines)
        chunks = split_with_overlap(text, chunk_size=250, overlap=50)
        assert len(chunks) > 1
        # All chunk content combined should contain all lines
        combined = "".join(chunks)
        for i in range(10):
            assert f"Line {i:02d}" in combined

    def test_overlap_content(self):
        from backend.app.ingest_pipeline import split_with_overlap

        lines = [f"Line{i}\n" for i in range(20)]
        text = "".join(lines)
        chunks = split_with_overlap(text, chunk_size=50, overlap=20)
        assert len(chunks) > 1
        # The second chunk should contain some content from the tail of the first
        if len(chunks) >= 2:
            tail = chunks[0][-20:]
            # The overlap tail should appear at the start of chunk 2
            assert chunks[1].startswith(tail)

    def test_empty_text(self):
        from backend.app.ingest_pipeline import split_with_overlap

        assert split_with_overlap("", chunk_size=100, overlap=10) == []

    def test_zero_overlap(self):
        from backend.app.ingest_pipeline import split_with_overlap

        lines = ["A" * 100 + "\n"] * 5
        text = "".join(lines)
        chunks = split_with_overlap(text, chunk_size=150, overlap=0)
        assert len(chunks) >= 2
        # With zero overlap, the combined length of all chunks should not
        # significantly exceed the original text length (no repeated content)
        combined_len = sum(len(c) for c in chunks)
        assert combined_len <= len(text) + 10  # small tolerance for boundary splits


# ---------------------------------------------------------------------------
# API: POST /v1/ingest/file
# ---------------------------------------------------------------------------


class TestIngestFileEndpoint:
    def test_upload_text_file_returns_202(self, client):
        content = b"This is some plain text content for testing."
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("test.txt", content, "text/plain")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["kind"] == "file"
        assert data["source"] == "test.txt"
        assert data["status"] in ("queued", "running", "success")
        assert "job_id" in data
        # In DISABLE_JOBS mode the job runs synchronously
        assert data["status"] == "success"
        assert data["chunk_count"] >= 1

    def test_upload_python_file(self, client):
        code = (
            b"def hello():\n    print('Hello, world!')\n"
            b"\nif __name__ == '__main__':\n    hello()\n"
        )
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("script.py", code, "text/plain")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "success"
        assert data["file_count"] == 1

    def test_upload_html_file(self, client):
        html = b"<html><body><h1>Title</h1><p>Some text content here.</p></body></html>"
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("page.html", html, "text/html")},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "success"

    def test_upload_csv_file(self, client):
        csv_data = b"col1,col2,col3\nval1,val2,val3\nval4,val5,val6\n"
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("data.csv", csv_data, "text/csv")},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "success"

    def test_upload_empty_binary_file_no_text(self, client, tmp_path):
        """A binary file with no extractable text results in success with 0 chunks."""
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("image.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "success"
        assert data["chunk_count"] == 0

    def test_upload_with_conversation_id(self, client):
        conv_id = str(uuid.uuid4())
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("note.txt", b"Important note", "text/plain")},
            data={"conversation_id": conv_id},
        )
        assert resp.status_code == 202
        assert resp.json()["conversation_id"] == conv_id

    def test_upload_requires_auth(self, client):
        resp = client.post(
            "/v1/ingest/file",
            files={"file": ("test.txt", b"content", "text/plain")},
        )
        assert resp.status_code in (401, 403)

    def test_upload_size_limit(self, client, monkeypatch):
        import backend.app.ingest_routes as ir

        monkeypatch.setattr(ir, "MAX_UPLOAD_BYTES", 10)
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("big.txt", b"x" * 100, "text/plain")},
        )
        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# API: POST /v1/ingest/url
# ---------------------------------------------------------------------------


class TestIngestUrlEndpoint:
    def test_url_ingestion_queued(self, client):
        """
        URL content is pre-fetched in the API layer and stored as blob_data.
        The worker reads ONLY from blob_data — no network dependency.

        MQP-CONTRACT: URL ingestion MUST store blob before enqueue.
        """
        from unittest.mock import MagicMock, patch

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.content = b"<html><body><p>Hello world content</p></body></html>"
        fake_response.headers = {"content-type": "text/html; charset=utf-8"}
        fake_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=fake_response):
            resp = client.post(
                "/v1/ingest/url",
                headers=_AUTH,
                json={"url": "https://example.com/test-page"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["kind"] == "url"
        assert data["source"] == "https://example.com/test-page"
        assert data["job_id"]
        # BACKEND_DISABLE_JOBS=1 runs worker synchronously; job must succeed
        assert data["status"] == "success"

    def test_invalid_url_rejected(self, client):
        resp = client.post(
            "/v1/ingest/url",
            headers=_AUTH,
            json={"url": "ftp://invalid.example.com"},
        )
        assert resp.status_code == 400

    def test_url_requires_auth(self, client):
        resp = client.post("/v1/ingest/url", json={"url": "https://example.com"})
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# API: POST /v1/ingest/repo
# ---------------------------------------------------------------------------


class TestIngestRepoEndpoint:
    def test_repo_ingestion_queued(self, client):
        with mock_github_fetch([("README.md", "# Test Repo")]):
            resp = client.post(
                "/v1/ingest/repo",
                headers=_AUTH,
                json={
                    "repo_url": "https://github.com/testowner/testrepo",
                    "branch": "main",
                    "conversation_id": str(uuid.uuid4()),
                },
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["kind"] == "repo"
        assert "testowner/testrepo" in data["source"]
        assert "main" in data["source"]

    def test_invalid_repo_url_rejected(self, client):
        resp = client.post(
            "/v1/ingest/repo",
            headers=_AUTH,
            json={"repo_url": "https://notgithub.com/owner/repo"},
        )
        assert resp.status_code == 400

    def test_deduplication_returns_existing_job(self, client, monkeypatch):
        """Two identical repo requests return the same job ID (no force_refresh)."""
        import backend.app.ingest_pipeline as pipeline

        # Patch _dispatch_job to a no-op so the job stays "queued" between
        # requests, allowing the deduplication logic to detect it on the second call.
        monkeypatch.setattr(pipeline, "_dispatch_job", lambda job_id: None)

        conv_id = str(uuid.uuid4())
        payload = {
            "repo_url": "https://github.com/owner/repo",
            "branch": "main",
            "conversation_id": conv_id,
        }
        with mock_github_fetch([("main.py", "print('hello')")]):
            resp1 = client.post("/v1/ingest/repo", headers=_AUTH, json=payload)
            resp2 = client.post("/v1/ingest/repo", headers=_AUTH, json=payload)

        assert resp1.status_code == 202
        assert resp2.status_code == 202
        assert resp1.json()["job_id"] == resp2.json()["job_id"]

    def test_force_refresh_creates_new_job(self, client, monkeypatch):
        """force_refresh=true always creates a new job."""
        import backend.app.ingest_pipeline as pipeline

        monkeypatch.setattr(pipeline, "_dispatch_job", lambda job_id: None)

        conv_id = str(uuid.uuid4())
        base_payload = {
            "repo_url": "https://github.com/owner/repo2",
            "branch": "main",
            "conversation_id": conv_id,
        }
        with mock_github_fetch([("code.py", "def foo(): pass")]):
            resp1 = client.post("/v1/ingest/repo", headers=_AUTH, json=base_payload)
            resp2 = client.post(
                "/v1/ingest/repo",
                headers=_AUTH,
                json={**base_payload, "force_refresh": True},
            )
        assert resp1.status_code == 202
        assert resp2.status_code == 202
        assert resp1.json()["job_id"] != resp2.json()["job_id"]

    def test_repo_requires_auth(self, client):
        resp = client.post(
            "/v1/ingest/repo",
            json={"repo_url": "https://github.com/owner/repo"},
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# API: GET /v1/ingest/jobs and GET /v1/ingest/{job_id}
# ---------------------------------------------------------------------------


class TestIngestJobStatus:
    def test_get_job_status(self, client):
        # Create a job first
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("note.txt", b"Hello world", "text/plain")},
        )
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/v1/ingest/{job_id}", headers=_AUTH)
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["job_id"] == job_id
        assert data["kind"] == "file"

    def test_get_nonexistent_job_returns_404(self, client):
        resp = client.get(f"/v1/ingest/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404

    def test_get_invalid_job_id_returns_400(self, client):
        resp = client.get("/v1/ingest/not-a-uuid", headers=_AUTH)
        assert resp.status_code == 400

    def test_list_jobs(self, client):
        # Upload two files
        for i in range(2):
            client.post(
                "/v1/ingest/file",
                headers=_AUTH,
                files={"file": (f"file{i}.txt", f"content {i}".encode(), "text/plain")},
            )

        resp = client.get("/v1/ingest/jobs", headers=_AUTH)
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) >= 2

    def test_list_jobs_filter_by_kind(self, client):
        conv_id = str(uuid.uuid4())
        client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("f.txt", b"text", "text/plain")},
            data={"conversation_id": conv_id},
        )
        client.post(
            "/v1/ingest/repo",
            headers=_AUTH,
            json={
                "repo_url": "https://github.com/o/r",
                "conversation_id": conv_id,
            },
        )

        file_jobs = client.get(
            f"/v1/ingest/jobs?kind=file&conversation_id={conv_id}", headers=_AUTH
        ).json()
        repo_jobs = client.get(
            f"/v1/ingest/jobs?kind=repo&conversation_id={conv_id}", headers=_AUTH
        ).json()

        assert all(j["kind"] == "file" for j in file_jobs)
        assert all(j["kind"] == "repo" for j in repo_jobs)

    def test_list_jobs_requires_auth(self, client):
        resp = client.get("/v1/ingest/jobs")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# API: DELETE /v1/ingest/{job_id}
# ---------------------------------------------------------------------------


class TestDeleteIngestJob:
    def test_delete_job_removes_chunks(self, client):
        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={
                "file": (
                    "delete_me.txt",
                    b"This is content that should be chunked and then deleted.",
                    "text/plain",
                )
            },
        )
        job_id = resp.json()["job_id"]
        chunk_count = resp.json()["chunk_count"]
        assert chunk_count >= 1

        del_resp = client.delete(f"/v1/ingest/{job_id}", headers=_AUTH)
        assert del_resp.status_code == 204

        # Job should be gone
        get_resp = client.get(f"/v1/ingest/{job_id}", headers=_AUTH)
        assert get_resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete(f"/v1/ingest/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404

    def test_delete_removes_blob_and_chunks(self, client):
        """
        DELETE removes the job record (and its blob_data) plus all associated chunks.

        MQP-CONTRACT: All data is DB-backed; no filesystem cleanup is involved.
        """
        import uuid as _uuid

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob, RepoChunk

        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={
                "file": (
                    "delete_blob_test.txt",
                    b"Content to be fully removed from the database on delete.",
                    "text/plain",
                )
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        chunk_count = resp.json()["chunk_count"]
        assert chunk_count >= 1

        # Confirm job and chunks exist in DB before delete
        job_uuid = _uuid.UUID(job_id)
        with Session(get_engine()) as s:
            assert s.get(IngestJob, job_uuid) is not None

        # Delete the job
        del_resp = client.delete(f"/v1/ingest/{job_id}", headers=_AUTH)
        assert del_resp.status_code == 204

        # Job record must be gone from DB
        with Session(get_engine()) as s:
            assert s.get(IngestJob, job_uuid) is None, (
                "Job record must be removed from DB on DELETE"
            )
            # Chunks must also be gone
            from sqlmodel import select as _select
            remaining = s.exec(
                _select(RepoChunk).where(RepoChunk.ingest_job_id == job_uuid)
            ).all()
            assert remaining == [], (
                f"All {len(remaining)} chunk(s) must be removed from DB on DELETE"
            )

    def test_delete_requires_auth(self, client):
        resp = client.delete(f"/v1/ingest/{uuid.uuid4()}")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Invariant enforcement regression tests
# ---------------------------------------------------------------------------


class TestInvariantEnforcement:
    """
    MQP-CONTRACT: AIC-v1.1 — Regression tests that lock invariants.

    These tests MUST BREAK THE BUILD if any invariant is removed or weakened.
    """

    def test_blob_stored_in_db_before_enqueue(self, client):
        """
        INVARIANT 1+2: blob_data must exist in DB and job must be in 'queued'
        state before dispatch.

        Verifies the API layer stores blob THEN transitions state THEN dispatches —
        by intercepting _dispatch_job at the moment it is called from transition().
        """
        import uuid as _uuid

        from sqlmodel import Session

        import backend.app.ingest_pipeline as pipeline
        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        captured = {}
        original_dispatch = pipeline._dispatch_job

        def capturing_dispatch(job_id):
            with Session(get_engine()) as s:
                job = s.get(IngestJob, _uuid.UUID(job_id))
                captured["status"] = job.status if job else None
                captured["has_blob"] = bool(job.blob_data) if job else False
                captured["blob_size"] = job.blob_size_bytes if job else 0
            original_dispatch(job_id)

        pipeline._dispatch_job = capturing_dispatch
        try:
            resp = client.post(
                "/v1/ingest/file",
                headers=_AUTH,
                files={"file": ("invariant_test.txt", b"hello invariant", "text/plain")},
            )
        finally:
            pipeline._dispatch_job = original_dispatch

        assert resp.status_code == 202
        assert captured.get("status") == "queued", (
            f"INVARIANT_VIOLATION: job must be in 'queued' state at dispatch, "
            f"got {captured.get('status')!r}"
        )
        assert captured.get("has_blob") is True, (
            "INVARIANT_VIOLATION: blob_data must exist in DB before dispatch"
        )
        assert captured.get("blob_size", 0) > 0, (
            "INVARIANT_VIOLATION: blob_size_bytes must be > 0 before dispatch"
        )

    def test_worker_fails_deterministically_if_blob_missing(self):
        """
        INVARIANT 4: Worker must transition job to FAILED with WORKER_ENTRY_VIOLATION
        if blob_data is missing — never raise an unhandled exception.
        """
        import uuid as _uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import process_ingest_job
        from backend.app.models import IngestJob

        # Create a job in QUEUED state with NO blob_data
        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="ghost.txt",
                status="queued",
                blob_data=None,
                blob_size_bytes=0,
            )
            s.add(job)
            s.commit()

        # Worker must handle this gracefully — no unhandled exception
        process_ingest_job(str(job_id))

        # Job must be in FAILED state with PIPELINE_VALIDATION_FAIL error
        with Session(db_module.get_engine()) as s:
            job = s.get(IngestJob, job_id)
            assert job is not None
            assert job.status == "failed", (
                f"INVARIANT_VIOLATION: worker must transition to 'failed' "
                f"when blob_data is missing, got {job.status!r}"
            )
            assert job.error and "PIPELINE_VALIDATION_FAIL" in job.error, (
                f"INVARIANT_VIOLATION: error must contain 'PIPELINE_VALIDATION_FAIL', "
                f"got {job.error!r}"
            )

    def test_enqueue_blocked_without_queued_state(self, client):
        """
        INVARIANT 5 (STRUCTURAL): transition("queued") raises ENQUEUE_GATE_VIOLATION
        if blob_data is absent, even when the state machine would otherwise allow it.

        This verifies the gate is inside transition(), not in caller code.
        """
        import uuid as _uuid

        import pytest
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        # Create a job that has reached 'stored' state but then somehow
        # lost its blob_data (e.g., DB corruption scenario).
        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="gate_test.txt",
                status="stored",
                blob_data=None,
                blob_size_bytes=0,
            )
            s.add(job)
            s.commit()

        # transition("queued") MUST raise — the enqueue gate is inside transition()
        with pytest.raises(RuntimeError, match="ENQUEUE_GATE_VIOLATION"):
            transition(job_id, "queued")

    def test_enqueue_blocked_without_blob_data(self, client):
        """
        INVARIANT 1 (STRUCTURAL): transition("queued") raises ENQUEUE_GATE_VIOLATION
        if blob_data is absent.

        Tests the identical scenario via the public transition() API to confirm
        the gate cannot be bypassed by any caller.
        """
        import uuid as _uuid

        import pytest
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="no_blob.txt",
                status="stored",
                blob_data=None,
                blob_size_bytes=0,
            )
            s.add(job)
            s.commit()

        with pytest.raises(RuntimeError, match="ENQUEUE_GATE_VIOLATION"):
            transition(job_id, "queued")

    def test_size_limit_rejects_before_db_record_created(self, client, monkeypatch):
        """
        SIZE ENFORCEMENT: 413 must be returned before any IngestJob is created.
        """

        from sqlmodel import Session, select

        import backend.app.ingest_routes as ir
        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        monkeypatch.setattr(ir, "MAX_UPLOAD_BYTES", 5)

        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("toobig.txt", b"x" * 100, "text/plain")},
        )
        assert resp.status_code == 413

        # No DB record must exist for this rejected upload
        with Session(get_engine()) as s:
            jobs = s.exec(
                select(IngestJob).where(IngestJob.source == "toobig.txt")
            ).all()
            assert jobs == [], (
                "INVARIANT_VIOLATION: no IngestJob must be created when upload is rejected"
            )

    def test_worker_does_not_raise_when_job_already_failed(self):
        """
        Regression test for the failed→failed STATE_MACHINE_VIOLATION loop.

        If a job is ALREADY in 'failed' state when the worker picks it up
        (e.g., the route handler raced to mark it failed after enqueue),
        ``process_ingest_job`` must return cleanly WITHOUT raising an exception.

        Previously the exception handler called ``_transition(FAILED)``
        unconditionally, which raised RuntimeError for ``failed → failed``
        transitions. That propagated to RQ, triggering infinite retries.
        """
        import uuid as _uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import process_ingest_job
        from backend.app.models import IngestJob

        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="already_failed.txt",
                status="failed",
                blob_data=b"some data",
                blob_size_bytes=9,
                error="pre-existing failure",
            )
            s.add(job)
            s.commit()

        # Must NOT raise — previously this raised RuntimeError and caused RQ retries
        process_ingest_job(str(job_id))

        # Job must still be in 'failed' state with the original error preserved
        with Session(db_module.get_engine()) as s:
            job = s.get(IngestJob, job_id)
            assert job is not None
            assert job.status == "failed", (
                f"Worker must not change a pre-failed job's state, got {job.status!r}"
            )
            assert job.error == "pre-existing failure", (
                "Worker must not overwrite the original error message"
            )

    def test_exception_handler_swallows_secondary_transition_error(self):
        """
        Regression test: if the pipeline exception handler itself cannot
        transition to 'failed' (because the job is already terminal), it must
        NOT propagate the secondary RuntimeError to RQ.

        This simulates the race condition where a concurrent process already
        marked the job failed before the exception handler runs.
        """
        import uuid as _uuid
        from unittest.mock import patch

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import (
            IngestJobState,
            _transition,
            process_ingest_job,
        )
        from backend.app.models import IngestJob

        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="race_condition.txt",
                status="queued",
                blob_data=b"hello race",
                blob_size_bytes=10,
            )
            s.add(job)
            s.commit()

        # Simulate a mid-pipeline failure where the job is concurrently marked
        # failed, so the exception handler's _transition(FAILED) would hit
        # 'failed → failed'.  We do this by making _ingest_file raise an error
        # AND patching _transition to fail for the FAILED→FAILED call.
        original_transition = _transition
        calls: list[str] = []

        def patched_transition(jid, state, **payload):
            calls.append(state)
            # Simulate the job already being in failed state when the exception
            # handler tries to transition to FAILED a second time.
            if state == IngestJobState.FAILED and calls.count(IngestJobState.FAILED) >= 2:
                raise RuntimeError(
                    "STATE_MACHINE_VIOLATION: Cannot transition from terminal state "
                    "failed to failed"
                )
            return original_transition(jid, state, **payload)

        import backend.app.ingest_pipeline as pipeline_mod

        with patch.object(pipeline_mod, "_ingest_file", side_effect=RuntimeError("boom")):
            with patch.object(pipeline_mod, "_transition", side_effect=patched_transition):
                # Must NOT raise — secondary transition error must be swallowed
                process_ingest_job(str(job_id))


# ---------------------------------------------------------------------------
# Static structural invariant scans
# ---------------------------------------------------------------------------


class TestStaticInvariants:
    """
    Static scan tests that enforce structural invariants by inspecting source.

    MQP-CONTRACT: CI MUST FAIL if forbidden patterns appear in worker code.
    These tests break the build if legacy architecture re-emerges.
    """

    @staticmethod
    def _worker_sources() -> dict[str, str]:
        """Return {name: source} for all worker-path functions."""
        import inspect

        from backend.app import ingest_pipeline as p

        return {
            fn.__name__: inspect.getsource(fn)
            for fn in (
                p.process_ingest_job,
                p._ingest_file,
                p._ingest_url,
                p._ingest_repo,
            )
        }

    def test_no_tmp_paths_in_ingest_pipeline(self):
        """
        Worker pipeline must never reference /tmp/ filesystem paths.
        All data is stored in the database.
        """
        import inspect

        from backend.app import ingest_pipeline

        source_lines = inspect.getsource(ingest_pipeline).splitlines()
        violations = [
            f"line {i + 1}: {line.rstrip()}"
            for i, line in enumerate(source_lines)
            if "/tmp/" in line and not line.strip().startswith("#")
        ]
        assert not violations, (
            "INVARIANT_VIOLATION: /tmp/ path found in ingest_pipeline.py — "
            "all data must use database storage:\n" + "\n".join(violations)
        )

    def test_no_httpx_in_worker_functions(self):
        """
        Worker functions must not use httpx.
        Network access is forbidden in the worker; URL content is pre-fetched
        at the API layer and stored as blob_data.
        """
        for name, source in self._worker_sources().items():
            assert "httpx" not in source, (
                f"INVARIANT_VIOLATION: httpx found in {name}() — "
                f"workers must be pure (no network access)"
            )

    def test_no_filesystem_open_in_worker_functions(self):
        """
        Worker functions must not call open() for filesystem access.
        All I/O uses in-memory bytes (io.BytesIO) read from blob_data.
        """
        import re

        for name, source in self._worker_sources().items():
            # Match bare open( but not io.open( or os.fdopen(
            if re.search(r"(?<![.\w])open\s*\(", source):
                raise AssertionError(
                    f"INVARIANT_VIOLATION: open() found in {name}() — "
                    f"workers must not access the filesystem directly"
                )

    def test_no_staging_dir_references(self):
        """
        _STAGING_DIR and source_path must be absent from every production
        ingestion module.  The filesystem-staging architecture is eliminated.

        MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE Section 11 — ZERO tolerance.
        Covers: ingest_pipeline, ingest_routes, github_routes, models.
        """
        import inspect

        import backend.app.github_routes as gr
        import backend.app.ingest_pipeline as ip
        import backend.app.ingest_routes as ir
        import backend.app.models as mo

        modules = (
            ("ingest_routes", ir),
            ("ingest_pipeline", ip),
            ("github_routes", gr),
            ("models", mo),
        )
        for mod_name, mod in modules:
            source = inspect.getsource(mod)
            assert "_STAGING_DIR" not in source, (
                f"INVARIANT_VIOLATION: _STAGING_DIR found in {mod_name} — "
                f"filesystem staging has been eliminated"
            )
            assert "source_path" not in source, (
                f"INVARIANT_VIOLATION: source_path found in {mod_name} — "
                f"filesystem staging has been eliminated"
            )

    def test_source_path_construction_fails(self):
        """
        MQP-CONTRACT: AIC-v1.1-ENFORCEMENT-COMPLETE Section 11 — ZERO tolerance.

        The field is structurally absent from IngestJob.

        • SQLModel silently drops unknown constructor kwargs, so source_path
          cannot enter the object via construction — it is absent on the result.
        • Post-construction assignment via __setattr__ raises ValueError.

        Both proofs together show the illegal field cannot enter the system.
        """
        import uuid

        from backend.app.models import IngestJob

        # Construction with unknown field: SQLModel silently drops it.
        # The resulting object must NOT carry source_path.
        job = IngestJob(
            id=uuid.uuid4(),
            kind="file",
            source="test.txt",
            status="created",
            **{"source_path": "/tmp/illegal"},
        )
        assert not hasattr(job, "source_path"), (
            "INVARIANT_VIOLATION: IngestJob accepted source_path via constructor — "
            "filesystem-staging field must be structurally absent from the model"
        )

        # Post-construction assignment is explicitly rejected by SQLModel.
        with pytest.raises(ValueError):
            job.__setattr__("source_path", "/tmp/illegal")

    def test_no_ready_flag_references_in_pipeline(self):
        """
        .ready flag pattern must not exist in the pipeline.
        Ready flags were part of the eliminated filesystem-staging protocol.
        """
        import inspect

        from backend.app import ingest_pipeline

        source = inspect.getsource(ingest_pipeline)
        assert ".ready" not in source, (
            "INVARIANT_VIOLATION: .ready flag reference found in ingest_pipeline — "
            "filesystem-staging protocol has been eliminated"
        )

    def test_no_external_dispatch_calls_in_routes(self):
        """
        MQP-CONTRACT: AIC-v1.1-FINAL-INVARIANT-SEAL §4

        Routes MUST NOT call _dispatch_job() or _enqueue() directly.
        Dispatch is a structural consequence of transition("queued") —
        it happens inside ingest_pipeline.transition(), not in caller code.

        CI MUST FAIL if any direct dispatch call appears in ingest_routes.
        """
        import inspect

        import backend.app.ingest_routes as ir

        source = inspect.getsource(ir)
        assert "_dispatch_job(" not in source, (
            "INVARIANT_VIOLATION: _dispatch_job() called directly in ingest_routes — "
            "dispatch must only occur inside transition(); never in routes"
        )
        assert "_enqueue(" not in source, (
            "INVARIANT_VIOLATION: _enqueue() called directly in ingest_routes — "
            "the _enqueue helper has been eliminated; dispatch is inside transition()"
        )

    def test_dispatch_coupled_to_transition_in_pipeline(self):
        """
        MQP-CONTRACT: AIC-v1.1-FINAL-INVARIANT-SEAL §4

        _dispatch_job() MUST be called from within transition() — it must be
        structurally impossible to reach dispatch without going through
        the full transition authority (state validation + blob gate + commit).

        Verify by inspecting the source of transition().
        """
        import inspect

        from backend.app.ingest_pipeline import transition

        source = inspect.getsource(transition)
        assert "_dispatch_job(" in source, (
            "INVARIANT_VIOLATION: _dispatch_job() is not called from transition() — "
            "the structural coupling has been broken"
        )


# ---------------------------------------------------------------------------
# Invalid path prevention tests
# ---------------------------------------------------------------------------


class TestInvalidPathPrevention:
    """
    MQP-CONTRACT: AIC-v1.1-FINAL-INVARIANT-SEAL §2

    Simulate attempts to create or execute invalid ingestion jobs.
    ALL attempts MUST be structurally blocked.
    """

    def test_simulate_invalid_path_attempt_no_blob(self):
        """
        Simulate a scenario where code attempts to enqueue a job
        without blob_data — this MUST be structurally impossible.

        Any code path that tries to call transition("queued") on a job
        without blob_data MUST raise ENQUEUE_GATE_VIOLATION.
        """
        import uuid as _uuid

        import pytest
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        # Attempt: create job in "stored" state (bypassing blob requirement)
        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="attempt.txt",
                status="stored",
                blob_data=None,
                blob_size_bytes=0,
            )
            s.add(job)
            s.commit()

        # Attempt: transition to queued — MUST be blocked
        with pytest.raises(RuntimeError, match="ENQUEUE_GATE_VIOLATION"):
            transition(job_id, "queued")

        # Verify job was NOT dispatched (still in 'stored' state, not 'queued')
        with Session(db_module.get_engine()) as s:
            job = s.get(IngestJob, job_id)
            assert job.status == "stored", (
                "INVARIANT_VIOLATION: job must remain in 'stored' after failed enqueue attempt"
            )

    def test_simulate_invalid_path_attempt_bypass_transition(self):
        """
        Simulate an attempt to manipulate job state by mutating status directly,
        bypassing transition() authority.

        The validate_state_transition() function enforces state machine rules.
        Direct mutation is technically possible in Python but architecturally
        forbidden — this test documents the requirement.
        """
        import uuid as _uuid

        import pytest
        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.ingest_pipeline import transition
        from backend.app.models import IngestJob

        # Attempt: skip "stored" and jump directly from "created" to "queued"
        job_id = _uuid.uuid4()
        with Session(db_module.get_engine()) as s:
            job = IngestJob(
                id=job_id,
                kind="file",
                source="bypass.txt",
                status="created",
                blob_data=b"data",
                blob_size_bytes=4,
            )
            s.add(job)
            s.commit()

        # transition() MUST reject created → queued (must go through stored)
        with pytest.raises(Exception):  # STATE_MACHINE_VIOLATION
            transition(job_id, "queued")

    def test_file_upload_job_never_created_without_blob(self, client, monkeypatch):
        """
        MQP-CONTRACT: File upload jobs must be created with blob_data in a
        single atomic commit — the "created without blob" window is eliminated.

        Verify: DB record is never present in "created" state without blob_data
        after a successful upload.
        """

        from sqlmodel import Session

        import backend.app.ingest_pipeline as pipeline
        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        snapshots = []
        original_dispatch = pipeline._dispatch_job

        def inspecting_dispatch(job_id):
            # Look up the job at dispatch time — it must already have blob_data
            with Session(get_engine()) as s:
                all_jobs = s.exec(
                    __import__("sqlmodel").select(IngestJob)
                ).all()
                for j in all_jobs:
                    snapshots.append({
                        "id": str(j.id),
                        "status": j.status,
                        "has_blob": bool(j.blob_data),
                    })
            original_dispatch(job_id)

        pipeline._dispatch_job = inspecting_dispatch
        try:
            resp = client.post(
                "/v1/ingest/file",
                headers=_AUTH,
                files={"file": ("atomic_test.txt", b"atomic blob data", "text/plain")},
            )
        finally:
            pipeline._dispatch_job = original_dispatch

        assert resp.status_code == 202

        # At no point during dispatch should any job exist without blob_data
        blobless = [s for s in snapshots if not s["has_blob"]]
        assert blobless == [], (
            "INVARIANT_VIOLATION: jobs without blob_data observed at dispatch time — "
            f"found: {blobless}"
        )


class TestDeterministicStateLayer:
    @staticmethod
    def _create_job(status: str, conversation_id: str | None = None) -> str:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        job = IngestJob(
            id=uuid.uuid4(),
            kind="repo",
            source="https://github.com/example/repo@main",
            status=status,
            blob_data=b"repo-manifest",
            blob_size_bytes=13,
            conversation_id=conversation_id,
        )
        with Session(get_engine()) as s:
            s.add(job)
            s.commit()
            s.refresh(job)
            assert job.status == status
        return str(job.id)

    @staticmethod
    def _set_status(job_id: str, status: str, execution_locked: bool | None = None) -> None:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as s:
            job = s.get(IngestJob, uuid.UUID(job_id))
            assert job is not None
            job.status = status
            if execution_locked is not None:
                job.execution_locked = execution_locked
            s.add(job)
            s.commit()

    def test_rehydrate_on_screen_entry(self, client):
        conv_id = str(uuid.uuid4())
        job_id = self._create_job("running", conv_id)

        first = client.get(f"/chat/{conv_id}/jobs?kind=repo", headers=_AUTH)
        assert first.status_code == 200
        first_job = next(j for j in first.json() if j["job_id"] == job_id)
        assert first_job["status"] == "running"

        self._set_status(job_id, "success", execution_locked=True)

        second = client.get(f"/chat/{conv_id}/jobs?kind=repo", headers=_AUTH)
        assert second.status_code == 200
        second_job = next(j for j in second.json() if j["job_id"] == job_id)
        assert second_job["status"] == "success"
        assert second_job["execution_locked"] is True

    def test_poll_until_completion(self, client):
        job_id = self._create_job("running")

        first = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert first.status_code == 200
        assert first.json()["status"] == "running"

        self._set_status(job_id, "success", execution_locked=True)

        second = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert second.status_code == 200
        assert second.json()["status"] == "success"

    def test_navigation_independence(self, client):
        job_id = self._create_job("processing")

        initial = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert initial.status_code == 200
        assert initial.json()["status"] == "running"

        self._set_status(job_id, "finalizing")
        resumed = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"

        self._set_status(job_id, "failed", execution_locked=True)
        terminal = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert terminal.status_code == 200
        assert terminal.json()["status"] == "failed"

    def test_no_local_state_dependency(self, client):
        job_id = self._create_job("stored")

        first = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert first.status_code == 200
        assert first.json()["status"] == "queued"

        second = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert second.status_code == 200
        assert second.json()["status"] == "queued"

    def test_terminal_state_persistence(self, client):
        conv_id = str(uuid.uuid4())
        job_id = self._create_job("success", conv_id)
        self._set_status(job_id, "success", execution_locked=True)

        detail = client.get(f"/jobs/{job_id}", headers=_AUTH)
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["status"] == "success"
        assert detail_body["execution_locked"] is True
        assert set(detail_body) >= {
            "job_id",
            "status",
            "execution_locked",
            "created_at",
            "updated_at",
        }

        listing = client.get(f"/chat/{conv_id}/jobs?kind=repo", headers=_AUTH)
        assert listing.status_code == 200
        listed = next(j for j in listing.json() if j["job_id"] == job_id)
        assert listed["status"] == "success"
        assert listed["execution_locked"] is True
