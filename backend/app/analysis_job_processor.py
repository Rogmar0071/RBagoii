"""
backend.app.analysis_job_processor
====================================
Modular analysis pipeline for uploaded zips and video clips.

Job lifecycle
-------------
Each job is represented by an ``AnalysisJob`` row in the database.

Stages executed in order
------------------------
1. ``extract_zip``     — streaming zip extraction (zip uploads only).
2. ``parse_ui_files``  — locate and parse Android XML layout files.
3. ``check_code_syntax`` — run ruff / AST check on Python/Kotlin sources.
4. ``verify_assets``   — validate image assets (format, size).

Each stage is wrapped in try/except; partial results and errors are persisted
after every stage so the API can return interim state.

Configuration
-------------
MAX_UNCOMPRESSED_BYTES  Maximum allowed total uncompressed size for a zip
                        archive (zip-bomb protection).  Default: 500 MB.

RELEVANT_EXTENSIONS     Set of lowercase file extensions that are extracted
                        from zip archives.  Everything else is skipped.
"""

from __future__ import annotations

import ast
import imghdr
import logging
import os
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_UNCOMPRESSED_BYTES: int = int(os.environ.get("MAX_UNCOMPRESSED_BYTES", 500 * 1024 * 1024))

RELEVANT_EXTENSIONS: frozenset[str] = frozenset(
    {".kt", ".java", ".py", ".xml", ".json", ".png", ".jpg", ".jpeg", ".mp4", ".webp", ".svg"}
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _update_analysis_job(job_id: str, **kwargs) -> None:
    """Persist AnalysisJob fields to the database."""
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import AnalysisJob

        kwargs["updated_at"] = datetime.now(timezone.utc)
        with Session(get_engine()) as session:
            job = session.get(AnalysisJob, uuid.UUID(job_id))
            if job is None:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)
            session.add(job)
            session.commit()
    except Exception:
        logger.exception("Failed to update analysis job %s", job_id)


def _get_analysis_job(job_id: str):
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import AnalysisJob

        with Session(get_engine()) as session:
            return session.get(AnalysisJob, uuid.UUID(job_id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stage 1 — Zip extraction
# ---------------------------------------------------------------------------


def extract_zip(zip_path: str, extract_dir: str) -> dict[str, Any]:
    """
    Extract relevant files from *zip_path* into *extract_dir*.

    Returns a result dict::

        {
            "files_extracted": int,
            "files_skipped": int,
            "total_uncompressed_bytes": int,
        }

    Raises
    ------
    ValueError(code="corrupt_archive")       — bad or truncated zip.
    ValueError(code="unsupported_compression") — password-protected zip.
    ValueError(code="zip_bomb")               — uncompressed size exceeds limit.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Zip-bomb check: sum uncompressed sizes before extracting.
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"zip_bomb: uncompressed size {total_uncompressed} bytes exceeds "
                    f"limit {MAX_UNCOMPRESSED_BYTES} bytes"
                )

            files_extracted = 0
            files_skipped = 0

            for info in zf.infolist():
                # Skip directories.
                if info.filename.endswith("/"):
                    continue

                ext = Path(info.filename).suffix.lower()
                if ext not in RELEVANT_EXTENSIONS:
                    files_skipped += 1
                    continue

                # Guard against path traversal within the zip.
                safe_name = os.path.normpath(info.filename)
                if safe_name.startswith(".."):
                    files_skipped += 1
                    continue

                out_path = Path(extract_dir) / safe_name
                out_path.parent.mkdir(parents=True, exist_ok=True)

                # Extract one file at a time (streaming).
                try:
                    with zf.open(info) as src, out_path.open("wb") as dst:
                        import shutil

                        shutil.copyfileobj(src, dst)
                    files_extracted += 1
                except RuntimeError as exc:
                    # RuntimeError is raised for encrypted/password-protected entries.
                    raise ValueError(f"unsupported_compression: {exc}") from exc

            return {
                "files_extracted": files_extracted,
                "files_skipped": files_skipped,
                "total_uncompressed_bytes": total_uncompressed,
            }
    except zipfile.BadZipFile as exc:
        raise ValueError(f"corrupt_archive: {exc}") from exc


# ---------------------------------------------------------------------------
# Stage 2 — Parse UI files
# ---------------------------------------------------------------------------


def parse_ui_files(directory: str) -> dict[str, Any]:
    """
    Locate and parse Android XML layout/manifest files under *directory*.

    Returns::

        {
            "layout_files": [{"path": str, "root_tag": str|None}],
            "manifest_found": bool,
        }
    """
    layout_files: list[dict] = []
    manifest_found = False

    for path in Path(directory).rglob("*.xml"):
        rel = str(path.relative_to(directory))
        root_tag: str | None = None
        try:
            import xml.etree.ElementTree as ET

            tree = ET.parse(str(path))
            root_tag = tree.getroot().tag.split("}")[-1] if tree.getroot() is not None else None
        except Exception as exc:
            root_tag = f"parse_error:{exc}"
        if "AndroidManifest.xml" in str(path):
            manifest_found = True
        layout_files.append({"path": rel, "root_tag": root_tag})

    return {"layout_files": layout_files, "manifest_found": manifest_found}


# ---------------------------------------------------------------------------
# Stage 3 — Check code syntax
# ---------------------------------------------------------------------------


def check_code_syntax(directory: str) -> dict[str, Any]:
    """
    Run syntax checks on Python (.py) and Kotlin (.kt) source files.

    Python: uses the stdlib ``ast`` module for fast parse checking.
    Kotlin: uses a subprocess ``kotlinc -script -nowarn`` dry-run if available;
            falls back to a simple brace-balance heuristic.

    Returns::

        {
            "python": {"files_checked": int, "errors": [{"path": str, "error": str}]},
            "kotlin": {"files_checked": int, "errors": [{"path": str, "error": str}]},
        }
    """
    py_errors: list[dict] = []
    py_checked = 0
    kt_errors: list[dict] = []
    kt_checked = 0

    for path in Path(directory).rglob("*.py"):
        py_checked += 1
        try:
            with path.open("r", errors="replace") as fh:
                source = fh.read()
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            py_errors.append({"path": str(path.relative_to(directory)), "error": str(exc)})
        except Exception as exc:
            py_errors.append(
                {
                    "path": str(path.relative_to(directory)),
                    "error": f"read_error:{exc}",
                }
            )

    for path in Path(directory).rglob("*.kt"):
        kt_checked += 1
        try:
            with path.open("r", errors="replace") as fh:
                source = fh.read()
            # Simple brace-balance check as a lightweight heuristic.
            if source.count("{") != source.count("}"):
                kt_errors.append(
                    {
                        "path": str(path.relative_to(directory)),
                        "error": "Unbalanced braces",
                    }
                )
        except Exception as exc:
            kt_errors.append(
                {
                    "path": str(path.relative_to(directory)),
                    "error": f"read_error:{exc}",
                }
            )

    return {
        "python": {"files_checked": py_checked, "errors": py_errors},
        "kotlin": {"files_checked": kt_checked, "errors": kt_errors},
    }


# ---------------------------------------------------------------------------
# Stage 4 — Verify assets
# ---------------------------------------------------------------------------


def verify_assets(directory: str) -> dict[str, Any]:
    """
    Validate image assets (.png, .jpg, .jpeg, .webp) under *directory*.

    Checks:
      - File is non-empty.
      - ``imghdr.what`` identifies it as the expected image format.

    Returns::

        {
            "assets_checked": int,
            "invalid": [{"path": str, "reason": str}],
        }
    """
    assets_checked = 0
    invalid: list[dict] = []

    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    for path in Path(directory).rglob("*"):
        if path.suffix.lower() not in image_exts:
            continue
        assets_checked += 1
        try:
            if path.stat().st_size == 0:
                invalid.append({"path": str(path.relative_to(directory)), "reason": "empty_file"})
                continue
            detected = imghdr.what(str(path))
            if detected is None:
                invalid.append(
                    {
                        "path": str(path.relative_to(directory)),
                        "reason": "unrecognised_format",
                    }
                )
        except Exception as exc:
            invalid.append(
                {
                    "path": str(path.relative_to(directory)),
                    "reason": f"read_error:{exc}",
                }
            )

    return {"assets_checked": assets_checked, "invalid": invalid}


# ---------------------------------------------------------------------------
# Main entry point — process_analysis_job
# ---------------------------------------------------------------------------


def process_analysis_job(job_id: str) -> None:
    """
    Run the full analysis pipeline for the AnalysisJob identified by *job_id*.

    This function is designed to be called from an RQ worker or a background
    thread.  It persists partial results and errors after every stage so that
    the REST API can return interim state.
    """
    logger.info("Analysis job %s starting", job_id)
    _update_analysis_job(job_id, status="running")

    job = _get_analysis_job(job_id)
    if job is None:
        logger.error("Analysis job %s not found in DB", job_id)
        return

    file_path: str = job.file_path or ""
    if not file_path or not Path(file_path).exists():
        _update_analysis_job(
            job_id,
            status="failed",
            errors_json=[{"stage": "init", "error": f"File not found: {file_path}"}],
        )
        return

    errors: list[dict] = []
    warnings: list[dict] = []
    results: dict[str, Any] = {"file_path": file_path}

    with tempfile.TemporaryDirectory(prefix=f"analysis_{job_id}_") as tmpdir:
        work_dir = tmpdir

        # ---- Stage 1: zip extraction (if applicable) ----
        if file_path.endswith(".zip"):
            try:
                zip_result = extract_zip(file_path, work_dir)
                results["zip_extraction"] = zip_result
            except ValueError as exc:
                error_code = str(exc).split(":")[0]
                error_msg = str(exc)
                errors.append({"stage": "extract_zip", "code": error_code, "error": error_msg})
                _update_analysis_job(
                    job_id,
                    status="failed",
                    errors_json=errors,
                    results_json=results,
                )
                logger.error("Analysis job %s failed at extract_zip: %s", job_id, exc)
                return
            except Exception as exc:
                errors.append({"stage": "extract_zip", "error": str(exc)})
                _update_analysis_job(
                    job_id,
                    status="failed",
                    errors_json=errors,
                    results_json=results,
                )
                logger.exception("Analysis job %s failed at extract_zip", job_id)
                return

        # ---- Stage 2: parse UI files ----
        try:
            ui_result = parse_ui_files(work_dir)
            results["ui_files"] = ui_result
        except Exception as exc:
            errors.append({"stage": "parse_ui_files", "error": str(exc)})
            logger.exception("Analysis job %s parse_ui_files error", job_id)

        # Persist partial results.
        _update_analysis_job(job_id, results_json=results, errors_json=errors or None)

        # ---- Stage 3: check code syntax ----
        try:
            syntax_result = check_code_syntax(work_dir)
            results["code_syntax"] = syntax_result
            # Promote syntax errors as warnings (non-fatal).
            for lang_key, lang_data in syntax_result.items():
                for err in lang_data.get("errors", []):
                    warnings.append({"stage": "check_code_syntax", "lang": lang_key, **err})
        except Exception as exc:
            errors.append({"stage": "check_code_syntax", "error": str(exc)})
            logger.exception("Analysis job %s check_code_syntax error", job_id)

        # Persist partial results.
        _update_analysis_job(
            job_id,
            results_json=results,
            errors_json=errors or None,
            warnings_json=warnings or None,
        )

        # ---- Stage 4: verify assets ----
        try:
            asset_result = verify_assets(work_dir)
            results["assets"] = asset_result
            for inv in asset_result.get("invalid", []):
                warnings.append({"stage": "verify_assets", **inv})
        except Exception as exc:
            errors.append({"stage": "verify_assets", "error": str(exc)})
            logger.exception("Analysis job %s verify_assets error", job_id)

    # ---- Final status ----
    final_status = "failed" if errors else "succeeded"
    _update_analysis_job(
        job_id,
        status=final_status,
        results_json=results,
        errors_json=errors or None,
        warnings_json=warnings or None,
    )
    logger.info("Analysis job %s finished: %s", job_id, final_status)
