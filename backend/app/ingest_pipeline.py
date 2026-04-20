"""
backend.app.ingest_pipeline
============================
Unified, stable ingestion pipeline — single code path for all source kinds.

Replaces
--------
- analysis_job_processor.py   (zip / Android-artifact analysis)
- worker.py:run_repo_ingestion (GitHub ingestion worker, async/sync dance)
- github_routes.py ingestion helper (_fetch_repo_file_list)
- document_routes.py          (incomplete parallel implementation)

Architecture
------------
All ingestion flows through one entry point::

    process_ingest_job(job_id: str)
           │
           ├─ kind="file" → read staging file → extract_text → chunk → store
           ├─ kind="url"  → httpx.get → html.parser strip → chunk → store
           └─ kind="repo" → GitHub Trees API → per-file extract → chunk → store

LAW: ALWAYS_UPDATE_TERMINAL
    ``process_ingest_job`` unconditionally writes a terminal status
    (``success`` or ``failed``) to the DB, even if an unexpected exception
    is raised.  Callers never need to handle partial state.

Extraction support
------------------
Format    Library              Fallback
--------- -------------------- ---------------------------------
PDF       pypdf (optional)     warn + return None
DOCX      python-docx (opt.)   warn + return None
HTML      stdlib html.parser   always available
CSV       stdlib csv           always available
ZIP       stdlib zipfile       recurse on extractable members
text/code UTF-8 decode         always available

Chunking
--------
CHUNK_MAX_CHARS      env var, default 1500
CHUNK_OVERLAP_CHARS  env var, default 150

Chunks are split on line boundaries; the overlap window carries the
trailing *CHUNK_OVERLAP_CHARS* characters of the previous chunk forward
so context is not lost at boundaries.

GitHub ingestion
----------------
Uses the ``/git/trees/{branch}?recursive=1`` endpoint (single request for
the full tree), then fetches raw content from raw.githubusercontent.com.

Env vars
--------
GITHUB_TOKEN          GitHub personal-access token (optional)
REPO_MAX_FILES        Maximum files to ingest per repo  (default 200)
REPO_MAX_FILE_CHARS   Maximum characters per file       (default 50000)
MAX_UNCOMPRESSED_BYTES ZIP-bomb protection limit        (default 500 MB)
CHUNK_MAX_CHARS       Chunk size in characters          (default 1500)
CHUNK_OVERLAP_CHARS   Overlap between chunks            (default 150)
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

CHUNK_MAX_CHARS: int = int(os.environ.get("CHUNK_MAX_CHARS", "1500"))
CHUNK_OVERLAP_CHARS: int = int(os.environ.get("CHUNK_OVERLAP_CHARS", "150"))
REPO_MAX_FILES: int = int(os.environ.get("REPO_MAX_FILES", "200"))
REPO_MAX_FILE_CHARS: int = int(os.environ.get("REPO_MAX_FILE_CHARS", "50000"))
MAX_UNCOMPRESSED_BYTES: int = int(
    os.environ.get("MAX_UNCOMPRESSED_BYTES", str(500 * 1024 * 1024))
)
_URL_FETCH_MAX_BYTES: int = 500 * 1024  # 500 KB per page
_URL_FETCH_TIMEOUT: float = 10.0

# File extensions eligible for text ingestion
INGESTIBLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".scala",
        ".cpp", ".c", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
        ".swift", ".sh", ".bash", ".sql",
        ".yaml", ".yml", ".toml", ".ini", ".conf",
        ".html", ".htm", ".css", ".scss", ".sass", ".less",
        ".xml", ".json", ".md", ".txt", ".rst", ".csv",
    }
)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_text(data: bytes, mime_type: str, filename: str) -> str | None:
    """
    Extract plain text from *data*.

    Returns the extracted string, or ``None`` when the content cannot be
    decoded to meaningful text.  Always logs a warning on failure — never
    raises.
    """
    _, ext = os.path.splitext(filename.lower())

    # HTML — must be checked before the `text/*` fast-path (text/html is a
    # subtype of text/) so that we strip tags rather than return raw markup.
    if mime_type in {"text/html", "application/xhtml+xml"} or ext in {".html", ".htm"}:
        return _extract_html(data, filename)

    # CSV — same reasoning: return structured rows, not raw bytes.
    if mime_type == "text/csv" or ext == ".csv":
        return _extract_csv(data, filename)

    # Fast path: plain text and code files
    if (
        mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml"}
        or ext in INGESTIBLE_EXTENSIONS
    ):
        try:
            return data.decode("utf-8", errors="ignore") or None
        except Exception as exc:
            logger.warning("UTF-8 decode failed for %s: %s", filename, exc)
            return None

    if mime_type == "application/pdf" or ext == ".pdf":
        return _extract_pdf(data, filename)

    if mime_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    } or ext in {".docx", ".doc"}:
        return _extract_docx(data, filename)

    if mime_type in {"application/zip", "application/x-zip-compressed"} or ext == ".zip":
        return _extract_zip_text(data, filename)

    return None


def _extract_pdf(data: bytes, filename: str) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(p for p in parts if p.strip())
        return text if text.strip() else None
    except ImportError:
        logger.warning("pypdf not installed — cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", filename, exc)
        return None


def _extract_docx(data: bytes, filename: str) -> str | None:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs) if paragraphs else None
    except ImportError:
        logger.warning("python-docx not installed — cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("DOCX extraction failed for %s: %s", filename, exc)
        return None


def _extract_html(data: bytes, filename: str) -> str | None:
    import html as _html_module
    from html.parser import HTMLParser

    class _TextCollector(HTMLParser):
        _SKIP: frozenset[str] = frozenset(
            {"script", "style", "head", "meta", "link", "noscript"}
        )

        def __init__(self) -> None:
            super().__init__()
            self._parts: list[str] = []
            self._depth: int = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag.lower() in self._SKIP:
                self._depth += 1

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() in self._SKIP and self._depth > 0:
                self._depth -= 1

        def handle_data(self, data: str) -> None:
            if not self._depth:
                s = _html_module.unescape(data).strip()
                if s:
                    self._parts.append(s)

        def result(self) -> str:
            return " ".join(self._parts)

    try:
        raw = data.decode("utf-8", errors="ignore")
        collector = _TextCollector()
        collector.feed(raw)
        result = collector.result()
        return result if result.strip() else None
    except Exception as exc:
        logger.warning("HTML extraction failed for %s: %s", filename, exc)
        return None


def _extract_csv(data: bytes, filename: str) -> str | None:
    import csv

    try:
        text = data.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        lines = [",".join(row) for row in reader if row]
        return "\n".join(lines) if lines else None
    except Exception as exc:
        logger.warning("CSV extraction failed for %s: %s", filename, exc)
        return None


def _extract_zip_text(data: bytes, zip_filename: str) -> str | None:
    """Iterate a ZIP archive and concatenate text from all ingestible members."""
    parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
            if total > MAX_UNCOMPRESSED_BYTES:
                logger.warning(
                    "ZIP bomb protection triggered for %s (uncompressed %d bytes > %d limit)",
                    zip_filename,
                    total,
                    MAX_UNCOMPRESSED_BYTES,
                )
                return None

            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                _, ext = os.path.splitext(info.filename.lower())
                if ext not in INGESTIBLE_EXTENSIONS:
                    continue
                # Guard against path traversal entries
                safe = os.path.normpath(info.filename)
                if safe.startswith(".."):
                    continue
                try:
                    member_data = zf.read(info)
                    member_mime = mimetypes.guess_type(info.filename)[0] or "text/plain"
                    member_text = extract_text(member_data, member_mime, info.filename)
                    if member_text and member_text.strip():
                        parts.append(f"--- {info.filename} ---\n{member_text}")
                except Exception as exc:
                    logger.debug("Skipping ZIP member %s: %s", info.filename, exc)
    except zipfile.BadZipFile as exc:
        logger.warning("Bad ZIP file %s: %s", zip_filename, exc)
        return None

    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Chunking with overlap
# ---------------------------------------------------------------------------


def split_with_overlap(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """
    Split *text* into chunks of at most *chunk_size* characters.

    Chunks are split on line boundaries where possible.  The trailing
    *overlap* characters of the previous chunk are prepended to the next
    chunk so that context is not lost at boundaries.
    """
    max_chars = chunk_size if chunk_size is not None else CHUNK_MAX_CHARS
    olap = overlap if overlap is not None else CHUNK_OVERLAP_CHARS
    olap = min(olap, max_chars // 2)  # prevent infinite overlap

    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    lines = text.splitlines(keepends=True)
    current: list[str] = []
    current_len = 0
    prev_tail = ""

    for line in lines:
        if current_len + len(line) > max_chars and current:
            body = "".join(current)
            chunks.append(prev_tail + body if prev_tail else body)
            prev_tail = body[-olap:] if olap > 0 else ""
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        body = "".join(current)
        chunks.append(prev_tail + body if prev_tail else body)

    return chunks or [text]


# ---------------------------------------------------------------------------
# GitHub helpers (sync, no asyncio)
# ---------------------------------------------------------------------------


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _fetch_github_tree(
    owner: str,
    name: str,
    branch: str,
    token: str | None,
) -> list[dict[str, Any]]:
    """
    Return all blob entries from the GitHub Trees API for a repository.

    Uses a single ``GET /repos/{owner}/{name}/git/trees/{branch}?recursive=1``
    request.  Falls back to an empty list on any network or API error.
    """
    import httpx

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{owner}/{name}/git/trees/{branch}",
                headers=_github_headers(token),
                params={"recursive": "1"},
            )
    except httpx.RequestError as exc:
        logger.warning("GitHub Trees API request failed for %s/%s: %s", owner, name, exc)
        return []

    if resp.status_code == 404:
        logger.warning(
            "GitHub repo/branch not found: %s/%s@%s (HTTP 404)", owner, name, branch
        )
        return []
    if resp.status_code != 200:
        logger.warning(
            "GitHub Trees API returned HTTP %d for %s/%s", resp.status_code, owner, name
        )
        return []

    data = resp.json()
    if data.get("truncated"):
        logger.warning(
            "GitHub tree for %s/%s@%s was truncated — ingest may be incomplete",
            owner,
            name,
            branch,
        )

    return [
        item
        for item in data.get("tree", [])
        if item.get("type") == "blob"
        and os.path.splitext(item.get("path", "").lower())[1] in INGESTIBLE_EXTENSIONS
    ]


def _fetch_raw_file(
    owner: str,
    name: str,
    branch: str,
    path: str,
    client: Any,
) -> bytes | None:
    """Fetch raw file bytes from raw.githubusercontent.com."""
    url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"
    try:
        resp = client.get(url, timeout=15.0)
        if resp.status_code == 200:
            # Byte-level cap before decode to avoid huge files
            return resp.content[: REPO_MAX_FILE_CHARS * 4]
        logger.debug("HTTP %d fetching raw file %s", resp.status_code, path)
        return None
    except Exception as exc:
        logger.debug("Failed to fetch raw file %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_ingest_job(job_id: str):
    """Load IngestJob from DB.  Returns None if not found or DB unavailable."""
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            return session.get(IngestJob, uuid.UUID(job_id))
    except Exception:
        logger.exception("Failed to load IngestJob %s", job_id)
        return None


def _update_ingest_job(job_id: str, **kwargs: Any) -> None:
    """Persist IngestJob field updates.  Silent on DB errors."""
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        kwargs["updated_at"] = datetime.now(timezone.utc)
        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            if job is None:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)
            session.add(job)
            session.commit()
    except Exception:
        logger.exception("Failed to update IngestJob %s", job_id)


# ---------------------------------------------------------------------------
# Per-kind ingestion handlers
# ---------------------------------------------------------------------------


def _ingest_file(session: Any, job: Any) -> tuple[int, int]:
    """
    Ingest a file that was previously saved to *job.source_path*.

    Returns ``(file_count, chunk_count)``.
    
    MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §3
    This function MUST NOT run unless BOTH the staged file AND the .ready flag exist.
    The .ready flag is the deterministic signal that staging is complete.
    """
    from backend.app.models import RepoChunk
    from backend.app.repo_chunk_extractor import extract_structure

    # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §7 - Mandatory logging
    logger.info("INGEST_START job_id=%s", job.id)
    
    _update_ingest_job(str(job.id), progress=10)

    # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §3 - HARD GATE
    # Check source_path is set
    if not job.source_path:
        logger.error("INGEST_FAIL job_id=%s reason=no_source_path", job.id)
        raise RuntimeError(
            f"INVARIANT_VIOLATION: IngestJob {job.id} has no source_path. "
            f"This indicates a programming error in the upload handler."
        )
    
    path = Path(job.source_path)
    ready_path = Path(str(path) + ".ready")
    
    # HARD VERIFY: File must exist
    if not path.exists():
        logger.error("INGEST_FAIL job_id=%s reason=file_missing path=%s", job.id, path)
        raise RuntimeError(
            f"INVARIANT_VIOLATION: file missing {path}. "
            f"Job {job.id} was enqueued but the file does not exist."
        )
    
    # HARD VERIFY: Ready flag must exist
    if not ready_path.exists():
        logger.error("INGEST_FAIL job_id=%s reason=not_finalized path=%s", job.id, ready_path)
        raise RuntimeError(
            f"INVARIANT_VIOLATION: file not finalized {ready_path}. "
            f"Job {job.id} has data file but no ready flag. "
            f"System is structurally invalid if ingestion runs without ready file."
        )
    
    # Verify file is readable and get size
    file_size = 0
    try:
        file_size = path.stat().st_size
        if file_size == 0:
            # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §4 - HARD FAIL ONLY
            logger.error("INGEST_FAIL job_id=%s reason=empty_file path=%s", job.id, path)
            raise RuntimeError(
                f"INVARIANT_VIOLATION: Staged file is empty: {path} (job {job.id}). "
                f"This indicates data corruption during staging."
            )
        logger.debug("Processing staged file: %s (%d bytes, job %s)", path, file_size, job.id)
    except Exception as exc:
        if "INVARIANT_VIOLATION" in str(exc):
            raise  # Re-raise our own violations
        logger.error("INGEST_FAIL job_id=%s reason=cannot_access path=%s", job.id, path)
        raise RuntimeError(
            f"INVARIANT_VIOLATION: Cannot access staged file {path}: {exc}"
        ) from exc

    data = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    text = extract_text(data, mime_type, path.name)

    if not text or not text.strip():
        # Binary file with content but no extractable text (e.g., images, videos)
        # This is valid - return success with 0 chunks
        logger.info(
            "No extractable text in uploaded file: %s (%d bytes, job %s)",
            path.name, file_size, job.id
        )
        # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §5 - Cleanup after success
        path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        logger.info("INGEST_SUCCESS job_id=%s chunks=0", job.id)
        return 0, 0

    _update_ingest_job(str(job.id), progress=50)

    chunks = split_with_overlap(text)
    for idx, chunk_text in enumerate(chunks):
        structure = extract_structure(chunk_text, path.name)
        session.add(
            RepoChunk(
                ingest_job_id=job.id,
                file_path=path.name,
                content=chunk_text,
                chunk_index=idx,
                token_estimate=max(1, len(chunk_text) // 4),
                chunk_type=structure["chunk_type"],
                symbol=structure["symbol"],
                dependencies=structure["dependencies"],
                graph_group=structure["graph_group"],
                start_line=structure["start_line"],
                end_line=structure["end_line"],
            )
        )

    _update_ingest_job(str(job.id), progress=95)
    
    # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §5 - Cleanup ONLY after success
    path.unlink(missing_ok=True)
    ready_path.unlink(missing_ok=True)
    
    # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §7 - Mandatory logging
    logger.info("INGEST_SUCCESS job_id=%s chunks=%d", job.id, len(chunks))
    
    return 1, len(chunks)


def _ingest_url(session: Any, job: Any) -> tuple[int, int]:
    """
    Fetch a URL and ingest its text content.

    Returns ``(file_count, chunk_count)``.
    """
    import httpx

    from backend.app.models import RepoChunk
    from backend.app.repo_chunk_extractor import extract_structure

    _update_ingest_job(str(job.id), progress=10)

    url = job.source
    try:
        with httpx.Client(timeout=_URL_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(f"Failed to fetch URL {url!r}: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"URL {url!r} returned HTTP {resp.status_code}")

    _update_ingest_job(str(job.id), progress=30)

    content_bytes = resp.content[:_URL_FETCH_MAX_BYTES]
    content_type = resp.headers.get("content-type", "text/html").split(";")[0].strip()

    # Use the <title> tag (or URL) as the document filename
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", resp.text[:4096], re.IGNORECASE | re.DOTALL
    )
    page_title = title_match.group(1).strip() if title_match else url
    safe_title = re.sub(r"[^\w\-. ]", "_", page_title)[:80] or "webpage"
    filename = f"{safe_title}.html"

    text = extract_text(content_bytes, content_type, filename)
    if not text or not text.strip():
        logger.warning("No extractable text from URL: %s", url)
        return 0, 0

    _update_ingest_job(str(job.id), progress=50)

    chunks = split_with_overlap(text)
    for idx, chunk_text in enumerate(chunks):
        structure = extract_structure(chunk_text, filename)
        session.add(
            RepoChunk(
                ingest_job_id=job.id,
                file_path=filename,
                content=chunk_text,
                chunk_index=idx,
                token_estimate=max(1, len(chunk_text) // 4),
                source_url=url,
                chunk_type=structure["chunk_type"],
                symbol=structure["symbol"],
                dependencies=structure["dependencies"],
                graph_group=structure["graph_group"],
                start_line=structure["start_line"],
                end_line=structure["end_line"],
            )
        )

    _update_ingest_job(str(job.id), progress=95)
    return 1, len(chunks)


def _ingest_repo(session: Any, job: Any) -> tuple[int, int]:
    """
    Ingest a GitHub repository using the Trees API.

    Returns ``(file_count, chunk_count)``.
    """
    import httpx

    from backend.app.models import RepoChunk
    from backend.app.repo_chunk_extractor import extract_structure

    # source format: "{repo_url}@{branch}"
    source = job.source  # e.g. "https://github.com/owner/repo@main"
    match = re.search(r"github\.com/([^/]+)/([^/@]+)", source)
    if not match:
        raise ValueError(f"Cannot parse GitHub URL from job source: {source!r}")

    owner, repo_name = match.groups()
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    branch = job.branch or "main"

    token = os.environ.get("GITHUB_TOKEN", "").strip() or None

    # Fetch full file tree (single API request)
    blobs = _fetch_github_tree(owner, repo_name, branch, token)

    max_files = int(os.environ.get("REPO_MAX_FILES", str(REPO_MAX_FILES)))
    if not blobs:
        raise RuntimeError(
            f"No ingestible files found in {owner}/{repo_name}@{branch}. "
            "Verify the repository URL, branch name, and that it contains "
            "supported file types."
        )

    if len(blobs) > max_files:
        logger.warning(
            "Repo %s/%s@%s has %d eligible files; capping at %d (REPO_MAX_FILES)",
            owner,
            repo_name,
            branch,
            len(blobs),
            max_files,
        )
        blobs = blobs[:max_files]

    max_file_chars = int(os.environ.get("REPO_MAX_FILE_CHARS", str(REPO_MAX_FILE_CHARS)))
    file_count = 0
    chunk_count = 0
    total_files = len(blobs)

    with httpx.Client(timeout=30.0) as client:
        for item in blobs:
            path = item.get("path", "")
            raw_data = _fetch_raw_file(owner, repo_name, branch, path, client)
            if raw_data is None:
                continue

            mime_type = mimetypes.guess_type(path)[0] or "text/plain"
            text = extract_text(raw_data, mime_type, path)
            if not text or not text.strip():
                continue

            text = text[:max_file_chars]
            chunks = split_with_overlap(text)
            for idx, chunk_text in enumerate(chunks):
                structure = extract_structure(chunk_text, path)
                session.add(
                    RepoChunk(
                        ingest_job_id=job.id,
                        file_path=path,
                        content=chunk_text,
                        chunk_index=idx,
                        token_estimate=max(1, len(chunk_text) // 4),
                        chunk_type=structure["chunk_type"],
                        symbol=structure["symbol"],
                        dependencies=structure["dependencies"],
                        graph_group=structure["graph_group"],
                        start_line=structure["start_line"],
                        end_line=structure["end_line"],
                    )
                )
                chunk_count += 1

            file_count += 1

            # Commit partial progress every 20 files so status polling is live
            if file_count % 20 == 0:
                job.chunk_count = chunk_count
                job.file_count = file_count
                # Calculate progress: 0-95% based on files processed
                job.progress = (
                    min(95, int((file_count / total_files) * 95))
                    if total_files > 0
                    else 95
                )
                session.add(job)
                session.commit()
                logger.info(
                    "IngestJob %s: progress %d files / %d chunks (%d%%)",
                    str(job.id),
                    file_count,
                    chunk_count,
                    job.progress,
                )

    return file_count, chunk_count


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def process_ingest_job(job_id: str) -> None:
    """
    Run the ingestion pipeline for the IngestJob identified by *job_id*.

    Designed to run in an RQ worker or background thread.

    LAW: ALWAYS_UPDATE_TERMINAL
        A terminal status (``success`` or ``failed``) is written
        unconditionally — even when an unexpected exception is raised.
        Callers never need to handle partial or stuck state.
    
    MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §2
        For kind="file", BOTH the data file AND .ready flag MUST exist.
        The _ingest_file function performs the hard gate check.
    """
    logger.info("IngestJob %s: starting", job_id)
    _update_ingest_job(job_id, status="running", progress=0)

    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            job = session.get(IngestJob, uuid.UUID(job_id))
            if job is None:
                logger.error("IngestJob %s not found in DB — aborting", job_id)
                return

            # MQP-CONTRACT:FILE_STAGING_FINAL_INVARIANT_V2 §3
            # Pre-flight checks moved into _ingest_file for centralized validation
            if job.kind == "file":
                file_count, chunk_count = _ingest_file(session, job)
            elif job.kind == "url":
                file_count, chunk_count = _ingest_url(session, job)
            elif job.kind == "repo":
                file_count, chunk_count = _ingest_repo(session, job)
            else:
                raise ValueError(f"Unknown IngestJob kind: {job.kind!r}")

            job.status = "success"
            job.progress = 100
            job.file_count = file_count
            job.chunk_count = chunk_count
            job.updated_at = datetime.now(timezone.utc)
            session.add(job)
            session.commit()
            logger.info(
                "IngestJob %s: success (kind=%s, files=%d, chunks=%d)",
                job_id,
                job.kind,
                file_count,
                chunk_count,
            )

    except Exception as exc:
        logger.exception("IngestJob %s: failed — %s", job_id, exc)
        _update_ingest_job(job_id, status="failed", error=str(exc)[:1000], progress=0)
