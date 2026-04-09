"""
backend.app.worker
==================
Background job functions for RQ (Redis Queue) workers.

Each function is designed to be enqueued via ``rq`` but also callable
directly for synchronous execution (tests / DISABLE_JOBS mode).

Job types
---------
analyze    Run ``ui_blueprint extract`` on the clip stored in R2, then
           upload the resulting blueprint JSON + Markdown to R2, and
           create the corresponding Artifact rows.

blueprint  Compile a blueprint from an existing analysis_json artifact,
           producing blueprint_json + blueprint_md artifacts.

Environment
-----------
REDIS_URL              Redis / Valkey connection URL (e.g. redis://localhost:6379/0).
                       When absent, jobs are executed synchronously in a thread.
BACKEND_DISABLE_JOBS   If "1", skip job execution entirely (for unit tests).
DATABASE_URL           Required by the job to persist status updates.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RQ integration helpers
# ---------------------------------------------------------------------------


def _redis_queue(name: str = "default"):
    """Return an RQ Queue connected to REDIS_URL, or None if not configured."""
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(redis_url)
        return Queue(name, connection=conn)
    except Exception as exc:  # pragma: no cover – connection errors in prod
        logger.warning("RQ unavailable (%s); will run jobs synchronously.", exc)
        return None


def enqueue_job(job_id: str, job_type: str) -> Optional[str]:
    """
    Enqueue *job_type* for *job_id*.

    Returns the RQ job ID string on success, or ``None`` when running
    synchronously (no Redis) or when BACKEND_DISABLE_JOBS=1.

    When BACKEND_DISABLE_JOBS is set the job function is called directly
    on the current thread so tests get predictable behaviour.
    """
    disable = os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        return None

    q = _redis_queue()
    if q is not None:
        fn = _JOB_FUNCTIONS.get(job_type)
        if fn is None:
            raise ValueError(f"Unknown job type: {job_type!r}")
        rq_job = q.enqueue(fn, job_id)
        return rq_job.id

    # No Redis – run synchronously in a thread pool (same behaviour as the
    # legacy sessions implementation).
    from concurrent.futures import ThreadPoolExecutor

    fn = _JOB_FUNCTIONS.get(job_type)
    if fn is None:
        raise ValueError(f"Unknown job type: {job_type!r}")
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(fn, job_id)
    executor.shutdown(wait=False)
    return None


# ---------------------------------------------------------------------------
# Shared DB helpers (used inside job functions)
# ---------------------------------------------------------------------------


def _update_job(job_id: str, **kwargs) -> None:
    """Persist job-status fields to the database."""
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    kwargs["updated_at"] = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        for k, v in kwargs.items():
            setattr(job, k, v)
        session.add(job)
        session.commit()


def _get_job(job_id: str):
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Job

    with Session(get_engine()) as session:
        return session.get(Job, uuid.UUID(job_id))


def _get_folder(folder_id: str):
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Folder

    with Session(get_engine()) as session:
        return session.get(Folder, uuid.UUID(folder_id))


def _create_artifact(folder_id: str, artifact_type: str, object_key: str) -> None:
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Artifact

    with Session(get_engine()) as session:
        artifact = Artifact(
            folder_id=uuid.UUID(folder_id),
            type=artifact_type,
            object_key=object_key,
        )
        session.add(artifact)
        session.commit()


def _update_folder_status(folder_id: str, status: str) -> None:
    from sqlmodel import Session

    from backend.app.database import get_engine
    from backend.app.models import Folder

    with Session(get_engine()) as session:
        folder = session.get(Folder, uuid.UUID(folder_id))
        if folder is None:
            return
        folder.status = status
        folder.updated_at = datetime.now(timezone.utc)
        session.add(folder)
        session.commit()


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------


def run_analyze(job_id: str) -> None:
    """
    Download the clip from R2, run ``ui_blueprint extract``, upload outputs.

    Updates the ``jobs`` row with running/succeeded/failed status throughout.
    Also uploads blueprint JSON to R2 and creates Artifact rows.
    """
    job = _get_job(job_id)
    if job is None:
        logger.error("run_analyze: job %s not found", job_id)
        return

    folder_id = str(job.folder_id)
    _update_job(job_id, status="running", progress=5)
    _update_folder_status(folder_id, "running")

    try:
        from backend.app import storage

        folder = _get_folder(folder_id)
        if folder is None or not folder.clip_object_key:
            raise RuntimeError("Folder has no clip to analyze")

        # Download clip from R2 to a temp file.
        clip_bytes = storage.get_object_bytes(folder.clip_object_key)
        if clip_bytes is None:
            raise RuntimeError(f"Clip not found in storage: {folder.clip_object_key}")

        _update_job(job_id, progress=15)

        with tempfile.TemporaryDirectory() as tmpdir:
            clip_path = os.path.join(tmpdir, "clip.mp4")
            blueprint_path = os.path.join(tmpdir, "blueprint.json")

            with open(clip_path, "wb") as fh:
                fh.write(clip_bytes)

            _update_job(job_id, progress=20)

            # Run extractor.
            result = subprocess.run(
                [sys.executable, "-m", "ui_blueprint", "extract", clip_path, "-o", blueprint_path],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Extraction failed: {result.stderr.strip()}")

            _update_job(job_id, progress=70)

            # Upload blueprint JSON to R2.
            with open(blueprint_path, "rb") as fh:
                bp_bytes = fh.read()

            bp_key = storage.upload_bytes(
                folder_id, "blueprint.json", bp_bytes, "application/json"
            )
            _create_artifact(folder_id, "blueprint_json", bp_key)

            _update_job(job_id, progress=90)

            # Also upload a Markdown version if available.
            md_path = blueprint_path.replace(".json", ".md")
            if os.path.exists(md_path):
                with open(md_path, "rb") as fh:
                    md_bytes = fh.read()
                md_key = storage.upload_bytes(
                    folder_id, "blueprint.md", md_bytes, "text/markdown"
                )
                _create_artifact(folder_id, "blueprint_md", md_key)

        _update_job(job_id, status="succeeded", progress=100)
        _update_folder_status(folder_id, "done")

    except Exception as exc:
        logger.exception("run_analyze failed for job %s", job_id)
        _update_job(job_id, status="failed", error=str(exc))
        _update_folder_status(folder_id, "failed")


def run_blueprint(job_id: str) -> None:
    """
    Compile a blueprint from an existing analysis JSON artifact.

    Looks for the most recent ``blueprint_json`` artifact in R2, re-runs
    ``ui_blueprint preview`` to regenerate outputs, and uploads any new files.
    """
    job = _get_job(job_id)
    if job is None:
        logger.error("run_blueprint: job %s not found", job_id)
        return

    folder_id = str(job.folder_id)
    _update_job(job_id, status="running", progress=10)

    try:
        from sqlmodel import Session, select

        from backend.app import storage
        from backend.app.database import get_engine
        from backend.app.models import Artifact

        # Find the latest blueprint_json artifact.
        with Session(get_engine()) as session:
            artifact = session.exec(
                select(Artifact)
                .where(Artifact.folder_id == uuid.UUID(folder_id))
                .where(Artifact.type == "blueprint_json")
                .order_by(Artifact.created_at.desc())
            ).first()

        if artifact is None:
            raise RuntimeError("No blueprint_json artifact found; run analyze first.")

        bp_bytes = storage.get_object_bytes(artifact.object_key)
        if bp_bytes is None:
            raise RuntimeError(f"Blueprint JSON not found in storage: {artifact.object_key}")

        _update_job(job_id, progress=40)

        with tempfile.TemporaryDirectory() as tmpdir:
            blueprint_path = os.path.join(tmpdir, "blueprint.json")
            preview_dir = os.path.join(tmpdir, "preview")
            os.makedirs(preview_dir)

            with open(blueprint_path, "wb") as fh:
                fh.write(bp_bytes)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ui_blueprint",
                    "preview",
                    blueprint_path,
                    "--out",
                    preview_dir,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Preview failed: {result.stderr.strip()}")

            _update_job(job_id, progress=80)

            # Upload preview PNGs as artifacts.
            for fname in os.listdir(preview_dir):
                if not fname.endswith(".png"):
                    continue
                with open(os.path.join(preview_dir, fname), "rb") as fh:
                    png_bytes = fh.read()
                key = storage.upload_bytes(folder_id, f"preview/{fname}", png_bytes, "image/png")
                _create_artifact(folder_id, "preview_png", key)

        _update_job(job_id, status="succeeded", progress=100)

    except Exception as exc:
        logger.exception("run_blueprint failed for job %s", job_id)
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# Job-function registry
# ---------------------------------------------------------------------------

_JOB_FUNCTIONS = {
    "analyze": run_analyze,
    "blueprint": run_blueprint,
}
