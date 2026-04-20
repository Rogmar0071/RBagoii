"""
backend.app.github_routes
=========================
FastAPI router for GitHub repository integration endpoints.

Endpoints
---------
POST   /api/chat/{conversation_id}/github/repos    Add GitHub repo to conversation context
GET    /api/chat/{conversation_id}/github/repos    List GitHub repos linked to conversation
DELETE /api/chat/{conversation_id}/github/repos/{repo_id}  Remove GitHub repo
GET    /api/github/user/{username}/repos           List all public repos for a GitHub user
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.app.auth import require_auth
from backend.app.database import get_session
from backend.app.models import ChatFile, ConversationRepo, Repo, RepoChunk
from backend.app.repo_retrieval import _split_into_chunks

router = APIRouter()
logger = logging.getLogger(__name__)

# Get GitHub token from environment (optional - if not set, uses public API with rate limits)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class GithubRepoRequest(BaseModel):
    repo_url: str  # Full GitHub URL, e.g., https://github.com/owner/repo
    branch: str = "main"  # Default branch


class GithubRepoResponse(BaseModel):
    id: str
    repo_id: str
    conversation_id: str
    repo_url: str
    branch: str
    created_at: str
    ingestion_status: Optional[str] = None


# REPO_CONTEXT_FINALIZATION_V1 — Phase 1+2
class RepoCreateRequest(BaseModel):
    """Request body for the first-class Repo ingestion endpoint."""

    repo_url: str  # Full GitHub URL, e.g., https://github.com/owner/repo
    branch: str = "main"


class RepoStatusResponse(BaseModel):
    """Response from the first-class Repo API."""

    id: str
    repo_id: str
    conversation_id: Optional[str]
    repo_url: str
    owner: str
    name: str
    branch: str
    status: str  # pending / running / success / failed
    total_files: int
    chunk_count: int
    created_at: str
    updated_at: str


class RepoAddRequest(BaseModel):
    """Request body for the global repo upsert + bind endpoint."""

    conversation_id: str
    repo_url: str
    branch: str = "main"


class RepoAddResponse(BaseModel):
    """Response from POST /api/repos/add."""

    repo_id: str
    status: str  # pending / running / success / failed


class GithubRepoListItem(BaseModel):
    """Repository information from GitHub API."""

    name: str
    full_name: str
    description: Optional[str]
    html_url: str
    default_branch: str
    private: bool
    language: Optional[str]
    stargazers_count: int
    updated_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/github/user",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
async def get_authenticated_user():
    """
    Get the authenticated GitHub user information.
    Returns an error if no GITHUB_TOKEN is configured.
    """
    if not GITHUB_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="GitHub token not configured. Set GITHUB_TOKEN environment variable.",
        )

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_TOKEN}",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.github.com/user", headers=headers, timeout=10.0
            )

            if response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code, detail=f"GitHub API error: {response.text}"
                )

            user_data = response.json()
            return {
                "login": user_data.get("login"),
                "name": user_data.get("name"),
                "avatar_url": user_data.get("avatar_url"),
                "public_repos": user_data.get("public_repos", 0),
            }
    except httpx.RequestError as e:
        logger.error(f"GitHub API request failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to connect to GitHub API")


@router.get(
    "/api/github/user/{username}/repos",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
async def list_user_repos(
    username: str,
    page: int = 1,
    per_page: int = 30,
) -> List[GithubRepoListItem]:
    """
    List all public repositories for a GitHub user.
    If GITHUB_TOKEN is set, also includes private repos if the token has access.
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    url = f"https://api.github.com/users/{username}/repos"
    params = {
        "page": page,
        "per_page": min(per_page, 100),  # GitHub API max is 100
        "sort": "updated",
        "direction": "desc",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=10.0)

            if response.status_code == 404:
                raise HTTPException(status_code=404, detail=f"GitHub user '{username}' not found")
            elif response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code, detail=f"GitHub API error: {response.text}"
                )

            repos_data = response.json()
            return [
                GithubRepoListItem(
                    name=repo["name"],
                    full_name=repo["full_name"],
                    description=repo.get("description"),
                    html_url=repo["html_url"],
                    default_branch=repo.get("default_branch", "main"),
                    private=repo.get("private", False),
                    language=repo.get("language"),
                    stargazers_count=repo.get("stargazers_count", 0),
                    updated_at=repo.get("updated_at", ""),
                )
                for repo in repos_data
            ]
    except httpx.RequestError as e:
        logger.error(f"GitHub API request failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to connect to GitHub API")


_ALLOWED_EXTENSIONS = {".py", ".kt", ".java", ".ts", ".js", ".json", ".md"}
_MAX_FILES = 50
_MAX_DEPTH = 3
_MAX_FILE_CHARS = 5000


async def _fetch_repo_file_list(
    owner: str,
    repo_name: str,
    branch: str,
    headers: dict,
) -> list[tuple[str, str]]:
    """
    Recursively fetch text files from a GitHub repository.

    Returns a list of ``(file_path, content)`` tuples, one per fetched file.
    Depth is limited to _MAX_DEPTH levels and at most _MAX_FILES files are
    collected.  Exceptions (including ``RuntimeError`` on GitHub API errors)
    are propagated to the caller.
    """
    files: list[tuple[str, str]] = []
    file_count = 0

    async def _traverse(path: str, depth: int, client: httpx.AsyncClient) -> None:
        nonlocal file_count
        if depth > _MAX_DEPTH or file_count >= _MAX_FILES:
            return

        base = f"https://api.github.com/repos/{owner}/{repo_name}/contents"
        url = f"{base}/{path}" if path else base
        response = await client.get(url, headers=headers, params={"ref": branch}, timeout=15.0)
        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub API returned {response.status_code} for {url}: {response.text[:200]}"
            )

        items = response.json()
        if isinstance(items, dict):
            items = [items]

        for item in items:
            if file_count >= _MAX_FILES:
                break
            if item["type"] == "file":
                ext = os.path.splitext(item["name"])[1].lower()
                if ext not in _ALLOWED_EXTENSIONS:
                    continue
                download_url = item.get("download_url")
                if not download_url:
                    continue
                raw_resp = await client.get(download_url, timeout=15.0)
                if raw_resp.status_code != 200:
                    logger.warning(
                        "Failed to fetch %s: HTTP %s", item["path"], raw_resp.status_code
                    )
                    continue
                content = raw_resp.text[:_MAX_FILE_CHARS]
                files.append((item["path"], content))
                file_count += 1
            elif item["type"] == "dir":
                await _traverse(item["path"], depth + 1, client)

    async with httpx.AsyncClient() as client:
        await _traverse("", 1, client)

    return files


async def _fetch_repo_contents(
    owner: str,
    repo_name: str,
    branch: str,
    headers: dict,
) -> str:
    """
    Recursively fetch text files from a GitHub repository and return them
    aggregated as a single string.  Depth is limited to _MAX_DEPTH levels and
    at most _MAX_FILES files are collected.

    Returns a REPO_FETCH_FAILED message on any GitHub API error.
    """
    try:
        files = await _fetch_repo_file_list(owner, repo_name, branch, headers)
    except Exception as exc:
        logger.error("GitHub repo fetch failed for %s/%s: %s", owner, repo_name, exc)
        return f"REPO_FETCH_FAILED: {owner}/{repo_name} — {exc}"

    if not files:
        return (
            f"GitHub Repository: {owner}/{repo_name} (branch: {branch})"
            " — no supported source files found."
        )
    parts = [f"---\nFILE: {path}\n{content}\n" for path, content in files]
    return "\n".join(parts)


@router.post(
    "/api/chat/{conversation_id}/github/repos",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def add_github_repo(
    conversation_id: str,
    repo: GithubRepoRequest,
    session: Session = Depends(get_session),
) -> GithubRepoResponse:
    """
    Add a GitHub repository to the conversation context.

    Fetches repository file contents, stores them as RepoChunk rows for
    selective retrieval (REPO_CONTEXT_SELECTIVE_RETRIEVAL_LAYER_V1), and
    creates a ChatFile with category 'github_repo' whose extracted_text is
    reduced to a compact summary (file list only).
    """
    # Parse owner/repo from URL
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo.repo_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid GitHub URL")

    owner, repo_name = match.groups()
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    headers: dict = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        file_list = await _fetch_repo_file_list(owner, repo_name, repo.branch, headers)
    except Exception as exc:
        logger.error("GitHub repo fetch failed for %s/%s: %s", owner, repo_name, exc)
        raise HTTPException(
            status_code=422,
            detail={
                "status": "failed",
                "reason": "repo_ingestion_failed",
                "detail": f"No files retrieved from repository: {exc}",
            },
        )

    if not file_list:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "failed",
                "reason": "repo_ingestion_failed",
                "detail": "No files retrieved from repository",
            },
        )

    # Build compact summary for extracted_text (not full content)
    file_paths = [path for path, _ in file_list]
    top_paths = file_paths[:10]
    top_files_lines = "\n".join(f"- {p}" for p in top_paths)
    summary = f"Repo: {owner}/{repo_name}\nFiles: {len(file_paths)}\nTop Files:\n{top_files_lines}"

    github_file = ChatFile(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        filename=f"{owner}/{repo_name}",
        mime_type="application/x-git-repository",
        size_bytes=len(summary.encode("utf-8")),
        object_key=f"github:{repo.repo_url}@{repo.branch}",
        category="github_repo",
        included_in_context=True,
        extracted_text=summary,
    )

    session.add(github_file)
    session.flush()  # Assign github_file.id before creating chunks

    # Store file content as RepoChunk rows for selective retrieval
    from backend.app.repo_chunk_extractor import extract_structure

    chunk_count = 0
    for file_path, content in file_list:
        for chunk_index, chunk_text in enumerate(_split_into_chunks(content)):
            structure = extract_structure(chunk_text, file_path)
            chunk = RepoChunk(
                chat_file_id=github_file.id,
                file_path=file_path,
                content=chunk_text,
                chunk_index=chunk_index,
                token_estimate=max(1, len(chunk_text) // 4),
                chunk_type=structure["chunk_type"],
                symbol=structure["symbol"],
                dependencies=structure["dependencies"],
                graph_group=structure["graph_group"],
                start_line=structure["start_line"],
                end_line=structure["end_line"],
            )
            session.add(chunk)
            chunk_count += 1

    # Set ingestion status based on whether chunks were created
    github_file.ingestion_status = "success" if chunk_count > 0 else "failed"

    session.commit()
    session.refresh(github_file)

    return GithubRepoResponse(
        id=str(github_file.id),
        repo_id=str(github_file.id),
        conversation_id=github_file.conversation_id,
        repo_url=repo.repo_url,
        branch=repo.branch,
        created_at=github_file.created_at.isoformat(),
        ingestion_status=github_file.ingestion_status,
    )


@router.get(
    "/api/chat/{conversation_id}/github/repos",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def list_github_repos(
    conversation_id: str,
    session: Session = Depends(get_session),
) -> List[GithubRepoResponse]:
    """List all GitHub repositories linked to the conversation."""
    stmt = (
        select(ChatFile)
        .where(ChatFile.conversation_id == conversation_id)
        .where(ChatFile.category == "github_repo")
        .order_by(ChatFile.created_at.desc())
    )
    repos = session.exec(stmt).all()

    result = []
    for repo in repos:
        # Parse repo URL and branch from object_key
        match = re.search(r"github:(.+)@(.+)", repo.object_key)
        if match:
            repo_url, branch = match.groups()
            result.append(
                GithubRepoResponse(
                    id=str(repo.id),
                    repo_id=str(repo.id),
                    conversation_id=repo.conversation_id,
                    repo_url=repo_url,
                    branch=branch,
                    created_at=repo.created_at.isoformat(),
                    ingestion_status=repo.ingestion_status,
                )
            )

    return result


@router.delete(
    "/api/chat/{conversation_id}/github/repos/{repo_id}",
    status_code=204,
    dependencies=[Depends(require_auth)],
)
def remove_github_repo(
    conversation_id: str,
    repo_id: str,
    session: Session = Depends(get_session),
):
    """Remove a GitHub repository from the conversation."""
    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo ID")

    stmt = select(ChatFile).where(
        ChatFile.id == repo_uuid,
        ChatFile.conversation_id == conversation_id,
        ChatFile.category == "github_repo",
    )
    repo = session.exec(stmt).first()

    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    session.delete(repo)
    session.commit()

    return None


# ===========================================================================
# REPO_CONTEXT_FINALIZATION_V1 — Phase 1+2+8
# First-class Repo endpoints (async ingestion pipeline)
# ===========================================================================


def _enqueue_repo_ingestion(repo_id: str) -> None:
    """
    DEPRECATED: This function is no longer used by any endpoints.
    All repo ingestion now goes through the unified pipeline.

    This function is kept temporarily for backward compatibility but will be
    removed in a future release. Do not use for new code.

    Use the unified pipeline instead:
    - Create IngestJob with kind="repo"
    - Call ingest_pipeline._transition() and ingest_routes._enqueue()

    ---

    Enqueue or synchronously run repo ingestion.

    CONTRACT: MQP-CONTRACT:RQ_EXECUTION_SPINE_LOCK_V5 §2
    Queue always stores the stable string path "backend.app.job_runner.execute_job"
    with job name "run_repo_ingestion" — never a function object.

    CONTRACT: MQP-CONTRACT:REPO_INGESTION_REBUILD_V1 Phase 3
    Uses a deterministic job_id to prevent duplicate enqueue.  When Redis is
    available the function checks whether a job with that id is already queued
    or running before submitting a new one.
    """
    import os as _os
    import warnings

    # DEPRECATION WARNING
    warnings.warn(
        "_enqueue_repo_ingestion() is deprecated. Use the unified pipeline instead.",
        DeprecationWarning,
        stacklevel=2
    )
    logger.warning({
        "event": "deprecated_function_called",
        "function": "_enqueue_repo_ingestion",
        "repo_id": repo_id,
        "message": (
            "This function is deprecated. "
            "Use unified pipeline (IngestJob + process_ingest_job)."
        )
    })

    disable = _os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1"
    if disable:
        # In test/DISABLE_JOBS mode: run inline in a background thread so the
        # API returns immediately but the DB gets populated before test assertions.
        from concurrent.futures import ThreadPoolExecutor as _TPE

        from backend.app.worker import run_repo_ingestion

        e = _TPE(max_workers=1)
        e.submit(run_repo_ingestion, repo_id)
        e.shutdown(wait=True)  # tests need it to complete synchronously
        return

    redis_url = _os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        from redis import Redis
        from rq.exceptions import NoSuchJobError
        from rq.job import Job as RQJob

        conn = Redis.from_url(redis_url)
        job_id = f"repo_ingestion_{repo_id}"

        # Phase 3 — queue discipline: skip if job is already queued or running.
        try:
            existing = RQJob.fetch(job_id, connection=conn)
            status = existing.get_status()
            if status in ("queued", "started"):
                logger.info(
                    {"event": "skipped_duplicate", "repo_id": repo_id, "job_status": str(status)}
                )
                return
        except NoSuchJobError:
            pass  # No existing job — proceed to enqueue.

        # MQP-CONTRACT:QUEUE_SINGLE_PATH_ENFORCEMENT_V1 §2 — Use single entry point
        from backend.app.worker import enqueue_job

        enqueue_job(repo_id, "run_repo_ingestion", rq_job_id=job_id)
        logger.info({"event": "job_enqueued", "repo_id": repo_id, "job_id": job_id})
        return

    # No Redis and jobs enabled — run in a background thread (non-blocking).
    from concurrent.futures import ThreadPoolExecutor as _TPE

    from backend.app.worker import run_repo_ingestion

    e = _TPE(max_workers=1)
    e.submit(run_repo_ingestion, repo_id)
    e.shutdown(wait=False)


@router.post(
    "/api/chat/{conversation_id}/repos",
    status_code=410,
    dependencies=[Depends(require_auth)],
)
def create_repo_ingestion_job(
    conversation_id: str,
    repo: RepoCreateRequest,
    session: Session = Depends(get_session),
) -> None:
    """
    GLOBAL_REPO_ASSET_SYSTEM_LOCK_V1 — DEPRECATED.

    This endpoint has been permanently retired.  Use POST /api/repos/add instead.
    """
    raise HTTPException(
        status_code=410,
        detail="DEPRECATED — use /api/repos/add",
    )


@router.get(
    "/api/chat/{conversation_id}/repos",
    status_code=200,
    dependencies=[Depends(require_auth)],
)
def list_repos(
    conversation_id: str,
    session: Session = Depends(get_session),
) -> List[RepoStatusResponse]:
    """
    GLOBAL_REPO_ASSET_SYSTEM_LOCK_V1.
    List all Repo entities bound to the conversation via ConversationRepo.
    """
    stmt = (
        select(Repo)
        .join(ConversationRepo, ConversationRepo.repo_id == Repo.id)
        .where(ConversationRepo.conversation_id == conversation_id)
        .order_by(Repo.created_at.desc())
    )
    repos = session.exec(stmt).all()
    return [
        RepoStatusResponse(
            id=str(r.id),
            repo_id=str(r.id),
            conversation_id=conversation_id,
            repo_url=r.repo_url,
            owner=r.owner,
            name=r.name,
            branch=r.branch,
            status=r.ingestion_status,
            total_files=r.total_files,
            chunk_count=r.total_chunks,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
        )
        for r in repos
    ]


@router.delete(
    "/api/chat/{conversation_id}/repos/{repo_id}",
    status_code=204,
    dependencies=[Depends(require_auth)],
)
def remove_repo(
    conversation_id: str,
    repo_id: str,
    session: Session = Depends(get_session),
):
    """
    GLOBAL_REPO_ASSET_SYSTEM_LOCK_V1.
    Remove a first-class Repo and all its RepoChunk rows.
    The conversation_id path parameter is accepted for API compatibility but
    is not used to filter the lookup — repos are now global assets.
    """
    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo ID")

    repo = session.get(Repo, repo_uuid)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    # Delete associated chunks first
    chunk_stmt = select(RepoChunk).where(RepoChunk.repo_id == repo_uuid)
    for chunk in session.exec(chunk_stmt).all():
        session.delete(chunk)

    # Remove ConversationRepo bindings
    binding_stmt = select(ConversationRepo).where(ConversationRepo.repo_id == repo_uuid)
    for binding in session.exec(binding_stmt).all():
        session.delete(binding)

    session.delete(repo)
    session.commit()
    return None


@router.post(
    "/api/repos/{repo_id}/retry",
    status_code=202,
    dependencies=[Depends(require_auth)],
)
def retry_repo_ingestion(
    repo_id: str,
    session: Session = Depends(get_session),
) -> RepoStatusResponse:
    """
    REPO_CONTEXT_FINALIZATION_V1 — Phase 8.

    Retry ingestion for a failed (or stuck) Repo.
    Resets status to "pending" and re-enqueues via unified pipeline.

    MIGRATION: Now uses unified IngestJob pipeline instead of legacy run_repo_ingestion().
    """
    try:
        repo_uuid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo ID")

    repo = session.get(Repo, repo_uuid)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    # LAW 6 — HARD FAIL: reject retry while ingestion is already in progress.
    # Interrupting a running job would delete its in-progress chunks and leave
    # the system in an inconsistent state.
    if repo.ingestion_status == "running":
        raise HTTPException(
            status_code=409,
            detail=(
                f"REPO_INGESTION_RUNNING: {repo.id} — "
                "cannot retry while ingestion is in progress"
            ),
        )

    # Delete stale chunks from previous attempt (both repo_id and ingest_job_id linked)
    chunk_stmt = select(RepoChunk).where(RepoChunk.repo_id == repo_uuid)
    for chunk in session.exec(chunk_stmt).all():
        session.delete(chunk)

    repo.ingestion_status = "pending"
    repo.total_files = 0
    repo.total_chunks = 0
    session.add(repo)
    session.commit()
    session.refresh(repo)

    # Use unified pipeline: ingest_repo handles fetch, blob storage, and state transitions.
    from backend.app.ingest_routes import IngestRepoRequest, ingest_repo
    from backend.app.models import IngestJob

    ingest_req = IngestRepoRequest(
        repo_url=repo.repo_url,
        branch=repo.branch or "main",
        conversation_id=repo.conversation_id or "",
        workspace_id=None,
        force_refresh=True,
    )

    try:
        ingest_response = ingest_repo(ingest_req, session)
        job_id = uuid.UUID(ingest_response.job_id)

        ingest_job = session.get(IngestJob, job_id)
        if ingest_job and ingest_job.status in ("success", "failed"):
            repo.ingestion_status = ingest_job.status
            repo.total_files = ingest_job.file_count or 0
            repo.total_chunks = ingest_job.chunk_count or 0
            session.add(repo)

            if ingest_job.status == "success":
                chunks = session.exec(
                    select(RepoChunk).where(RepoChunk.ingest_job_id == job_id)
                ).all()
                for chunk in chunks:
                    chunk.repo_id = repo_uuid
                    session.add(chunk)

            session.commit()
            session.refresh(repo)

    except HTTPException:
        logger.warning("Retry ingestion failed for repo %s; marking as failed", repo_id)
        try:
            repo.ingestion_status = "failed"
            session.add(repo)
            session.commit()
            session.refresh(repo)
        except Exception:
            pass
    except Exception as exc:
        logger.error("Retry ingestion error for repo %s: %s", repo_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info({"event": "retry_job_completed", "repo_id": repo_id})

    return RepoStatusResponse(
        id=str(repo.id),
        repo_id=str(repo.id),
        conversation_id=repo.conversation_id,
        repo_url=repo.repo_url,
        owner=repo.owner,
        name=repo.name,
        branch=repo.branch,
        status=repo.ingestion_status,
        total_files=repo.total_files,
        chunk_count=repo.total_chunks,
        created_at=repo.created_at.isoformat(),
        updated_at=repo.updated_at.isoformat(),
    )


# ===========================================================================
# Global repo upsert + conversation binding
# GLOBAL_REPO_ASSET_INGESTION_AND_CONTEXT_BINDING_V1
# ===========================================================================


@router.post("/api/repos/add", response_model=RepoAddResponse)
def add_repo(
    req: RepoAddRequest,
    session: Session = Depends(get_session),
) -> RepoAddResponse:
    """
    MQP-CONTRACT: REPO_INGESTION_REBUILD_V1

    Phase 1 — Canonical GitHub resolution: parse URL with regex then confirm
              identity via the GitHub API.  Owner and name are ALWAYS taken
              from the API response, never from user input.
    Phase 2 — Atomic DB transaction: find-or-create Repo by (repo_url, branch)
              and idempotently bind to conversation.  Single commit on exit.
    Phase 3 — Queue discipline: enqueue ingestion only when no job is already
              queued or running for this repo.
    """
    logger.info({"event": "add_repo_entered", "repo_url": req.repo_url})

    # -----------------------------------------------------------------------
    # Phase 1 — Canonical repo resolution via GitHub API
    # -----------------------------------------------------------------------
    match = re.search(r"github\.com/([^/]+)/([^/]+)", req.repo_url)
    if not match:
        raise HTTPException(status_code=400, detail="INVALID_REPO")

    raw_owner, raw_repo = match.groups()
    # Strip .git suffix from the initial URL parse — this is the ONLY point
    # at which we tolerate user-supplied string mutation.
    if raw_repo.endswith(".git"):
        raw_repo = raw_repo[:-4]

    _gh_headers: dict = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        _gh_headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        with httpx.Client(timeout=10.0) as _client:
            _gh_resp = _client.get(
                f"https://api.github.com/repos/{raw_owner}/{raw_repo}",
                headers=_gh_headers,
            )
    except httpx.RequestError as exc:
        logger.error({"event": "canonical_resolution_failed", "error": str(exc)})
        raise HTTPException(status_code=400, detail="INVALID_REPO")

    if _gh_resp.status_code != 200:
        logger.warning(
            {
                "event": "canonical_resolution_failed",
                "github_status": _gh_resp.status_code,
            }
        )
        raise HTTPException(status_code=400, detail="INVALID_REPO")

    _gh_data = _gh_resp.json()
    try:
        owner: str = _gh_data["owner"]["login"]
        repo_name: str = _gh_data["name"]
    except (KeyError, TypeError):
        logger.error(
            {"event": "canonical_resolution_invalid", "body": str(_gh_data)[:200]}
        )
        raise HTTPException(status_code=400, detail="INVALID_REPO")

    logger.info({"event": "canonical_resolved", "owner": owner, "name": repo_name})

    # -----------------------------------------------------------------------
    # Validate conversation_id before touching the DB
    # -----------------------------------------------------------------------
    if not req.conversation_id:
        raise HTTPException(status_code=400, detail="MISSING_CONVERSATION_ID")

    # -----------------------------------------------------------------------
    # Phase 2 — Atomic upsert (single transaction, single commit on block exit)
    # -----------------------------------------------------------------------
    branch = ((req.branch or "main").strip()) or "main"
    newly_created = False
    repo_id_str: str
    current_status: str

    with session.begin():
        repo = session.exec(
            select(Repo).where(
                Repo.repo_url == req.repo_url,
                Repo.branch == branch,
            )
        ).first()

        if repo is None:
            repo = Repo(
                id=uuid.uuid4(),
                repo_url=req.repo_url,
                owner=owner,
                name=repo_name,
                branch=branch,
                ingestion_status="pending",
                total_files=0,
                total_chunks=0,
            )
            session.add(repo)
            session.flush()  # assign PK before binding FK
            newly_created = True
            logger.info(
                {
                    "event": "repo_created",
                    "repo_id": str(repo.id),
                    "owner": owner,
                    "name": repo_name,
                }
            )
        else:
            logger.info(
                {
                    "event": "repo_found",
                    "repo_id": str(repo.id),
                    "owner": repo.owner,
                    "name": repo.name,
                }
            )

        existing_binding = session.exec(
            select(ConversationRepo).where(
                ConversationRepo.conversation_id == req.conversation_id,
                ConversationRepo.repo_id == repo.id,
            )
        ).first()

        if existing_binding is None:
            session.add(
                ConversationRepo(
                    id=uuid.uuid4(),
                    conversation_id=req.conversation_id,
                    repo_id=repo.id,
                )
            )
            logger.info(
                {
                    "event": "binding_created",
                    "conversation_id": req.conversation_id,
                    "repo_id": str(repo.id),
                }
            )
        else:
            logger.info(
                {
                    "event": "binding_exists",
                    "conversation_id": req.conversation_id,
                    "repo_id": str(repo.id),
                }
            )

        repo_id_str = str(repo.id)
        current_status = repo.ingestion_status
        # Single commit happens on exit from `with session.begin()` block.

    # -----------------------------------------------------------------------
    # Phase 3 — Queue discipline (after transaction is committed)
    # -----------------------------------------------------------------------
    # MIGRATION: Use unified ingestion pipeline (IngestJob + process_ingest_job)
    # instead of legacy run_repo_ingestion() path.
    # Maintains Repo table for backward compatibility but uses unified pipeline.

    if not newly_created and current_status in ("running", "success"):
        # Repo is already being ingested or fully ingested — idempotent guard.
        raise HTTPException(
            status_code=409,
            detail=f"REPO_INGESTION_{current_status.upper()}",
        )

    if newly_created or current_status in ("pending", "failed"):
        # Create IngestJob via unified pipeline endpoint
        # This ensures proper blob storage and state transitions
        from backend.app.ingest_routes import IngestRepoRequest, ingest_repo
        from backend.app.models import IngestJob

        ingest_req = IngestRepoRequest(
            repo_url=req.repo_url,
            branch=branch,
            conversation_id=req.conversation_id,
            workspace_id=None,
            force_refresh=False
        )

        try:
            # Use the unified ingest endpoint to handle repo fetching and storage
            ingest_response = ingest_repo(ingest_req, session)
            job_id = uuid.UUID(ingest_response.job_id)

            ingest_job = session.get(IngestJob, job_id)
            if ingest_job:
                # MQP-CONTRACT: Sync Repo.ingestion_status with IngestJob terminal state.
                if ingest_job.status in ("success", "failed"):
                    repo_obj = session.get(Repo, uuid.UUID(repo_id_str))
                    if repo_obj:
                        repo_obj.ingestion_status = ingest_job.status
                        repo_obj.total_files = ingest_job.file_count or 0
                        repo_obj.total_chunks = ingest_job.chunk_count or 0
                        session.add(repo_obj)
                        current_status = ingest_job.status

                        # Bind RepoChunk.repo_id for chunks created by this IngestJob.
                        if ingest_job.status == "success":
                            from sqlmodel import select as _select
                            chunks = session.exec(
                                _select(RepoChunk).where(
                                    RepoChunk.ingest_job_id == job_id
                                )
                            ).all()
                            for chunk in chunks:
                                chunk.repo_id = uuid.UUID(repo_id_str)
                                session.add(chunk)

                session.commit()

            logger.info({
                "event": "ingest_job_linked",
                "job_id": str(job_id),
                "repo_id": repo_id_str,
            })

        except HTTPException:
            # Ingestion failed — mark repo as failed but return 200 to caller.
            logger.warning(
                "Ingestion failed for repo %s; marking repo as failed", repo_id_str
            )
            try:
                repo_obj = session.get(Repo, uuid.UUID(repo_id_str))
                if repo_obj:
                    repo_obj.ingestion_status = "failed"
                    session.add(repo_obj)
                    session.commit()
            except Exception:
                pass
            current_status = "failed"
        except Exception as exc:
            logger.error("Failed to create ingest job for repo %s: %s", repo_id_str, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RepoAddResponse(
        repo_id=repo_id_str,
        status=current_status,
    )
