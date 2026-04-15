"""
backend.app.mutation_routes
============================
FastAPI router for MUTATION_GOVERNANCE_EXECUTION_V1 endpoints.

Endpoints
---------
POST /api/mutations/propose
    Submit a user intent to the mutation governance pipeline.
    Returns a structured mutation proposal (PROPOSAL ONLY — no execution).

All endpoints require ``Authorization: Bearer <API_KEY>`` when API_KEY is set.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.auth import require_auth
from backend.app.mutation_governance import (
    MutationGovernanceResult,
    mutation_governance_gateway,
)

router = APIRouter(prefix="/api", tags=["mutations"])

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("OPENAI_MODEL_CHAT", "gpt-4.1-mini")
_DEFAULT_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
_DEFAULT_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class MutationProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(..., description="User intent describing the desired mutation")
    modes: list[str] | None = Field(
        default=None,
        description=(
            "Optional active mode list. Governance modes "
            "(strict_mode, prediction_mode, builder_mode) are always enforced."
        ),
    )

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("intent is required and must not be empty.")
        return text


# ---------------------------------------------------------------------------
# AI call factory
# ---------------------------------------------------------------------------


def _build_ai_call() -> Any:
    """Return an ``ai_call`` callable for the mutation governance gateway.

    Uses the OpenAI API when ``OPENAI_API_KEY`` is set.  Falls back to a
    stub that returns a blocked proposal indicating AI is not configured.
    """
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        # Stub: returns the correct hybrid text+JSON format.
        # target_files is intentionally empty so the proposal is blocked by
        # stage_1 structural validation, exercising the full governance pipeline.
        def _stub(system_prompt: str) -> str:  # noqa: ARG001
            contract_json = json.dumps(
                {
                    "target_files": [],
                    "operation_type": "update_file",
                    "proposed_changes": (
                        "[Stub] OPENAI_API_KEY not configured. "
                        "Cannot generate a real mutation proposal."
                    ),
                    "assumptions": ["OPENAI_API_KEY is not set on this server"],
                    "alternatives": [
                        "Configure OPENAI_API_KEY to enable AI mutation proposals"
                    ],
                    "confidence": "low",
                    "risks": ["No real proposal generated — AI not available"],
                    "missing_data": ["OPENAI_API_KEY required"],
                },
                indent=2,
            )
            return (
                "SECTION_INTENT_ANALYSIS:\n"
                "ASSUMPTIONS: OPENAI_API_KEY is not set on this server\n"
                "ALTERNATIVES: Configure OPENAI_API_KEY to enable AI mutation proposals\n"
                "CONFIDENCE: low\n"
                "MISSING_DATA: OPENAI_API_KEY required\n"
                "\n"
                "SECTION_MUTATION_CONTRACT:\n"
                + contract_json
            )

        return _stub

    import httpx

    def _live_call(system_prompt: str) -> str:
        url = f"{_DEFAULT_BASE_URL.rstrip('/')}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": _DEFAULT_MODEL,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": 0,
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    return _live_call


# ---------------------------------------------------------------------------
# POST /api/mutations/propose
# ---------------------------------------------------------------------------


@router.post("/mutations/propose", dependencies=[Depends(require_auth)])
async def propose_mutation(
    body: MutationProposeRequest,
) -> JSONResponse:
    """Submit a user intent to the mutation governance pipeline.

    The request passes through:
      1. MODE_ENGINE_EXECUTION_V2 (strict + prediction + builder modes enforced)
      2. MutationContract JSON parsing
      3. 3-stage validation (structural → logical → scope)
      4. Mutation enforcement gate
      5. Mandatory audit logging
      6. Returns a structured mutation proposal — NEVER executes

    Returns
    -------
    200 OK — ``status="approved"``: structured mutation proposal
    200 OK — ``status="blocked"``:  structured failure with ``blocked_reason``
    """
    ai_call = _build_ai_call()

    result: MutationGovernanceResult = mutation_governance_gateway(
        user_intent=body.intent,
        modes=body.modes,
        ai_call=ai_call,
    )

    return JSONResponse(content=result.to_dict(), status_code=200)
