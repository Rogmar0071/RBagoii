"""
backend.app.chat_file_routes
=============================
FastAPI router for chat file management endpoints.

Endpoints
---------
POST   /api/chat/{conversation_id}/files        Upload a file to a conversation
POST   /api/chat/{conversation_id}/files/chunks Upload a chunk of a file
PUT    /api/chat/{conversation_id}/files/chunks/{upload_id}/finalize  Finalize chunked upload
GET    /api/chat/{conversation_id}/files        List all files in a conversation
PATCH  /api/chat/{conversation_id}/files/{file_id}   Update file metadata (rename, toggle context)
DELETE /api/chat/{conversation_id}/files/{file_id}   Delete a file
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.app.auth import require_auth
from backend.app.database import get_session
from backend.app.models import ChatFile
from backend.app.repo_chunking import (
    assemble_chunks,
    cleanup,
    save_chunk,
)
from backend.app.storage import get_presigned_url, upload_bytes

router = APIRouter(prefix="/api/chat")
logger = logging.getLogger(__name__)

# File categorization based on MIME type
CATEGORY_MAP = {
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "text/plain": "document",
    "text/markdown": "document",
    "text/html": "document",
    "application/xhtml+xml": "document",
    "text/csv": "data",
    "application/json": "data",
    "application/xml": "data",
    "text/xml": "data",
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/svg+xml": "image",
    "video/mp4": "video",
    "video/webm": "video",
    "video/quicktime": "video",
    "audio/mpeg": "audio",
    "audio/wav": "audio",
    "audio/ogg": "audio",
    "application/zip": "archive",
    "application/x-tar": "archive",
    "application/gzip": "archive",
}

# Code file extensions
CODE_EXTENSIONS = {
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
}


def categorize_file(filename: str, mime_type: str) -> str:
    """Determine file category based on MIME type and extension."""
    # Check MIME type first
    if mime_type in CATEGORY_MAP:
        return CATEGORY_MAP[mime_type]

    # Check if it's a code file by extension
    _, ext = os.path.splitext(filename.lower())
    if ext in CODE_EXTENSIONS:
        return "code"

    # Default categorization by MIME type prefix
    if mime_type.startswith("text/"):
        return "document"
    elif mime_type.startswith("image/"):
        return "image"
    elif mime_type.startswith("video/"):
        return "video"
    elif mime_type.startswith("audio/"):
        return "audio"
    elif mime_type.startswith("application/"):
        return "data"

    return "other"


def extract_text_content(file_content: bytes, mime_type: str, filename: str) -> Optional[str]:
    """Extract text content from file for AI-friendly storage.

    Supports: plain text, code, JSON, XML, PDF, DOCX, HTML, CSV.
    Returns None for binary files that cannot be decoded to text.
    """
    try:
        _, ext = os.path.splitext(filename.lower())

        # HTML — strip tags before the text/* fast-path
        if mime_type in {"text/html", "application/xhtml+xml"} or ext in {".html", ".htm"}:
            return _extract_html_text(file_content, filename)

        # CSV — structured row extraction before text/* fast-path
        if mime_type == "text/csv" or ext == ".csv":
            return _extract_csv_text(file_content, filename)

        # Plain text and code files (fast path)
        if mime_type.startswith("text/") or mime_type in {"application/json", "application/xml"}:
            return file_content.decode("utf-8", errors="ignore")

        if ext in CODE_EXTENSIONS:
            return file_content.decode("utf-8", errors="ignore")

        # PDF extraction via pypdf
        if mime_type == "application/pdf" or ext == ".pdf":
            return _extract_pdf_text(file_content, filename)

        # DOCX extraction via python-docx
        if mime_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        } or ext in {".docx", ".doc"}:
            return _extract_docx_text(file_content, filename)

        return None
    except Exception as e:
        logger.warning(f"Failed to extract text from {filename}: {e}")
        return None


def _extract_pdf_text(file_content: bytes, filename: str) -> Optional[str]:
    """Extract text from a PDF using pypdf (soft dependency)."""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_content))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
        return "\n".join(parts) if parts else None
    except ImportError:
        logger.warning("pypdf not installed; cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", filename, exc)
        return None


def _extract_docx_text(file_content: bytes, filename: str) -> Optional[str]:
    """Extract text from a DOCX file using python-docx (soft dependency)."""
    try:
        import io

        from docx import Document

        doc = Document(io.BytesIO(file_content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs) if paragraphs else None
    except ImportError:
        logger.warning("python-docx not installed; cannot extract text from %s", filename)
        return None
    except Exception as exc:
        logger.warning("DOCX extraction failed for %s: %s", filename, exc)
        return None


def _extract_html_text(file_content: bytes, filename: str) -> Optional[str]:
    """Strip HTML tags and return plain text using stdlib html.parser."""
    import html
    from html.parser import HTMLParser

    class _TextCollector(HTMLParser):
        _SKIP_TAGS = {"script", "style", "head", "meta", "link"}

        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag.lower() in self._SKIP_TAGS:
                self._skip_depth += 1

        def handle_endtag(self, tag):
            if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
                self._skip_depth -= 1

        def handle_data(self, data):
            if self._skip_depth == 0:
                stripped = data.strip()
                if stripped:
                    self._parts.append(html.unescape(stripped))

        def text(self) -> str:
            return " ".join(self._parts)

    try:
        raw = file_content.decode("utf-8", errors="ignore")
        collector = _TextCollector()
        collector.feed(raw)
        result = collector.text()
        return result if result.strip() else None
    except Exception as exc:
        logger.warning("HTML extraction failed for %s: %s", filename, exc)
        return None


def _extract_csv_text(file_content: bytes, filename: str) -> Optional[str]:
    """Convert CSV rows to a readable text representation."""
    import csv
    import io

    try:
        text = file_content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return None
        lines = [",".join(row) for row in rows]
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("CSV extraction failed for %s: %s", filename, exc)
        return None


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class ChatFileResponse(BaseModel):
    id: str
    conversation_id: str
    filename: str
    mime_type: str
    size_bytes: int
    category: str
    included_in_context: bool
    created_at: str
    updated_at: str
    download_url: Optional[str] = None


class ChatFileUpdateRequest(BaseModel):
    filename: Optional[str] = None
    included_in_context: Optional[bool] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{conversation_id}/files", status_code=201, dependencies=[Depends(require_auth)])
async def upload_chat_file(
    conversation_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> ChatFileResponse:
    """Upload a file to a chat conversation."""
    try:
        # Read file content
        file_content = await file.read()
        file_size = len(file_content)

        # Determine MIME type
        mime_type = (
            file.content_type
            or mimetypes.guess_type(file.filename)[0]
            or "application/octet-stream"
        )

        # Categorize file
        category = categorize_file(file.filename, mime_type)

        # Extract text content if possible
        extracted_text = extract_text_content(file_content, mime_type, file.filename)

        # Generate object key
        file_id = uuid.uuid4()
        object_key = f"chat_files/{conversation_id}/{file_id}/{file.filename}"

        # Upload to storage
        upload_bytes(
            object_key=object_key,
            data=file_content,
            content_type=mime_type,
        )

        # Create database record
        chat_file = ChatFile(
            id=file_id,
            conversation_id=conversation_id,
            filename=file.filename,
            mime_type=mime_type,
            size_bytes=file_size,
            object_key=object_key,
            category=category,
            included_in_context=True,
            extracted_text=extracted_text,
        )

        session.add(chat_file)
        session.commit()
        session.refresh(chat_file)

        # Get download URL
        download_url = get_presigned_url(object_key, expires_in=3600)

        return ChatFileResponse(
            id=str(chat_file.id),
            conversation_id=chat_file.conversation_id,
            filename=chat_file.filename,
            mime_type=chat_file.mime_type,
            size_bytes=chat_file.size_bytes,
            category=chat_file.category,
            included_in_context=chat_file.included_in_context,
            created_at=chat_file.created_at.isoformat(),
            updated_at=chat_file.updated_at.isoformat(),
            download_url=download_url,
        )
    except Exception as e:
        logger.error(f"File upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@router.post(
    "/{conversation_id}/files/chunks", status_code=202, dependencies=[Depends(require_auth)]
)
async def upload_chat_file_chunk(
    conversation_id: str,
    chunk: UploadFile,
    x_upload_id: str = Header(..., alias="X-Upload-Id"),
    x_chunk_index: int = Header(..., alias="X-Chunk-Index"),
    x_total_chunks: int = Header(..., alias="X-Total-Chunks"),
    x_filename: str = Header(..., alias="X-Filename"),
) -> dict:
    """
    Upload a single chunk of a file being uploaded in parts.

    Headers required:
      X-Upload-Id     — stable UUID for this chunked upload session
      X-Chunk-Index   — 0-based index of this chunk
      X-Total-Chunks  — total number of chunks expected
      X-Filename      — original filename

    When all chunks are received, call PUT /{conversation_id}/files/chunks/{upload_id}/finalize
    """
    # Validate upload ID is a UUID
    try:
        uuid.UUID(x_upload_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Upload-Id must be a valid UUID")

    if x_chunk_index < 0 or x_total_chunks < 1 or x_chunk_index >= x_total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk index or total")

    # Read chunk data
    chunk_data = await chunk.read()

    # Save chunk
    result = save_chunk(
        upload_id=x_upload_id,
        chunk_index=x_chunk_index,
        total_chunks=x_total_chunks,
        data=chunk_data,
    )

    return {
        "upload_id": x_upload_id,
        "chunk_index": x_chunk_index,
        "chunks_received": result["chunks_received"],
        "total_chunks": x_total_chunks,
        "complete": result["complete"],
    }


@router.put(
    "/{conversation_id}/files/chunks/{upload_id}/finalize",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def finalize_chat_file_upload(
    conversation_id: str,
    upload_id: str,
    filename: str = Form(...),
    mime_type: str = Form("application/octet-stream"),
    session: Session = Depends(get_session),
) -> ChatFileResponse:
    """
    Finalize a chunked upload by assembling all chunks and creating the file record.
    """
    try:
        uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    try:
        # Assemble chunks
        file_content = assemble_chunks(upload_id)
        file_size = len(file_content)

        # Categorize file
        category = categorize_file(filename, mime_type)

        # Extract text content if possible
        extracted_text = extract_text_content(file_content, mime_type, filename)

        # Generate object key
        file_id = uuid.uuid4()
        object_key = f"chat_files/{conversation_id}/{file_id}/{filename}"

        # Upload to storage
        upload_bytes(
            object_key=object_key,
            data=file_content,
            content_type=mime_type,
        )

        # Create database record
        chat_file = ChatFile(
            id=file_id,
            conversation_id=conversation_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=file_size,
            object_key=object_key,
            category=category,
            included_in_context=True,
            extracted_text=extracted_text,
        )

        session.add(chat_file)
        session.commit()
        session.refresh(chat_file)

        # Clean up chunks
        cleanup(upload_id)

        # Get download URL
        download_url = get_presigned_url(object_key, expires_in=3600)

        return ChatFileResponse(
            id=str(chat_file.id),
            conversation_id=chat_file.conversation_id,
            filename=chat_file.filename,
            mime_type=chat_file.mime_type,
            size_bytes=chat_file.size_bytes,
            category=chat_file.category,
            included_in_context=chat_file.included_in_context,
            created_at=chat_file.created_at.isoformat(),
            updated_at=chat_file.updated_at.isoformat(),
            download_url=download_url,
        )
    except Exception as e:
        logger.error(f"Chunked file upload failed: {e}")
        # Try to clean up on error
        try:
            cleanup(upload_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@router.get("/{conversation_id}/files", status_code=200, dependencies=[Depends(require_auth)])
def list_chat_files(
    conversation_id: str,
    all_conversations: bool = False,
    session: Session = Depends(get_session),
) -> List[ChatFileResponse]:
    """
    List all files in a conversation, or optionally all files across all conversations.

    Args:
        conversation_id: The conversation to list files from (ignored if all_conversations=True)
        all_conversations: If True, list files from all conversations instead of just one
    """
    if all_conversations:
        # List ALL files across all conversations
        stmt = select(ChatFile).order_by(ChatFile.created_at.desc())
    else:
        # List files only for this conversation
        stmt = (
            select(ChatFile)
            .where(ChatFile.conversation_id == conversation_id)
            .order_by(ChatFile.category, ChatFile.created_at.desc())
        )

    files = session.exec(stmt).all()

    result = []
    for f in files:
        download_url = get_presigned_url(f.object_key, expires_in=3600)
        result.append(
            ChatFileResponse(
                id=str(f.id),
                conversation_id=f.conversation_id,
                filename=f.filename,
                mime_type=f.mime_type,
                size_bytes=f.size_bytes,
                category=f.category,
                included_in_context=f.included_in_context,
                created_at=f.created_at.isoformat(),
                updated_at=f.updated_at.isoformat(),
                download_url=download_url,
            )
        )

    return result


@router.patch(
    "/{conversation_id}/files/{file_id}",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def update_chat_file(
    conversation_id: str,
    file_id: str,
    update: ChatFileUpdateRequest,
    session: Session = Depends(get_session),
) -> ChatFileResponse:
    """Update file metadata (rename or toggle context inclusion)."""
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    stmt = select(ChatFile).where(
        ChatFile.id == file_uuid,
        ChatFile.conversation_id == conversation_id,
    )
    chat_file = session.exec(stmt).first()

    if not chat_file:
        raise HTTPException(status_code=404, detail="File not found")

    # Update fields
    if update.filename is not None:
        chat_file.filename = update.filename
    if update.included_in_context is not None:
        chat_file.included_in_context = update.included_in_context

    session.add(chat_file)
    session.commit()
    session.refresh(chat_file)

    download_url = get_presigned_url(chat_file.object_key, expires_in=3600)

    return ChatFileResponse(
        id=str(chat_file.id),
        conversation_id=chat_file.conversation_id,
        filename=chat_file.filename,
        mime_type=chat_file.mime_type,
        size_bytes=chat_file.size_bytes,
        category=chat_file.category,
        included_in_context=chat_file.included_in_context,
        created_at=chat_file.created_at.isoformat(),
        updated_at=chat_file.updated_at.isoformat(),
        download_url=download_url,
    )


@router.delete(
    "/{conversation_id}/files/{file_id}",
    status_code=204,
    dependencies=[Depends(require_auth)],
)
def delete_chat_file(
    conversation_id: str,
    file_id: str,
    allow_cross_conversation: bool = False,
    session: Session = Depends(get_session),
):
    """
    Delete a file from the conversation.

    Args:
        conversation_id: The conversation context
        file_id: The file to delete
        allow_cross_conversation: If True, allow deleting files from other conversations
    """
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    if allow_cross_conversation:
        # Allow deleting files from any conversation
        stmt = select(ChatFile).where(ChatFile.id == file_uuid)
    else:
        # Only delete files from this specific conversation
        stmt = select(ChatFile).where(
            ChatFile.id == file_uuid,
            ChatFile.conversation_id == conversation_id,
        )

    chat_file = session.exec(stmt).first()

    if not chat_file:
        raise HTTPException(status_code=404, detail="File not found")

    # Delete from object storage
    try:
        from backend.app.storage import delete_object

        delete_object(chat_file.object_key)
    except Exception as e:
        logger.warning(f"Failed to delete file from storage: {chat_file.object_key}, error: {e}")

    session.delete(chat_file)
    session.commit()

    return None
