"""
Tests for the analysis pipeline: chunked uploads, zip extraction, job processor,
and the /v1/analysis REST endpoints.
"""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

# Disable heavy jobs.
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture(autouse=True)
def _configure_uploads_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point _UPLOADS_DIR to a tmp_path so tests don't write to /tmp/uploads."""
    import backend.app.main as m

    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "_UPLOADS_DIR", uploads)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


_AUTH = {"Authorization": f"Bearer {TOKEN}"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41\x00\x00\x00\x08free"


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Return the bytes of an in-memory zip containing {name: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Upload endpoint — MIME type and size validation
# ---------------------------------------------------------------------------


class TestUploadValidation:
    def test_mp4_upload_accepted(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        resp = client.post(
            "/v1/sessions",
            files={"video": ("recording.mp4", _TINY_MP4, "video/mp4")},
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 201

    def test_zip_upload_accepted(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        zip_bytes = _make_zip({"README.md": b"hello"})
        resp = client.post(
            "/v1/sessions",
            files={"video": ("archive.zip", zip_bytes, "application/zip")},
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 201

    def test_unsupported_mime_type_rejected(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        resp = client.post(
            "/v1/sessions",
            files={"video": ("doc.pdf", b"%PDF", "application/pdf")},
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 415

    def test_oversized_upload_rejected(self, client: TestClient, tmp_path, monkeypatch) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        # Set limit to 100 bytes.
        monkeypatch.setattr(m, "MAX_UPLOAD_BYTES", 100)
        big_data = b"x" * 200
        resp = client.post(
            "/v1/sessions",
            files={"video": ("big.mp4", big_data, "video/mp4")},
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Chunked upload endpoints
# ---------------------------------------------------------------------------


class TestChunkedUpload:
    def _upload_id(self) -> str:
        import uuid

        return str(uuid.uuid4())

    def test_single_chunk_then_finalize(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        upload_id = self._upload_id()
        data = _TINY_MP4

        # Send one chunk.
        resp = client.post(
            "/v1/sessions/chunks",
            files={"chunk": ("part0", data, "video/mp4")},
            headers={
                **_AUTH,
                "X-Upload-Id": upload_id,
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["chunks_received"] == 1
        assert body["complete"] is True

        # Finalize.
        resp2 = client.put(
            f"/v1/sessions/chunks/{upload_id}/finalize",
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp2.status_code == 201
        assert "session_id" in resp2.json()

    def test_multi_chunk_then_finalize(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        upload_id = self._upload_id()
        # Split into 2 chunks.
        half = len(_TINY_MP4) // 2
        chunks = [_TINY_MP4[:half], _TINY_MP4[half:]]

        for i, chunk_data in enumerate(chunks):
            resp = client.post(
                "/v1/sessions/chunks",
                files={"chunk": (f"part{i}", chunk_data, "video/mp4")},
                headers={
                    **_AUTH,
                    "X-Upload-Id": upload_id,
                    "X-Chunk-Index": str(i),
                    "X-Total-Chunks": "2",
                },
            )
            assert resp.status_code == 202

        resp2 = client.put(
            f"/v1/sessions/chunks/{upload_id}/finalize",
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp2.status_code == 201

    def test_finalize_incomplete_upload_returns_409(self, client: TestClient, tmp_path) -> None:
        import backend.app.main as m

        m.DATA_DIR = tmp_path
        upload_id = self._upload_id()
        # Only send 1 of 3 chunks.
        client.post(
            "/v1/sessions/chunks",
            files={"chunk": ("part0", _TINY_MP4, "video/mp4")},
            headers={
                **_AUTH,
                "X-Upload-Id": upload_id,
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "3",
            },
        )
        resp = client.put(
            f"/v1/sessions/chunks/{upload_id}/finalize",
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 409

    def test_finalize_nonexistent_upload_returns_404(self, client: TestClient) -> None:
        import uuid

        resp = client.put(
            f"/v1/sessions/chunks/{uuid.uuid4()}/finalize",
            data={"meta": ""},
            headers=_AUTH,
        )
        assert resp.status_code == 404

    def test_invalid_upload_id_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/sessions/chunks",
            files={"chunk": ("p0", b"data", "video/mp4")},
            headers={
                **_AUTH,
                "X-Upload-Id": "not-a-uuid",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Zip extraction unit tests
# ---------------------------------------------------------------------------


class TestZipExtraction:
    def test_valid_zip_extracted(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import extract_zip

        zip_bytes = _make_zip(
            {
                "app/src/main/AndroidManifest.xml": b"<manifest/>",
                "README.md": b"# Project",
                "src/main.kt": b"fun main() {}",
            }
        )
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract_dir = tmp_path / "out"
        extract_dir.mkdir()

        result = extract_zip(str(zip_path), str(extract_dir))

        # .xml and .kt are relevant; .md is not.
        assert result["files_extracted"] == 2
        assert result["files_skipped"] == 1

    def test_corrupt_zip_raises(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import extract_zip

        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip file at all !@#$")
        extract_dir = tmp_path / "out"
        extract_dir.mkdir()

        with pytest.raises(ValueError, match="corrupt_archive"):
            extract_zip(str(zip_path), str(extract_dir))

    def test_zip_bomb_rejected(self, tmp_path, monkeypatch) -> None:
        import backend.app.analysis_job_processor as ajp

        # Set limit to 1 byte so any zip triggers it.
        monkeypatch.setattr(ajp, "MAX_UNCOMPRESSED_BYTES", 1)

        zip_bytes = _make_zip({"file.kt": b"fun main(){}"})
        zip_path = tmp_path / "bomb.zip"
        zip_path.write_bytes(zip_bytes)
        extract_dir = tmp_path / "out"
        extract_dir.mkdir()

        with pytest.raises(ValueError, match="zip_bomb"):
            ajp.extract_zip(str(zip_path), str(extract_dir))

    def test_path_traversal_in_zip_skipped(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import extract_zip

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # This would escape the extraction dir on a naive extractor.
            zf.writestr("../../../etc/evil.kt", b"evil")
            zf.writestr("safe/file.kt", b"fun main(){}")
        zip_bytes = buf.getvalue()
        zip_path = tmp_path / "traversal.zip"
        zip_path.write_bytes(zip_bytes)
        extract_dir = tmp_path / "out"
        extract_dir.mkdir()

        result = extract_zip(str(zip_path), str(extract_dir))
        # The traversal entry is skipped; safe entry is extracted.
        assert result["files_extracted"] == 1

    def test_irrelevant_files_skipped(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import extract_zip

        zip_bytes = _make_zip(
            {
                "notes.txt": b"hello",
                "Thumbs.db": b"binary",
                "app.kt": b"fun app(){}",
            }
        )
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract_dir = tmp_path / "out"
        extract_dir.mkdir()

        result = extract_zip(str(zip_path), str(extract_dir))
        assert result["files_extracted"] == 1  # only .kt
        assert result["files_skipped"] == 2


# ---------------------------------------------------------------------------
# Analysis pipeline stage unit tests
# ---------------------------------------------------------------------------


class TestAnalysisPipeline:
    def test_parse_ui_files_finds_xml(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import parse_ui_files

        layout = tmp_path / "res" / "layout"
        layout.mkdir(parents=True)
        (layout / "activity_main.xml").write_text("<LinearLayout/>")
        (tmp_path / "AndroidManifest.xml").write_text("<manifest/>")

        result = parse_ui_files(str(tmp_path))

        assert result["manifest_found"] is True
        assert len(result["layout_files"]) == 2

    def test_check_code_syntax_valid_python(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import check_code_syntax

        src = tmp_path / "main.py"
        src.write_text("def hello(): pass\n")

        result = check_code_syntax(str(tmp_path))
        assert result["python"]["files_checked"] == 1
        assert result["python"]["errors"] == []

    def test_check_code_syntax_invalid_python(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import check_code_syntax

        src = tmp_path / "bad.py"
        src.write_text("def hello(: pass\n")  # syntax error

        result = check_code_syntax(str(tmp_path))
        assert result["python"]["files_checked"] == 1
        assert len(result["python"]["errors"]) == 1

    def test_check_code_syntax_kotlin_unbalanced_braces(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import check_code_syntax

        src = tmp_path / "App.kt"
        src.write_text("fun main() { val x = 1\n")  # one { but no }

        result = check_code_syntax(str(tmp_path))
        assert result["kotlin"]["files_checked"] == 1
        assert len(result["kotlin"]["errors"]) == 1

    def test_verify_assets_valid_png(self, tmp_path) -> None:
        import struct
        import zlib

        from backend.app.analysis_job_processor import verify_assets

        # Minimal valid 1×1 white PNG.
        def _minimal_png() -> bytes:
            sig = b"\x89PNG\r\n\x1a\n"
            ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
            ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
            ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
            idat_data = zlib.compress(b"\x00\xff\xff\xff")
            idat_crc = zlib.crc32(b"IDAT" + idat_data) & 0xFFFFFFFF
            idat = (
                struct.pack(">I", len(idat_data))
                + b"IDAT"
                + idat_data
                + struct.pack(">I", idat_crc)
            )
            iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
            iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
            return sig + ihdr + idat + iend

        png_path = tmp_path / "icon.png"
        png_path.write_bytes(_minimal_png())

        result = verify_assets(str(tmp_path))
        assert result["assets_checked"] == 1
        assert result["invalid"] == []

    def test_verify_assets_empty_file_flagged(self, tmp_path) -> None:
        from backend.app.analysis_job_processor import verify_assets

        (tmp_path / "empty.png").write_bytes(b"")
        result = verify_assets(str(tmp_path))
        assert any(i["reason"] == "empty_file" for i in result["invalid"])


# ---------------------------------------------------------------------------
# /v1/analysis REST endpoints
# ---------------------------------------------------------------------------


class TestAnalysisRoutes:
    def _setup_uploads(self, tmp_path):
        import backend.app.main as m

        m._UPLOADS_DIR = tmp_path / "uploads"
        m._UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        return m._UPLOADS_DIR

    def test_create_job_with_nonexistent_file_returns_404(
        self, client: TestClient, tmp_path
    ) -> None:
        uploads = self._setup_uploads(tmp_path)
        nonexistent = uploads / "nonexistent_abc123.zip"
        resp = client.post(
            "/v1/analysis",
            json={"file_path": str(nonexistent)},
            headers=_AUTH,
        )
        assert resp.status_code == 404

    def test_create_job_missing_file_path_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/analysis", json={}, headers=_AUTH)
        assert resp.status_code == 400

    def test_create_and_get_job(self, client: TestClient, tmp_path) -> None:
        uploads = self._setup_uploads(tmp_path)
        f = uploads / "test.zip"
        f.write_bytes(_make_zip({"a.kt": b"fun a(){}"}))

        resp = client.post("/v1/analysis", json={"file_path": str(f)}, headers=_AUTH)
        assert resp.status_code == 201
        job = resp.json()
        job_id = job["id"]
        assert job["status"] == "queued"

        get_resp = client.get(f"/v1/analysis/{job_id}", headers=_AUTH)
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == job_id

    def test_get_nonexistent_job_returns_404(self, client: TestClient) -> None:
        import uuid

        resp = client.get(f"/v1/analysis/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404

    def test_results_for_pending_job_returns_409(self, client: TestClient, tmp_path) -> None:
        uploads = self._setup_uploads(tmp_path)
        f = uploads / "test2.zip"
        f.write_bytes(_make_zip({"b.kt": b"fun b(){}"}))

        resp = client.post("/v1/analysis", json={"file_path": str(f)}, headers=_AUTH)
        job_id = resp.json()["id"]

        res_resp = client.get(f"/v1/analysis/{job_id}/results", headers=_AUTH)
        assert res_resp.status_code == 409

    def test_delete_job(self, client: TestClient, tmp_path) -> None:
        uploads = self._setup_uploads(tmp_path)
        f = uploads / "test3.zip"
        f.write_bytes(_make_zip({"c.kt": b"fun c(){}"}))

        resp = client.post("/v1/analysis", json={"file_path": str(f)}, headers=_AUTH)
        job_id = resp.json()["id"]

        del_resp = client.delete(f"/v1/analysis/{job_id}", headers=_AUTH)
        assert del_resp.status_code == 204

        get_resp = client.get(f"/v1/analysis/{job_id}", headers=_AUTH)
        assert get_resp.status_code == 404

    def test_analysis_endpoints_require_auth(self, client: TestClient) -> None:
        import uuid

        job_id = str(uuid.uuid4())
        assert client.get(f"/v1/analysis/{job_id}").status_code == 401
        assert client.get(f"/v1/analysis/{job_id}/results").status_code == 401
        assert client.delete(f"/v1/analysis/{job_id}").status_code == 401
        assert client.post("/v1/analysis", json={}).status_code == 401

    def test_file_path_outside_uploads_rejected(self, client: TestClient, tmp_path) -> None:
        self._setup_uploads(tmp_path)
        # A file outside the uploads dir should be rejected.
        outside_file = tmp_path / "sneaky.zip"
        outside_file.write_bytes(_make_zip({"x.kt": b"fun x(){}"}))
        resp = client.post(
            "/v1/analysis",
            json={"file_path": str(outside_file)},
            headers=_AUTH,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# process_analysis_job (unit test with filesystem)
# ---------------------------------------------------------------------------


class TestProcessAnalysisJob:
    def test_process_valid_zip(self, tmp_path, monkeypatch) -> None:
        """
        process_analysis_job should mark the job succeeded for a valid zip.
        Uses BACKEND_DISABLE_JOBS=1 so the processor is called directly.
        """
        import uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.analysis_job_processor import process_analysis_job
        from backend.app.models import AnalysisJob

        zip_bytes = _make_zip(
            {
                "app/src/main/AndroidManifest.xml": b"<manifest/>",
                "app/src/main/java/Main.kt": b"fun main(){}",
            }
        )
        zip_path = tmp_path / "repo.zip"
        zip_path.write_bytes(zip_bytes)

        job_id = str(uuid.uuid4())
        with Session(db_module.get_engine()) as session:
            job = AnalysisJob(id=uuid.UUID(job_id), file_path=str(zip_path), status="queued")
            session.add(job)
            session.commit()

        process_analysis_job(job_id)

        with Session(db_module.get_engine()) as session:
            job = session.get(AnalysisJob, uuid.UUID(job_id))
            assert job is not None
            assert job.status == "succeeded"
            assert job.results_json is not None

    def test_process_missing_file(self, tmp_path) -> None:
        """process_analysis_job marks job failed when file is absent."""
        import uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.analysis_job_processor import process_analysis_job
        from backend.app.models import AnalysisJob

        job_id = str(uuid.uuid4())
        with Session(db_module.get_engine()) as session:
            job = AnalysisJob(
                id=uuid.UUID(job_id),
                file_path="/tmp/does_not_exist_abc.zip",
                status="queued",
            )
            session.add(job)
            session.commit()

        process_analysis_job(job_id)

        with Session(db_module.get_engine()) as session:
            job = session.get(AnalysisJob, uuid.UUID(job_id))
            assert job.status == "failed"
            assert job.errors_json is not None

    def test_process_corrupt_zip(self, tmp_path) -> None:
        """process_analysis_job marks job failed for a corrupt zip."""
        import uuid

        from sqlmodel import Session

        import backend.app.database as db_module
        from backend.app.analysis_job_processor import process_analysis_job
        from backend.app.models import AnalysisJob

        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip!!!")

        job_id = str(uuid.uuid4())
        with Session(db_module.get_engine()) as session:
            job = AnalysisJob(id=uuid.UUID(job_id), file_path=str(zip_path), status="queued")
            session.add(job)
            session.commit()

        process_analysis_job(job_id)

        with Session(db_module.get_engine()) as session:
            job = session.get(AnalysisJob, uuid.UUID(job_id))
            assert job.status == "failed"
            errors = job.errors_json or []
            assert any("corrupt_archive" in str(e) for e in errors)
