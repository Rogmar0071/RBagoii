"""
backend.app.document_routes
============================
Unified document ingestion API — mirrors AnythingLLM's collector service.

Endpoints
---------
POST   /v1/document/upload          Upload any text-extractable file; auto-chunks
                                    and stores as RepoChunk rows for RAG retrieval.
POST   /v1/document/upload-link     Scrape a URL and ingest its text content.
GET    /v1/documents                List all ingested documents for a conversation
                                    or workspace.
GET    /v1/document/{doc_id}        Retrieve document metadata and extracted text.

All endpoints require ``Authorization: Bearer <API_KEY>``.

Accepted file types
-------------------
The upload endpoint accepts any file whose content can be text-extracted:
  text/plain, text/markdown, text/html, text/csv, application/json,
  application/xml, application/pdf, application/msword,
  application/vnd.openxmlformats-officedocument.wordprocessingml.document,
  as well as any file with a recognised code extension.

The returned ``extraction_status`` field is one of:
  "ok"               — text extracted successfully
  "no_text_extracted" — file uploaded but no text could be extracted
  "failed"           — extraction error; see ``extraction_error``
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
_URL_FETCH_MAX_BYTES: int = 500 * 1024  # 500 KB for URL ingestion
_URL_FETCH_TIMEOUT: float = 10.0

# Accepted MIME types for the unified upload endpoint
_ACCEPTED_UPLOAD_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "text/xml",
        "application/json",
        "application/xml",
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/zip",
        "application/x-zip-compressed",
        "video/mp4",
    }
)

# Additional code-file extensions accepted regardless of MIME type
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".sh",
        ".bash",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".conf",
        ".html",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".xml",
        ".json",
        ".md",
        ".txt",
        ".csv",
    }
)


# ---------------------------------------------------------------------------
# DB / session helpers
# ---------------------------------------------------------------------------


def _db_session():
    """FastAPI dependency — yields a DB session or raises 503 if unconfigured."""
    try:
        from backend.app.database import get_session

        yield from get_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_db(session):
    if session is None:
        raise HTTPException(
            status_code=503, detail="Database not configured (DATABASE_URL missing)."
        )
    return session


# ---------------------------------------------------------------------------
# Text extraction (delegates to chat_file_routes helpers)
# ---------------------------------------------------------------------------


def _extract_text(file_content: bytes, mime_type: str, filename: str) -> tuple[str | None, str]:
    """
    Extract text from *file_content*.

    Returns ``(text, extraction_status)`` where status is one of:
      "ok" | "no_text_extracted" | "failed"
    """
    try:
        from backend.app.chat_file_routes import extract_text_content

        text = extract_text_content(file_content, mime_type, filename)
        if text and text.strip():
            return text, "ok"
        return None, "no_text_extracted"
    except Exception as exc:
        logger.warning("Text extraction error for %s: %s", filename, exc)
        return None, "failed"


# ---------------------------------------------------------------------------
# Chunk helpers
# ---------------------------------------------------------------------------


def _store_chunks(
    session,
    doc_id: uuid.UUID,
    filename: str,
    text: str,
    source_url: str | None = None,
) -> int:
    """Chunk *text* and insert RepoChunk rows linked to *doc_id* (as chat_file_id)."""
    from backend.app.models import RepoChunk
    from backend.app.repo_retrieval import _split_into_chunks

    chunks = _split_into_chunks(text)
    for idx, chunk_text in enumerate(chunks):
        chunk = RepoChunk(
            chat_file_id=doc_id,
            file_path=filename,
            content=chunk_text,
            chunk_index=idx,
            token_estimate=max(1, len(chunk_text) // 4),
            source_url=source_url,
        )
        session.add(chunk)
    return len(chunks)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DocumentUploadResponse(BaseModel):
    doc_id: str
    filename: str
    mime_type: str
    size_bytes: int
    category: str
    chunk_count: int
    extraction_status: str
    extraction_error: Optional[str] = None
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None


class DocumentListItem(BaseModel):
    doc_id: str
    filename: str
    mime_type: str
    size_bytes: int
    category: str
    extraction_status: str
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_at: str


class DocumentDetail(BaseModel):
    doc_id: str
    filename: str
    mime_type: str
    size_bytes: int
    category: str
    extraction_status: str
    extracted_text: Optional[str] = None
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# POST /v1/document/upload
# ---------------------------------------------------------------------------


@router.post("/document/upload", status_code=201, dependencies=[Depends(require_auth)])
async def upload_document(
    file: UploadFile = File(...),
    conversation_id: Optional[str] = Form(default=None),
    workspace_id: Optional[str] = Form(default=None),
    session=Depends(_db_session),
) -> DocumentUploadResponse:
    """
    Upload any text-extractable document.

    The file is:
    1. Read and size-checked (MAX_UPLOAD_BYTES).
    2. MIME-type validated.
    3. Text-extracted (PDF → pypdf, DOCX → python-docx, HTML → html.parser,
       CSV → csv module, plain text / code → UTF-8 decode).
    4. Uploaded to object storage (if configured).
    5. Saved as a ChatFile record.
    6. Chunked into RepoChunk rows for RAG retrieval.

    The response includes ``extraction_status``:
      "ok"               — text extracted successfully
      "no_text_extracted" — stored but no text could be extracted
      "failed"           — extraction error
    """
    from backend.app.chat_file_routes import categorize_file
    from backend.app.models import ChatFile

    filename = file.filename or "unknown"
    _, ext = os.path.splitext(filename.lower())

    # Size-limited read
    file_content = await file.read()
    if len(file_content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds maximum allowed size of {MAX_UPLOAD_BYTES} bytes",
        )

    # MIME type determination + validation
    mime_type = (
        file.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    # Allow known code extensions even if MIME is generic
    if mime_type not in _ACCEPTED_UPLOAD_TYPES and ext not in _CODE_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{mime_type}'. "
                "Accepted: text, PDF, DOCX, HTML, CSV, JSON, XML, code files, ZIP, MP4."
            ),
        )

    file_size = len(file_content)
    category = categorize_file(filename, mime_type)

    # Text extraction
    extracted_text, extraction_status = _extract_text(file_content, mime_type, filename)
    extraction_error: str | None = None
    if extraction_status == "failed":
        extraction_error = "Text extraction encountered an error; file stored without text."

    # Object storage upload (best-effort — non-fatal if R2 not configured)
    file_id = uuid.uuid4()
    object_key = f"documents/{file_id}/{filename}"
    try:
        from backend.app.storage import upload_bytes

        upload_bytes(object_key=object_key, data=file_content, content_type=mime_type)
    except Exception as exc:
        logger.warning("Object storage upload skipped for %s: %s", filename, exc)
        object_key = f"local:{file_id}/{filename}"

    # Persist ChatFile record
    chat_file = ChatFile(
        id=file_id,
        conversation_id=conversation_id or "documents",
        filename=filename,
        mime_type=mime_type,
        size_bytes=file_size,
        object_key=object_key,
        category=category,
        included_in_context=True,
        extracted_text=extracted_text,
        workspace_id=workspace_id,
    )
    session.add(chat_file)
    session.flush()

    # Chunk and store for RAG
    chunk_count = 0
    if extracted_text:
        chunk_count = _store_chunks(session, file_id, filename, extracted_text)

    session.commit()

    return DocumentUploadResponse(
        doc_id=str(file_id),
        filename=filename,
        mime_type=mime_type,
        size_bytes=file_size,
        category=category,
        chunk_count=chunk_count,
        extraction_status=extraction_status,
        extraction_error=extraction_error,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# POST /v1/document/upload-link
# ---------------------------------------------------------------------------


class UploadLinkRequest(BaseModel):
    url: str
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None


@router.post("/document/upload-link", status_code=201, dependencies=[Depends(require_auth)])
async def upload_link(
    body: UploadLinkRequest,
    session=Depends(_db_session),
) -> DocumentUploadResponse:
    """
    Scrape a URL and ingest its text content as a document.

    The page is fetched via httpx (10 s timeout, 500 KB limit), HTML tags are
    stripped using stdlib ``html.parser``, and the result is chunked and stored
    as RepoChunk rows for RAG retrieval.

    The page title (``<title>`` tag) is used as the filename.  The source URL
    is stored on each RepoChunk row so citations can reference the original URL.
    """
    from backend.app.chat_file_routes import _extract_html_text
    from backend.app.models import ChatFile

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # Fetch page content
    try:
        async with httpx.AsyncClient(timeout=_URL_FETCH_TIMEOUT) as client:
            response = await client.get(url, follow_redirects=True)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=422, detail=f"Failed to fetch URL: {exc}"
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=422,
            detail=f"URL returned HTTP {response.status_code}",
        )

    content_bytes = response.content[:_URL_FETCH_MAX_BYTES]
    content_type = response.headers.get("content-type", "text/html").split(";")[0].strip()

    # Extract title from HTML
    import re

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", response.text[:4096], re.IGNORECASE | re.DOTALL
    )
    page_title = title_match.group(1).strip() if title_match else url
    # Sanitize title to use as filename
    safe_title = re.sub(r"[^\w\-. ]", "_", page_title)[:100] or "webpage"
    filename = f"{safe_title}.html"

    # Extract text
    extracted_text = _extract_html_text(content_bytes, filename)
    extraction_status = "ok" if (extracted_text and extracted_text.strip()) else "no_text_extracted"

    file_id = uuid.uuid4()
    object_key = f"documents/{file_id}/{filename}"

    # Object storage upload (best-effort)
    try:
        from backend.app.storage import upload_bytes

        upload_bytes(object_key=object_key, data=content_bytes, content_type=content_type)
    except Exception as exc:
        logger.warning("Object storage upload skipped for URL doc %s: %s", url, exc)
        object_key = f"local:{file_id}/{filename}"

    chat_file = ChatFile(
        id=file_id,
        conversation_id=body.conversation_id or "documents",
        filename=filename,
        mime_type=content_type,
        size_bytes=len(content_bytes),
        object_key=object_key,
        category="document",
        included_in_context=True,
        extracted_text=extracted_text,
        workspace_id=body.workspace_id,
    )
    session.add(chat_file)
    session.flush()

    # Chunk and store for RAG (source_url = the scraped URL for citations)
    chunk_count = 0
    if extracted_text:
        chunk_count = _store_chunks(session, file_id, filename, extracted_text, source_url=url)

    session.commit()

    return DocumentUploadResponse(
        doc_id=str(file_id),
        filename=filename,
        mime_type=content_type,
        size_bytes=len(content_bytes),
        category="document",
        chunk_count=chunk_count,
        extraction_status=extraction_status,
        conversation_id=body.conversation_id,
        workspace_id=body.workspace_id,
    )


# ---------------------------------------------------------------------------
# GET /v1/documents
# ---------------------------------------------------------------------------


@router.get("/documents", status_code=200, dependencies=[Depends(require_auth)])
def list_documents(
    conversation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    session=Depends(_db_session),
) -> list[DocumentListItem]:
    """
    List ingested documents.

    Filter by ``conversation_id`` and/or ``workspace_id``.  If neither is
    supplied, all documents are returned (newest-first, limit 200).
    """
    from sqlmodel import select

    from backend.app.models import ChatFile, RepoChunk

    stmt = select(ChatFile).order_by(ChatFile.created_at.desc()).limit(200)
    if conversation_id:
        stmt = stmt.where(ChatFile.conversation_id == conversation_id)
    if workspace_id:
        stmt = stmt.where(ChatFile.workspace_id == workspace_id)

    files = session.exec(stmt).all()

    result: list[DocumentListItem] = []
    for f in files:
        # Count associated chunks
        chunk_stmt = select(RepoChunk).where(RepoChunk.chat_file_id == f.id)
        chunks = session.exec(chunk_stmt).all()
        extraction_status = "ok" if f.extracted_text else "no_text_extracted"
        result.append(
            DocumentListItem(
                doc_id=str(f.id),
                filename=f.filename,
                mime_type=f.mime_type,
                size_bytes=f.size_bytes,
                category=f.category,
                extraction_status=extraction_status,
                conversation_id=f.conversation_id,
                workspace_id=getattr(f, "workspace_id", None),
                created_at=f.created_at.isoformat() if f.created_at else "",
            )
        )
    return result


# ---------------------------------------------------------------------------
# GET /v1/document/{doc_id}
# ---------------------------------------------------------------------------


@router.get("/document/{doc_id}", status_code=200, dependencies=[Depends(require_auth)])
def get_document(
    doc_id: str,
    session=Depends(_db_session),
) -> DocumentDetail:
    """Retrieve document metadata and extracted text by doc_id (ChatFile UUID)."""
    from backend.app.models import ChatFile

    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid doc_id")

    doc = session.get(ChatFile, doc_uuid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    extraction_status = "ok" if doc.extracted_text else "no_text_extracted"
    return DocumentDetail(
        doc_id=str(doc.id),
        filename=doc.filename,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        category=doc.category,
        extraction_status=extraction_status,
        extracted_text=doc.extracted_text,
        conversation_id=doc.conversation_id,
        workspace_id=getattr(doc, "workspace_id", None),
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


# ---------------------------------------------------------------------------
# POST /api/repos/{repo_id}/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/api/repos/{repo_id}/refresh",
    status_code=202,
    dependencies=[Depends(require_auth)],
    tags=["repos"],
)
def refresh_repo(
    repo_id: str,
    session=Depends(_db_session),
) -> dict[str, Any]:
    """
    Re-fetch and re-chunk a GitHub repository, replacing stale chunks.

    Unlike the retry endpoint (which only re-runs failed repos), refresh works
    on any terminal repo (``success`` or ``failed``).  Running repos are
    rejected with HTTP 409.
    """
    from sqlmodel import select

    from backend.app.github_routes import _enqueue_repo_ingestion
    from backend.app.models import Repo, RepoChunk

    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    repo = session.get(Repo, repo_uuid)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    if repo.ingestion_status == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot refresh while ingestion is already running",
        )

    # Delete existing chunks so the worker rebuilds from scratch
    chunk_stmt = select(RepoChunk).where(RepoChunk.repo_id == repo_uuid)
    for chunk in session.exec(chunk_stmt).all():
        session.delete(chunk)

    repo.ingestion_status = "pending"
    repo.total_files = 0
    repo.total_chunks = 0
    session.add(repo)
    session.commit()

    _enqueue_repo_ingestion(repo_id)

    return {"repo_id": repo_id, "status": "pending", "message": "Refresh enqueued"}


# ---------------------------------------------------------------------------
# GET /api/repos/{repo_id}/status
# ---------------------------------------------------------------------------


@router.get(
    "/api/repos/{repo_id}/status",
    status_code=200,
    dependencies=[Depends(require_auth)],
    tags=["repos"],
)
def get_repo_status(
    repo_id: str,
    session=Depends(_db_session),
) -> dict[str, Any]:
    """
    Return ingestion status and statistics for a repository.

    Clients can poll this endpoint while ingestion is in progress.
    """
    from backend.app.models import Repo

    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    repo = session.get(Repo, repo_uuid)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    return {
        "repo_id": str(repo.id),
        "repo_url": repo.repo_url,
        "owner": repo.owner,
        "name": repo.name,
        "branch": repo.branch,
        "status": repo.ingestion_status,
        "total_files": repo.total_files,
        "chunk_count": repo.total_chunks,
        "validation_status": repo.validation_status,
        "trust_class": repo.trust_class,
        "created_at": repo.created_at.isoformat() if repo.created_at else None,
        "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
    }


# ---------------------------------------------------------------------------
# DELETE /api/repos/{repo_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/api/repos/{repo_id}",
    status_code=204,
    dependencies=[Depends(require_auth)],
    tags=["repos"],
)
def delete_repo(
    repo_id: str,
    session=Depends(_db_session),
) -> None:
    """
    Delete a repository and all its chunks and conversation bindings.

    This is the global delete endpoint — it removes the repo regardless of
    which conversation originally added it.
    """
    from sqlmodel import select

    from backend.app.models import ConversationRepo, Repo, RepoChunk

    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    repo = session.get(Repo, repo_uuid)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    chunk_stmt = select(RepoChunk).where(RepoChunk.repo_id == repo_uuid)
    for chunk in session.exec(chunk_stmt).all():
        session.delete(chunk)

    binding_stmt = select(ConversationRepo).where(ConversationRepo.repo_id == repo_uuid)
    for binding in session.exec(binding_stmt).all():
        session.delete(binding)

    session.delete(repo)
    session.commit()
    return None
