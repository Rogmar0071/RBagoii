"""
backend.app.bridge_routes
===========================
FastAPI router for MUTATION_BRIDGE_EXECUTION_V1 endpoints.

Endpoints
---------
POST /api/mutations/execute
    Submit a verified governance result and simulation result to the bridge
    execution pipeline.  Returns a structured BridgeResult with execution
    artifacts (branch_name, diff_patch, modified_files_list, build_status,
    execution_summary).

    NEVER executes real git operations.  All mutations are simulated and
    produce human-review-ready artifacts on an isolated branch only.

Integration:
    Caller must first obtain:
      1. An approved MutationGovernanceResult  (POST /api/mutations/propose)
      2. A safe SimulationResult               (POST /api/mutations/simulate)
    Both results are passed here as dicts.

All endpoints require ``Authorization: Bearer <API_KEY>`` when API_KEY is set.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.auth import require_auth
from backend.app.mutation_bridge import (
    BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH,
    BridgeResult,
    bridge_gateway,
)

router = APIRouter(prefix="/api", tags=["mutation_bridge"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class BridgeExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    governance_result: dict[str, Any] = Field(
        ...,
        description=(
            "Full MutationGovernanceResult dict (from POST /api/mutations/propose). "
            "Must have status='approved' and gate_result.passed=True."
        ),
    )
    simulation_result: dict[str, Any] = Field(
        ...,
        description=(
            "Full SimulationResult dict (from POST /api/mutations/simulate). "
            "Must have safe_to_execute=True."
        ),
    )
    override: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Override required when simulation_result.risk_level == 'high'. "
            "Required fields: "
            "explicit_approval (bool, must be true), "
            "justification (str, non-empty), "
            "accepted_risks (list[str], non-empty)."
        ),
    )
    system_context: dict[str, Any] | None = Field(
        default=None,
        description="Optional ambient system context (informational only).",
    )

    @field_validator("governance_result")
    @classmethod
    def _validate_governance_result(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("governance_result must be a JSON object.")
        if "governance_contract" not in value:
            raise ValueError(
                "governance_result must contain 'governance_contract' field. "
                "Provide the full MutationGovernanceResult dict."
            )
        return value

    @field_validator("simulation_result")
    @classmethod
    def _validate_simulation_result(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("simulation_result must be a JSON object.")
        if "governance_contract" not in value:
            raise ValueError(
                "simulation_result must contain 'governance_contract' field. "
                "Provide the full SimulationResult dict."
            )
        return value

    @field_validator("override")
    @classmethod
    def _validate_override(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        explicit_approval = value.get("explicit_approval")
        if explicit_approval is not True:
            raise ValueError(
                "override.explicit_approval must be true."
            )
        justification = value.get("justification", "")
        if (
            not isinstance(justification, str)
            or len(justification.strip()) < BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH
        ):
            raise ValueError(
                f"override.justification must be a non-empty string of at least "
                f"{BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH} characters."
            )
        accepted_risks = value.get("accepted_risks")
        if not isinstance(accepted_risks, list) or not accepted_risks:
            raise ValueError(
                "override.accepted_risks must be a non-empty list of strings."
            )
        if not all(isinstance(r, str) and r.strip() for r in accepted_risks):
            raise ValueError(
                "override.accepted_risks entries must all be non-empty strings."
            )
        return value


# ---------------------------------------------------------------------------
# POST /api/mutations/execute
# ---------------------------------------------------------------------------


@router.post("/mutations/execute", dependencies=[Depends(require_auth)])
async def execute_mutation(
    body: BridgeExecuteRequest,
) -> JSONResponse:
    """Submit verified governance and simulation results to the bridge pipeline.

    The request passes through MUTATION_BRIDGE_EXECUTION_V1:
      1. Verify governance authenticity (structural signature check)
      2. Verify simulation integrity (structural signature + safe_to_execute)
      3. Runtime re-validation (3 checks; HARD BLOCK on any failure)
      4. Execution gate (5 conditions; HARD BLOCK on any failure)
      5. Staged execution (simulated — no real git ops, no file writes)
      6. Artifact enforcement (BLOCK if any artifact missing)
      7. Mandatory audit logging
      8. Returns a structured BridgeResult — NEVER free text, NEVER real execution

    Returns
    -------
    200 OK — ``status="executed"``: all artifacts produced; human-review-ready
    200 OK — ``status="blocked"``: ``blocked_reason`` set; no artifacts
    """
    result: BridgeResult = bridge_gateway(
        governance_result=body.governance_result,
        simulation_result=body.simulation_result,
        override=body.override,
        system_context=body.system_context,
    )

    return JSONResponse(content=result.to_dict(), status_code=200)
