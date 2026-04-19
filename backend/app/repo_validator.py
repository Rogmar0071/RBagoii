"""
backend.app.repo_validator
==========================
REPO_VALIDATION_LAYER_V1

Stateless validation engine that classifies an ingested Repo as a trust-
class asset based on signals derived from its RepoChunk rows.

Invariants
----------
- NEVER calls the GitHub API.
- NEVER mutates ingestion data (ingestion_status, total_files, total_chunks, …).
- Pure functions: given the same repo + chunks they always return the same result.
- Only called when ingestion_status == "success".
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def _extract_signals(repo: Any, chunks: List[Any]) -> Dict:
    """Derive a set of boolean/numeric signals from the chunk file paths."""
    file_paths = [c.file_path for c in chunks]

    return {
        "has_readme": any("readme" in p.lower() for p in file_paths),
        "has_license": any("license" in p.lower() for p in file_paths),
        "file_count": len(file_paths),
        "code_files": sum(
            1
            for p in file_paths
            if p.endswith((".py", ".js", ".ts", ".kt", ".java", ".go", ".rs"))
        ),
        "has_build_files": any(
            p.endswith(("requirements.txt", "package.json", "Dockerfile"))
            for p in file_paths
        ),
        "has_ci": any(".github/workflows" in p for p in file_paths),
        "markdown_ratio": (
            sum(1 for p in file_paths if p.endswith(".md")) / max(len(file_paths), 1)
        ),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(signals: Dict) -> int:
    """Convert signals to a 0–100 integer quality score."""
    score = 0

    if signals["has_readme"]:
        score += 10
    if signals["has_license"]:
        score += 10
    if signals["file_count"] > 50:
        score += 20
    if signals["code_files"] > 30:
        score += 20
    if signals["has_build_files"]:
        score += 10
    if signals["has_ci"]:
        score += 10

    if signals["markdown_ratio"] > 0.7:
        score -= 20

    if signals["file_count"] == 0:
        score -= 50

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Trust classification
# ---------------------------------------------------------------------------


def _classify(score: int) -> str:
    """Map a numeric score to a trust class string."""
    if score >= 80:
        return "TRUTH"
    elif score >= 50:
        return "REFERENCE"
    elif score >= 20:
        return "WIP"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_repo(repo: Any, chunks: List[Any]) -> Dict:
    """
    Run the full validation pipeline for *repo* against its *chunks*.

    Returns a dict with keys:
        signals   – raw signal dict from ``_extract_signals``
        score     – integer 0–100
        trust_class – one of TRUTH / REFERENCE / WIP / UNKNOWN
    """
    signals = _extract_signals(repo, chunks)
    score = _score(signals)
    trust_class = _classify(score)

    return {
        "signals": signals,
        "score": score,
        "trust_class": trust_class,
    }
