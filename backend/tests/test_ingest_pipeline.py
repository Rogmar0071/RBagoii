"""
backend/tests/test_ingest_pipeline.py
======================================
Tests for the new unified ingestion pipeline.

Covers:
- Text extraction (plain text, HTML, CSV, ZIP, missing PDF/DOCX libs)
- Chunking with overlap
- IngestJob model creation and status transitions
- API endpoints: POST /v1/ingest/{file,url,repo}, GET, DELETE
- Deduplication for repo jobs
- Staging file cleanup on DELETE
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
        code = b"def hello():\n    print('Hello, world!')\n\nif __name__ == '__main__':\n    hello()\n"
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
        """URL ingestion endpoint returns 202 and creates a job."""
        # We don't mock httpx here — the job will fail (no real server),
        # but the endpoint itself must return 202 and create a queued job.
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
        # Status is either queued, running, success, or failed — any is valid here

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
        import backend.app.ingest_routes as ir

        # Patch _enqueue to a no-op so the job stays "queued" between requests,
        # allowing the deduplication logic to detect it on the second call.
        monkeypatch.setattr(ir, "_enqueue", lambda job_id: None)

        conv_id = str(uuid.uuid4())
        payload = {
            "repo_url": "https://github.com/owner/repo",
            "branch": "main",
            "conversation_id": conv_id,
        }
        resp1 = client.post("/v1/ingest/repo", headers=_AUTH, json=payload)
        resp2 = client.post("/v1/ingest/repo", headers=_AUTH, json=payload)

        assert resp1.status_code == 202
        assert resp2.status_code == 202
        assert resp1.json()["job_id"] == resp2.json()["job_id"]

    def test_force_refresh_creates_new_job(self, client, monkeypatch):
        """force_refresh=true always creates a new job."""
        import backend.app.ingest_routes as ir

        monkeypatch.setattr(ir, "_enqueue", lambda job_id: None)

        conv_id = str(uuid.uuid4())
        base_payload = {
            "repo_url": "https://github.com/owner/repo2",
            "branch": "main",
            "conversation_id": conv_id,
        }
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

    def test_delete_cleans_staging_file(self, client, tmp_path, monkeypatch):
        import backend.app.ingest_routes as ir

        staging = tmp_path / "staging"
        staging.mkdir()
        monkeypatch.setattr(ir, "_STAGING_DIR", staging)

        resp = client.post(
            "/v1/ingest/file",
            headers=_AUTH,
            files={"file": ("staged.txt", b"staged content here", "text/plain")},
        )
        job_id = resp.json()["job_id"]

        # Delete the job — staged file should be removed
        client.delete(f"/v1/ingest/{job_id}", headers=_AUTH)
        remaining = list(staging.iterdir())
        assert len(remaining) == 0

    def test_delete_requires_auth(self, client):
        resp = client.delete(f"/v1/ingest/{uuid.uuid4()}")
        assert resp.status_code in (401, 403)
