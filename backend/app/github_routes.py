"""
backend.app.github_routes
=========================
FastAPI router for GitHub repository integration endpoints.

Endpoints
---------
POST   /api/chat/{conversation_id}/github/repos    Add GitHub repo to conversation context
GET    /api/chat/{conversation_id}/github/repos    List GitHub repos linked to conversation
DELETE /api/chat/{conversation_id}/github/repos/{repo_id}  Remove GitHub repo
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.app.auth import require_auth
from backend.app.database import get_session
from backend.app.models import ChatFile

router = APIRouter(prefix="/api/chat")
logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{conversation_id}/github/repos",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def add_github_repo(
    conversation_id: str,
    repo: GithubRepoRequest,
    session: Session = Depends(get_session),
) -> GithubRepoResponse:
    """
    Add a GitHub repository to the conversation context.
    For now, this stores it as a special file entry with category 'github_repo'.
    """
    # Parse owner/repo from URL
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo.repo_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid GitHub URL")

    owner, repo_name = match.groups()
    repo_name = repo_name.rstrip(".git")

    # Create a "file" entry to represent the GitHub repo
    # We use the ChatFile table but with a special category
    github_file = ChatFile(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        filename=f"{owner}/{repo_name}",
        mime_type="application/x-git-repository",
        size_bytes=0,  # Unknown size
        object_key=f"github:{repo.repo_url}@{repo.branch}",
        category="github_repo",
        included_in_context=True,
        extracted_text=(
            f"GitHub Repository: {owner}/{repo_name} (branch: {repo.branch})\nURL: {repo.repo_url}"
        ),
    )

    session.add(github_file)
    session.commit()
    session.refresh(github_file)

    return GithubRepoResponse(
        id=str(github_file.id),
        conversation_id=github_file.conversation_id,
        repo_url=repo.repo_url,
        branch=repo.branch,
        created_at=github_file.created_at.isoformat(),
    )


@router.get(
    "/{conversation_id}/github/repos",
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
                )
            )

    return result


@router.delete(
    "/{conversation_id}/github/repos/{repo_id}",
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
