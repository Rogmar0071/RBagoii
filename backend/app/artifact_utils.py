"""
backend.app.artifact_utils
==========================
Pure stateless helpers for normalizing and injecting user-supplied artifacts
into AI system prompts.

Contract: ARTIFACT_INGESTION_PIPELINE_V1 (MQP-PHASE-B)

Rules (non-negotiable):
- Artifacts are NEVER summarized, truncated, or preprocessed.
- This module holds NO state.
- All reasoning on artifact content happens in the AI layer, not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Human-readable label for each supported artifact type.
_TYPE_LABELS: dict[str, str] = {
    "file": "File",
    "text": "Text",
    "repo": "Repository",
}


class ArtifactItem(BaseModel):
    """A single user-provided artifact.

    Schema:
      type    — one of "file" | "text" | "repo"
      name    — human-readable identifier (filename, snippet title, repo URL, …)
      content — raw content as a string; no transformation is applied
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["file", "text", "repo"]
    name: str
    content: str


def build_artifact_context_block(artifacts: list[ArtifactItem]) -> str:
    """Return a prompt-injection block for the explicitly provided artifacts.

    Returns an empty string when *artifacts* is empty.
    Artifacts are passed through verbatim — no summarization, no truncation.
    """
    if not artifacts:
        return ""

    parts = ["--- BEGIN PROVIDED ARTIFACTS ---"]
    for artifact in artifacts:
        label = _TYPE_LABELS.get(artifact.type, artifact.type.capitalize())
        parts.append(f"{label}: {artifact.name}")
        parts.append(artifact.content)
        parts.append("---")

    # Replace the trailing separator with the closing marker.
    parts[-1] = "--- END PROVIDED ARTIFACTS ---"
    return "\n".join(parts)
