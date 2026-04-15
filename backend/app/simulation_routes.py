"""
backend.app.simulation_routes
==============================
FastAPI router for MUTATION_SIMULATION_EXECUTION_V1 endpoints.

Endpoints
---------
POST /api/mutations/simulate
    Submit a validated mutation governance result to the simulation pipeline.
    Returns a structured simulation result (SIMULATION ONLY — no execution).

Integration:
    This router sits AFTER mutation_governance.  The caller must first obtain
    an ``approved`` MutationGovernanceResult and pass the full dict here.

All endpoints require ``Authorization: Bearer <API_KEY>`` when API_KEY is set.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.auth import require_auth
from backend.app.mutation_simulation import SimulationResult, simulation_gateway

router = APIRouter(prefix="/api", tags=["mutation_simulation"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    governance_result: dict[str, Any] = Field(
        ...,
        description=(
            "Full MutationGovernanceResult dict (from POST /api/mutations/propose). "
            "Must have status='approved'."
        ),
    )
    override: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional override for high-risk simulations. "
            "Required fields: justification (str), accepted_risks (list[str])."
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
        if "status" not in value:
            raise ValueError(
                "governance_result must contain 'status' field. "
                "Provide the full MutationGovernanceResult dict."
            )
        return value

    @field_validator("override")
    @classmethod
    def _validate_override(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        from backend.app.mutation_simulation.contract import OVERRIDE_MIN_JUSTIFICATION_LENGTH

        justification = value.get("justification", "")
        if (
            not isinstance(justification, str)
            or len(justification.strip()) < OVERRIDE_MIN_JUSTIFICATION_LENGTH
        ):
            raise ValueError(
                f"override.justification must be a non-empty string of at least "
                f"{OVERRIDE_MIN_JUSTIFICATION_LENGTH} characters."
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
# POST /api/mutations/simulate
# ---------------------------------------------------------------------------


@router.post("/mutations/simulate", dependencies=[Depends(require_auth)])
async def simulate_mutation(
    body: SimulationRequest,
) -> JSONResponse:
    """Submit an approved mutation governance result to the simulation pipeline.

    The request passes through MUTATION_SIMULATION_EXECUTION_V1:
      1. Validate governance_result.status == "approved"
      2. Dependency surface mapping
      3. Impact analysis
      4. Failure prediction
      5. Risk scoring
      6. Simulation decision gate (blocking rules enforced)
      7. Mandatory audit logging
      8. Returns a structured simulation result — NEVER executes

    Returns
    -------
    200 OK — ``safe_to_execute=true``:  simulation passed, proposal safe to proceed
    200 OK — ``safe_to_execute=false``: simulation blocked, ``blocked_reason`` set
    """
    result: SimulationResult = simulation_gateway(
        governance_result=body.governance_result,
        override=body.override,
        system_context=body.system_context,
    )

    return JSONResponse(content=result.to_dict(), status_code=200)
