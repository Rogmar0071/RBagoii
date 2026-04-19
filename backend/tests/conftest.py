"""
backend/tests/conftest.py
=========================
Shared pytest fixtures for the backend test suite.

GitHub API stub
---------------
``add_repo()`` and ``run_repo_ingestion()`` now call the GitHub REST API to
confirm canonical repo identity before writing to the DB or running ingestion.
The ``_mock_github_api_metadata`` autouse fixture patches ``httpx.Client`` so
those calls never hit the real network during unit tests.

The stub parses the request URL and returns a synthetic 200 response whose
``{"owner": {"login": ...}, "name": ...}`` payload matches the owner/repo
extracted from the GitHub URL.  This keeps existing test assertions
(``repo.owner``, ``repo.name``) correct without any test-level changes.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_github_response(url: str, **kwargs) -> MagicMock:
    """Return a synthetic GitHub API response based on the request URL."""
    resp = MagicMock()
    m = re.search(r"api\.github\.com/repos/([^/?]+)/([^/?]+)", url)
    if m:
        owner, name = m.group(1), m.group(2)
        resp.status_code = 200
        resp.json.return_value = {"owner": {"login": owner}, "name": name}
    else:
        resp.status_code = 200
        resp.json.return_value = {}
    return resp


def _make_fake_client(*args, **kwargs) -> MagicMock:
    """Factory that returns a mock httpx.Client acting as a context manager."""
    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = _make_fake_github_response
    return client


@pytest.fixture(autouse=True)
def _mock_github_api_metadata():
    """
    Auto-patch ``httpx.Client`` to prevent real GitHub API calls during tests.

    Affects both ``add_repo()`` (canonical resolution, Phase 1) and
    ``run_repo_ingestion()`` (worker hard validation, Phase 4).
    Test-local patches applied with ``with patch(...)`` take precedence and
    are unaffected by this fixture.
    """
    with patch("httpx.Client", side_effect=_make_fake_client):
        yield
