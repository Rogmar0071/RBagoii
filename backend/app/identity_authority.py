from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from backend.app.models import ConversationContext, ConversationRepo, Repo, RepoChunk, RepoFile

_AUTHORITY_ACTIVE: ContextVar[bool] = ContextVar("_AUTHORITY_ACTIVE", default=False)
_APP_PATH_FRAGMENT = f"{Path('/backend/app/')}"
_AUTHORITY_FILE = "identity_authority.py"
_MODELS_FILE = "models.py"


@contextmanager
def _authority_scope():
    token = _AUTHORITY_ACTIVE.set(True)
    try:
        yield
    finally:
        _AUTHORITY_ACTIVE.reset(token)


def _find_external_caller_path() -> str:
    for frame_info in inspect.stack()[2:]:
        path = (frame_info.filename or "").replace("\\", "/")
        if path.endswith(_AUTHORITY_FILE) or path.endswith(_MODELS_FILE):
            continue
        return path
    return ""


def assert_constructor_authority() -> None:
    if _AUTHORITY_ACTIVE.get():
        return
    for frame_info in inspect.stack()[1:]:
        path = (frame_info.filename or "").replace("\\", "/")
        if "/backend/app/" not in path:
            continue
        if path.endswith(_AUTHORITY_FILE) or path.endswith(_MODELS_FILE):
            continue
        raise RuntimeError("IDENTITY_CONSTRUCTOR_VIOLATION")


def create_repo(
    *,
    session: Session,
    repo_id: Any | None = None,
    repo_url: str,
    owner: str,
    name: str,
    branch: str,
    conversation_id: str | None = None,
    ingestion_status: str = "pending",
    total_files: int = 0,
    total_chunks: int = 0,
) -> Repo:
    with _authority_scope():
        repo = Repo(
            id=repo_id,
            repo_url=repo_url,
            owner=owner,
            name=name,
            branch=branch,
            conversation_id=conversation_id,
            ingestion_status=ingestion_status,
            total_files=total_files,
            total_chunks=total_chunks,
        )
        session.add(repo)
        session.flush()
        if not repo.id:
            raise RuntimeError("IDENTITY_BYPASS_DETECTED")
        return repo


def create_repo_file(
    *,
    session: Session,
    repo: Repo,
    path: str,
    language: str | None,
    size_bytes: int,
    content_hash: str | None,
) -> RepoFile:
    if repo is None or not repo.id:
        raise RuntimeError("IDENTITY_FORGERY")
    if session.get(Repo, repo.id) is None:
        raise RuntimeError("IDENTITY_FORGERY")
    with _authority_scope():
        repo_file = RepoFile(
            repo_id=repo.id,
            path=path,
            language=language,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )
        session.add(repo_file)
        session.flush()
        if not repo_file.id:
            raise RuntimeError("IDENTITY_BYPASS_DETECTED")
        return repo_file


def create_repo_chunk(
    *,
    session: Session,
    repo_file: RepoFile,
    content: str,
    chunk_index: int,
    token_estimate: int,
    repo_id: Any | None = None,
    ingest_job_id: Any | None = None,
    chat_file_id: Any | None = None,
    source_url: str | None = None,
    chunk_type: str | None = None,
    symbol: str | None = None,
    dependencies: Any | None = None,
    graph_group: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> RepoChunk:
    if repo_file is None or not repo_file.id:
        raise RuntimeError("IDENTITY_FORGERY")
    with _authority_scope():
        chunk = RepoChunk(
            repo_id=repo_id,
            ingest_job_id=ingest_job_id,
            chat_file_id=chat_file_id,
            file_id=repo_file.id,
            file_path=repo_file.path,
            content=content,
            chunk_index=chunk_index,
            token_estimate=token_estimate,
            source_url=source_url,
            chunk_type=chunk_type,
            symbol=symbol,
            dependencies=dependencies,
            graph_group=graph_group,
            start_line=start_line,
            end_line=end_line,
        )
        session.add(chunk)
        setattr(chunk, "_authority_verified", True)
        return chunk


def bind_conversation_repo(
    *,
    session: Session,
    conversation_id: str,
    repo: Repo,
) -> ConversationRepo:
    if repo is None or not repo.id:
        raise RuntimeError("IDENTITY_FORGERY")
    if session.get(Repo, repo.id) is None:
        raise RuntimeError("INVALID_CONTEXT_REPO")
    existing = session.exec(
        select(ConversationRepo).where(
            ConversationRepo.conversation_id == conversation_id,
            ConversationRepo.repo_id == repo.id,
        )
    ).first()
    if existing is not None:
        return existing
    with _authority_scope():
        binding = ConversationRepo(
            conversation_id=conversation_id,
            repo_id=repo.id,
        )
        session.add(binding)
        session.flush()
        return binding


def bind_conversation_context(
    *,
    session: Session,
    conversation_id: str,
    repo: Repo | None,
) -> ConversationContext:
    repo_id = repo.id if repo is not None else None
    if repo_id is not None and session.get(Repo, repo_id) is None:
        raise RuntimeError("INVALID_CONTEXT_REPO")
    existing = session.exec(
        select(ConversationContext).where(
            ConversationContext.conversation_id == conversation_id
        )
    ).first()
    if existing is not None:
        existing.repo_id = repo_id
        session.add(existing)
        session.flush()
        return existing
    with _authority_scope():
        ctx = ConversationContext(
            conversation_id=conversation_id,
            repo_id=repo_id,
        )
        session.add(ctx)
        session.flush()
        return ctx
