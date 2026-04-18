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
from backend.app.models import ChatFile, RepoChunk
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
    conversation_id: str
    repo_url: str
    branch: str
    created_at: str
    ingestion_status: Optional[str] = None


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
            detail="GitHub token not configured. Set GITHUB_TOKEN environment variable."
        )

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_TOKEN}",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.github.com/user",
                headers=headers,
                timeout=10.0
            )

            if response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"GitHub API error: {response.text}"
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
                    status_code=response.status_code,
                    detail=f"GitHub API error: {response.text}"
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
        response = await client.get(
            url, headers=headers, params={"ref": branch}, timeout=15.0
        )
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
    repo_name = repo_name.rstrip(".git")

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
    summary = (
        f"Repo: {owner}/{repo_name}\n"
        f"Files: {len(file_paths)}\n"
        f"Top Files:\n{top_files_lines}"
    )

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
    chunk_count = 0
    for file_path, content in file_list:
        for chunk_index, chunk_text in enumerate(_split_into_chunks(content)):
            chunk = RepoChunk(
                chat_file_id=github_file.id,
                file_path=file_path,
                content=chunk_text,
                chunk_index=chunk_index,
                token_estimate=max(1, len(chunk_text) // 4),
            )
            session.add(chunk)
            chunk_count += 1

    # Set ingestion status based on whether chunks were created
    github_file.ingestion_status = "success" if chunk_count > 0 else "failed"

    session.commit()
    session.refresh(github_file)

    return GithubRepoResponse(
        id=str(github_file.id),
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
