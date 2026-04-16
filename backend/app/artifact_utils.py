"""
backend.app.artifact_utils
==========================
Pure stateless helpers for normalizing and injecting user-supplied artifacts
into AI system prompts.

Contracts:
  ARTIFACT_INGESTION_PIPELINE_V1 (MQP-PHASE-B)
  CONTEXT_ASSEMBLY_ALIGNMENT_V2
  CONTEXT_ORIGIN_ENFORCEMENT_V1

Rules (non-negotiable):
- Artifacts are NEVER summarized, truncated, or preprocessed.
- This module holds NO state.
- All reasoning on artifact content happens in the AI layer, not here.
- Context resolution is deterministic and performs zero external I/O.
"""

from __future__ import annotations

from typing import Literal, Optional

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

    parts = ["--- BEGIN USER-PROVIDED DATA ---"]
    for artifact in artifacts:
        label = _TYPE_LABELS.get(artifact.type, artifact.type.capitalize())
        parts.append(f"{label}: {artifact.name}")
        parts.append(artifact.content)
        parts.append("---")

    # Replace the trailing separator with the closing marker.
    parts[-1] = "--- END USER-PROVIDED DATA ---"
    return "\n".join(parts)


def resolve_context_surface(
    *,
    context_scope: str,
    project_id: Optional[str],
    artifacts: list[ArtifactItem],
) -> dict:
    """Deterministic context resolver for CONTEXT_ASSEMBLY_ALIGNMENT_V2.

    Maps UI-declared scope + project_id + artifacts to a resolved surface dict.
    Performs ZERO external I/O — no DB access, no file access, no retrieval.

    Parameters
    ----------
    context_scope:
        One of "global" or "project".  Validation (422 on invalid values and on
        project scope without project_id) is enforced in the caller layer.
    project_id:
        Required when context_scope == "project"; None otherwise.
    artifacts:
        User-provided artifacts (already normalized).

    Returns
    -------
    {
        "resolved_artifacts": list[ArtifactItem],
        "scope": str,
        "project_id": Optional[str],
    }

    Notes
    -----
    resolved_artifacts is identical to *artifacts* in both scopes by design:
    no persistence layer exists, so no stored artifacts can be injected.
    """
    return {
        "resolved_artifacts": artifacts,
        "scope": context_scope,
        "project_id": project_id,
    }


def resolve_context_origin(
    *,
    raw_context_scope: Optional[str],
) -> tuple[str, str]:
    """Classify the context origin for CONTEXT_ORIGIN_ENFORCEMENT_V1.

    Separates what the system RECEIVED from what the system ASSUMED.
    This is an internal classification — never exposed to UI or AI.

    Parameters
    ----------
    raw_context_scope:
        The raw value from the incoming request body, or None if omitted.

    Returns
    -------
    (context_scope, context_origin)
        context_scope  — resolved scope string ("global" or "project")
        context_origin — "explicit" if the caller provided context_scope,
                         "implicit_legacy" if it was absent (backend defaulted)
    """
    if raw_context_scope is None:
        return "global", "implicit_legacy"
    return raw_context_scope, "explicit"
