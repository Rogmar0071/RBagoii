"""
UI Blueprint Backend
====================
FastAPI service that accepts Android screen-recording uploads, runs the
ui_blueprint extractor + preview generator in a background thread, and serves
the resulting blueprint JSON and preview PNG frames.

Environment variables
---------------------
API_KEY          Required bearer token for all mutating endpoints.
                 NOTE: this is the service access token, not the OpenAI key.
DATA_DIR         Root directory for session data (default: ./data).
BACKEND_DISABLE_JOBS
                 If set to "1", background extraction jobs are skipped
                 (useful in unit tests to avoid heavy processing).
MAX_UPLOAD_BYTES Maximum allowed upload size in bytes (default: 52428800 = 50 MB).
OPENAI_API_KEY   (Optional) Server-side OpenAI credential — enables AI-backed
                 domain derivation and /api/chat.  Never returned to clients.
OPENAI_MODEL_DOMAIN  (Optional) Model for domain derivation (default: gpt-4.1-mini).
OPENAI_MODEL_CHAT    (Optional) Model for /api/chat (default: gpt-4.1-mini).
OPENAI_BASE_URL      (Optional) OpenAI base URL (default: https://api.openai.com).
OPENAI_TIMEOUT_SECONDS (Optional) Request timeout in seconds (default: 30).
SLACK_WEBHOOK_URL    (Optional) Slack incoming-webhook URL for critical-failure alerts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
API_KEY: str | None = os.environ.get("API_KEY")
DISABLE_JOBS: bool = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))  # 50 MB

# Allowed MIME types for upload
_ALLOWED_CONTENT_TYPES = {"video/mp4", "application/zip", "application/x-zip-compressed"}

# Upload directory for streamed files
_UPLOADS_DIR = Path("/tmp/uploads")

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="UI Blueprint Backend", version="1.0.0")

# Domain Profile + Blueprint Compiler routes (no auth required — public API).
from backend.app.analysis_routes import router as _analysis_router  # noqa: E402
from backend.app.bridge_routes import router as _bridge_router  # noqa: E402
from backend.app.chat_file_routes import router as _chat_file_router  # noqa: E402
from backend.app.chat_routes import router as _chat_router  # noqa: E402
from backend.app.domain_routes import router as _domain_router  # noqa: E402
from backend.app.folder_routes import router as _folder_router  # noqa: E402
from backend.app.github_routes import router as _github_router  # noqa: E402
from backend.app.ingest_routes import router as _ingest_router  # noqa: E402
from backend.app.mutation_routes import router as _mutation_router  # noqa: E402
from backend.app.ops_routes import router as _ops_router  # noqa: E402
from backend.app.simulation_routes import router as _simulation_router  # noqa: E402
from backend.app.tool_routes import router as _tool_router  # noqa: E402

app.include_router(_domain_router)
app.include_router(_chat_router)
app.include_router(_chat_file_router)
app.include_router(_github_router)
app.include_router(_folder_router)
app.include_router(_ops_router)
app.include_router(_tool_router)
app.include_router(_analysis_router)
app.include_router(_mutation_router)
app.include_router(_simulation_router)
app.include_router(_bridge_router)
app.include_router(_ingest_router)


# ---------------------------------------------------------------------------
# Global exception handler — logs with context and optionally alerts Slack
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    job_id = request.headers.get("X-Job-Id")
    user_context = request.headers.get("Authorization", "")[:20] or "anonymous"
    logger.exception(
        "Unhandled exception | job_id=%s user=%s path=%s",
        job_id,
        user_context,
        request.url.path,
        exc_info=exc,
    )
    _maybe_slack_alert(
        f"Unhandled exception on {request.method} {request.url.path}: {type(exc).__name__}: {exc}"
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "An unexpected error occurred."}},
    )


def _maybe_slack_alert(message: str) -> None:
    """Post a message to SLACK_WEBHOOK_URL if configured (best-effort, non-blocking)."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        import httpx  # already a dep via httpx

        httpx.post(webhook_url, json={"text": message}, timeout=5)
    except Exception:
        pass  # Never let alerting failures surface to callers


# ---------------------------------------------------------------------------
# Startup: initialise DB tables + ensure upload dir exists + start cleanup
# ---------------------------------------------------------------------------


def _cleanup_old_uploads() -> None:
    """Delete stale files in upload and staging directories every hour."""
    max_age_seconds = 24 * 3600
    staging_dir = Path(os.environ.get("INGEST_STAGING_DIR", "/tmp/ingest_staging"))
    while True:
        time.sleep(3600)  # run every hour
        for directory in (_UPLOADS_DIR, staging_dir):
            try:
                if directory.exists():
                    cutoff = time.time() - max_age_seconds
                    for f in directory.iterdir():
                        try:
                            if f.is_file() and f.stat().st_mtime < cutoff:
                                f.unlink(missing_ok=True)
                                logger.info("Cleaned up old upload: %s", f.name)
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning("Upload cleanup error for %s: %s", directory, exc)


@app.on_event("startup")
def _startup_init_db() -> None:
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        try:
            from backend.app.database import init_db

            init_db()
            logger.info("Database tables initialised.")
        except Exception as exc:
            logger.warning("DB init failed (non-fatal): %s", exc)

    # Start the background cleanup daemon thread (non-blocking).
    _cleanup_thread = threading.Thread(
        target=_cleanup_old_uploads, daemon=True, name="upload-cleanup"
    )
    _cleanup_thread.start()


# ---------------------------------------------------------------------------
# Health / root
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> dict:
    """Service health check — no auth required (used by Render and load-balancers)."""
    return {"ok": True, "service": "ui-blueprint-backend", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    """Validate the Authorization: Bearer <token> header.

    Raises
    ------
    RuntimeError
        If API_KEY is not configured (``no_open_mode`` invariant).
    HTTPException 401 / 403
        For missing or invalid tokens.
    """
    if not API_KEY:
        raise RuntimeError(
            "AUTHENTICATION_NOT_CONFIGURED: API_KEY environment variable is not set. "
            "Set API_KEY to a strong secret before deployment."
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _validate_session_id(session_id: str) -> str:
    """Raise HTTP 400 if session_id is not a valid UUID (prevents path traversal)."""
    if not _UUID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    return session_id


def _validate_filename(filename: str) -> str:
    """Raise HTTP 400 if filename contains unsafe characters or patterns."""
    if not _SAFE_FILENAME_RE.match(filename) or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return filename


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _sessions_root() -> Path:
    """Return the canonical absolute path of the sessions root directory."""
    return (DATA_DIR / "sessions").resolve()


def _session_dir(session_id: str) -> Path:
    """
    Return the resolved, safe absolute path for a session directory.

    Uses Path().name to strip any directory separators from session_id, then
    verifies the resolved path is contained within the sessions root
    (defence-in-depth on top of UUID regex validation).
    """
    root = _sessions_root()
    # Path().name strips any directory separators — only the final component is used.
    safe_id = Path(session_id).name
    candidate = (root / safe_id).resolve()
    # Ensure the resolved path is directly inside root (not a parent or sibling).
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session id") from None
    return candidate


def _read_status(session_id: str) -> dict[str, Any]:
    status_file = _session_dir(session_id) / "status.json"
    if not status_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    with status_file.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_status(session_id: str, data: dict[str, Any]) -> None:
    status_file = _session_dir(session_id) / "status.json"
    with status_file.open("w", encoding="utf-8") as fh:
        json.dump(data, fh)


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


def _run_extraction(session_id: str) -> None:
    """Run extraction + preview in a background thread, updating status.json."""
    sdir = _session_dir(session_id)
    clip = sdir / "clip.mp4"
    blueprint = sdir / "blueprint.json"
    preview_dir = sdir / "preview"
    preview_dir.mkdir(exist_ok=True)

    try:
        _write_status(session_id, {"status": "running", "progress": 0})

        # Run extractor.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ui_blueprint",
                "extract",
                str(clip),
                "-o",
                str(blueprint),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Extraction failed: {result.stderr.strip()}")

        _write_status(session_id, {"status": "running", "progress": 50})

        # Run preview generator.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ui_blueprint",
                "preview",
                str(blueprint),
                "--out",
                str(preview_dir),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Preview generation failed: {result.stderr.strip()}")

        _write_status(session_id, {"status": "done", "progress": 100})

    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        logger.exception("Extraction job failed for session %s", session_id)
        _write_status(session_id, {"status": "failed", "error": str(exc)})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", status_code=201, dependencies=[Depends(_require_auth)])
async def create_session(
    video: UploadFile,
    meta: str = Form(default=""),
    background_tasks: BackgroundTasks = None,  # noqa: RUF009 — injected by FastAPI
) -> JSONResponse:
    """
    Accept a multipart upload (video MP4 + optional meta JSON string).
    Streams file directly to /tmp/uploads/<uuid>.ext, validates MIME type and
    size, saves session metadata, and enqueues the extraction job.
    """
    # --- MIME type validation ---
    content_type = (video.content_type or "").split(";")[0].strip()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{content_type}'. Allowed: video/mp4, application/zip",
        )

    ext = ".zip" if "zip" in content_type else ".mp4"
    upload_uuid = str(uuid.uuid4())
    tmp_path = _UPLOADS_DIR / f"{upload_uuid}{ext}"

    # --- Stream file to /tmp/uploads/ with size enforcement ---
    total_bytes = 0
    try:
        with tmp_path.open("wb") as fh:
            while True:
                chunk = await video.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    fh.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds maximum allowed size of {MAX_UPLOAD_BYTES} bytes",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        logger.exception("Failed to stream upload to disk")
        raise HTTPException(status_code=500, detail="Failed to save uploaded file") from exc

    session_id = str(uuid.uuid4())
    sdir = _session_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)

    # Copy from /tmp/uploads to session dir
    clip_path = sdir / f"clip{ext}"
    shutil.copy2(str(tmp_path), str(clip_path))

    # Persist meta.
    try:
        meta_obj = json.loads(meta) if meta.strip() else {}
    except json.JSONDecodeError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"meta is not valid JSON: {exc}") from exc

    with (sdir / "meta.json").open("w", encoding="utf-8") as fh:
        json.dump({**meta_obj, "upload_file": str(tmp_path), "file_size_bytes": total_bytes}, fh)

    # Create initial status.
    _write_status(session_id, {"status": "queued", "upload_file": str(tmp_path)})

    # Enqueue background job (unless disabled for tests).
    if not DISABLE_JOBS:
        _enqueue_extraction(session_id, str(clip_path))

    return JSONResponse(
        content={"session_id": session_id, "status": "queued"},
        status_code=201,
    )


def _enqueue_extraction(session_id: str, clip_path: str) -> None:
    """Enqueue extraction via RQ if available, otherwise run in a thread."""
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        try:
            from redis import Redis
            from rq import Queue as RQueue

            from backend.app.jobs import run_extraction_job
            from backend.app.worker import _assert_importable

            _assert_importable(run_extraction_job)
            conn = Redis.from_url(redis_url)
            q = RQueue("default", connection=conn)
            # CONTRACT: MQP-CONTRACT:RQ_EXECUTION_SPINE_LOCK_V4 §3
            # Queue stores stable string path, never a function object.
            q.enqueue(
                "backend.app.job_runner.execute_job",
                "run_extraction_job",
                session_id,
                job_timeout=1800,
            )
            return
        except Exception as exc:
            logger.warning("RQ unavailable (%s); falling back to thread pool.", exc)
    # Fallback: thread
    t = threading.Thread(
        target=_run_extraction,
        args=(session_id,),
        daemon=True,
        name=f"extract-{session_id}",
    )
    t.start()


# ---------------------------------------------------------------------------
# Chunked upload endpoints
# ---------------------------------------------------------------------------

# In-memory registry: upload_id → {chunks: {index: bytes}, total: int, content_type: str}
# For production this should be backed by Redis or the filesystem; for now we use a
# thread-safe dict since workers may run on separate processes (use filesystem variant).
_chunk_registry_lock = threading.Lock()


def _chunks_dir(upload_id: str) -> Path:
    """Return the chunk directory for *upload_id*, confined to _UPLOADS_DIR/chunks/."""
    chunks_root = (_UPLOADS_DIR / "chunks").resolve()
    candidate = (chunks_root / upload_id).resolve()
    try:
        candidate.relative_to(chunks_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


@app.post("/v1/sessions/chunks", status_code=202, dependencies=[Depends(_require_auth)])
async def upload_chunk(
    chunk: UploadFile,
    x_upload_id: str = Header(..., alias="X-Upload-Id"),
    x_chunk_index: int = Header(..., alias="X-Chunk-Index"),
    x_total_chunks: int = Header(..., alias="X-Total-Chunks"),
) -> JSONResponse:
    """
    Accept a single chunk of a file being uploaded in parts.

    Headers required:
      X-Upload-Id      — stable UUID for this chunked upload session
      X-Chunk-Index    — 0-based index of this chunk
      X-Total-Chunks   — total number of chunks expected

    The client sends all chunks in any order; when all X-Total-Chunks chunks have
    been received, call PUT /v1/sessions/chunks/{upload_id}/finalize to assemble
    them and create the session.
    """
    if not _UUID_RE.match(x_upload_id):
        raise HTTPException(status_code=400, detail="X-Upload-Id must be a valid UUID")
    if x_chunk_index < 0 or x_total_chunks < 1 or x_chunk_index >= x_total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk index or total")

    # _chunks_dir includes a path-confinement check — result is always within _UPLOADS_DIR.
    safe_chunks_dir = _chunks_dir(x_upload_id)
    chunk_data = await chunk.read()
    chunk_path = safe_chunks_dir / f"chunk_{x_chunk_index:05d}"
    with chunk_path.open("wb") as fh:
        fh.write(chunk_data)

    # Count how many distinct chunks are present.
    present = len(list(safe_chunks_dir.glob("chunk_*")))

    # Save metadata so finalize knows the total.
    meta_path = safe_chunks_dir / "_meta.json"
    with meta_path.open("w") as fh:
        json.dump(
            {"total_chunks": x_total_chunks, "content_type": chunk.content_type or ""},
            fh,
        )

    return JSONResponse(
        content={
            "upload_id": x_upload_id,
            "chunk_index": x_chunk_index,
            "chunks_received": present,
            "total_chunks": x_total_chunks,
            "complete": present >= x_total_chunks,
        },
        status_code=202,
    )


@app.put(
    "/v1/sessions/chunks/{upload_id}/finalize",
    status_code=201,
    dependencies=[Depends(_require_auth)],
)
async def finalize_chunked_upload(
    upload_id: str,
    meta: str = Form(default=""),
) -> JSONResponse:
    """
    Assemble all received chunks into a complete file and create a session.

    Returns the same shape as POST /v1/sessions on success.
    """
    if not _UUID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    # Confine the chunk directory to _UPLOADS_DIR/chunks/ to prevent path traversal.
    chunks_root = (_UPLOADS_DIR / "chunks").resolve()
    chunk_dir = (chunks_root / upload_id).resolve()
    try:
        chunk_dir.relative_to(chunks_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id") from None
    meta_path = chunk_dir / "_meta.json"
    if not chunk_dir.exists() or not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found — no chunks received")

    with meta_path.open() as fh:
        upload_meta = json.load(fh)
    total_chunks: int = upload_meta["total_chunks"]
    content_type: str = upload_meta.get("content_type", "")

    # Validate all chunks are present.
    chunk_files = sorted(chunk_dir.glob("chunk_*"))
    if len(chunk_files) < total_chunks:
        raise HTTPException(
            status_code=409,
            detail=f"Only {len(chunk_files)}/{total_chunks} chunks received",
        )

    ext = ".zip" if "zip" in content_type else ".mp4"
    assembled_path = _UPLOADS_DIR / f"{upload_id}{ext}"

    try:
        with assembled_path.open("wb") as out_fh:
            total_bytes = 0
            for cf in chunk_files:
                data = cf.read_bytes()
                total_bytes += len(data)
                if total_bytes > MAX_UPLOAD_BYTES:
                    assembled_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Assembled file exceeds maximum allowed size of "
                            f"{MAX_UPLOAD_BYTES} bytes"
                        ),
                    )
                out_fh.write(data)
    except HTTPException:
        raise
    except Exception as exc:
        assembled_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to assemble chunks") from exc
    finally:
        # Clean up chunk directory.
        shutil.rmtree(str(chunk_dir), ignore_errors=True)

    session_id = str(uuid.uuid4())
    sdir = _session_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)

    clip_path = sdir / f"clip{ext}"
    shutil.copy2(str(assembled_path), str(clip_path))

    try:
        meta_obj = json.loads(meta) if meta.strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"meta is not valid JSON: {exc}") from exc

    with (sdir / "meta.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {**meta_obj, "upload_file": str(assembled_path), "file_size_bytes": total_bytes},
            fh,
        )

    _write_status(session_id, {"status": "queued", "upload_file": str(assembled_path)})

    if not DISABLE_JOBS:
        _enqueue_extraction(session_id, str(clip_path))

    return JSONResponse(
        content={"session_id": session_id, "status": "queued"},
        status_code=201,
    )


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(_require_auth)])
def get_session(session_id: str) -> JSONResponse:
    """Return the current status.json for the session."""
    _validate_session_id(session_id)
    return JSONResponse(content=_read_status(session_id))


@app.get("/v1/sessions/{session_id}/blueprint", dependencies=[Depends(_require_auth)])
def get_blueprint(session_id: str) -> FileResponse:
    """Return the blueprint.json file if extraction has completed."""
    _validate_session_id(session_id)
    bp_path = _session_dir(session_id) / "blueprint.json"
    if not bp_path.exists():
        raise HTTPException(status_code=404, detail="Blueprint not yet available")
    return FileResponse(bp_path, media_type="application/json")


@app.get("/v1/sessions/{session_id}/preview/index", dependencies=[Depends(_require_auth)])
def get_preview_index(session_id: str) -> JSONResponse:
    """Return a JSON listing of available preview PNG filenames and base URL."""
    _validate_session_id(session_id)
    sdir = _session_dir(session_id)
    if not sdir.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    preview_dir = sdir / "preview"
    if not preview_dir.exists():
        return JSONResponse(
            content={
                "base_url": f"/v1/sessions/{session_id}/preview",
                "files": [],
            }
        )
    files = sorted(p.name for p in preview_dir.glob("*.png"))
    return JSONResponse(
        content={
            "base_url": f"/v1/sessions/{session_id}/preview",
            "files": files,
        }
    )


@app.get(
    "/v1/sessions/{session_id}/preview/{filename}",
    dependencies=[Depends(_require_auth)],
)
def get_preview_file(session_id: str, filename: str) -> FileResponse:
    """Serve an individual PNG preview frame."""
    _validate_session_id(session_id)
    _validate_filename(filename)
    preview_dir = _session_dir(session_id) / "preview"
    # Path().name strips directory separators from the filename before joining.
    safe_filename = Path(filename).name
    png_path = (preview_dir / safe_filename).resolve()
    try:
        png_path.relative_to(preview_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename") from None
    if not png_path.exists() or not png_path.is_file():
        raise HTTPException(status_code=404, detail="Preview file not found")
    return FileResponse(png_path, media_type="image/png")
