"""
UI Blueprint Backend
====================
FastAPI service that accepts Android screen-recording uploads, runs the
ui_blueprint extractor + preview generator in a background thread, and serves
the resulting blueprint JSON and preview PNG frames.

Environment variables
---------------------
API_KEY          Required bearer token for all mutating endpoints.
DATA_DIR         Root directory for session data (default: ./data).
BACKEND_DISABLE_JOBS
                 If set to "1", background extraction jobs are skipped
                 (useful in unit tests to avoid heavy processing).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
API_KEY: str | None = os.environ.get("API_KEY")
DISABLE_JOBS: bool = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"

_executor = ThreadPoolExecutor(max_workers=2)
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="UI Blueprint Backend", version="1.0.0")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    """Validate the Authorization: Bearer <token> header."""
    if not API_KEY:
        # No key configured — allow all requests (dev mode).
        return
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _session_dir(session_id: str) -> Path:
    return DATA_DIR / "sessions" / session_id


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

    except Exception as exc:  # noqa: BLE001
        logger.exception("Extraction job failed for session %s", session_id)
        _write_status(session_id, {"status": "failed", "error": str(exc)})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", status_code=201, dependencies=[Depends(_require_auth)])
async def create_session(
    video: UploadFile,
    meta: str = Form(default=""),
    background_tasks: BackgroundTasks = BackgroundTasks(),  # noqa: B008
) -> JSONResponse:
    """
    Accept a multipart upload (video MP4 + optional meta JSON string).
    Saves files to DATA_DIR/sessions/{session_id}/, creates status.json,
    and enqueues the extraction job.
    """
    session_id = str(uuid.uuid4())
    sdir = _session_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)

    # Persist video.
    clip_path = sdir / "clip.mp4"
    with clip_path.open("wb") as fh:
        content = await video.read()
        fh.write(content)

    # Persist meta.
    try:
        meta_obj = json.loads(meta) if meta.strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"meta is not valid JSON: {exc}") from exc

    with (sdir / "meta.json").open("w", encoding="utf-8") as fh:
        json.dump(meta_obj, fh)

    # Create initial status.
    _write_status(session_id, {"status": "queued"})

    # Enqueue background job (unless disabled for tests).
    if not DISABLE_JOBS:
        _executor.submit(_run_extraction, session_id)

    return JSONResponse(
        content={"session_id": session_id, "status": "queued"},
        status_code=201,
    )


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(_require_auth)])
def get_session(session_id: str) -> JSONResponse:
    """Return the current status.json for the session."""
    return JSONResponse(content=_read_status(session_id))


@app.get("/v1/sessions/{session_id}/blueprint", dependencies=[Depends(_require_auth)])
def get_blueprint(session_id: str) -> FileResponse:
    """Return the blueprint.json file if extraction has completed."""
    bp_path = _session_dir(session_id) / "blueprint.json"
    if not bp_path.exists():
        raise HTTPException(status_code=404, detail="Blueprint not yet available")
    return FileResponse(bp_path, media_type="application/json")


@app.get("/v1/sessions/{session_id}/preview/index", dependencies=[Depends(_require_auth)])
def get_preview_index(session_id: str) -> JSONResponse:
    """Return a JSON listing of available preview PNG filenames and base URL."""
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
    # Validate filename — only allow simple basenames (no path traversal).
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    png_path = _session_dir(session_id) / "preview" / filename
    if not png_path.exists() or not png_path.is_file():
        raise HTTPException(status_code=404, detail="Preview file not found")
    return FileResponse(png_path, media_type="image/png")
