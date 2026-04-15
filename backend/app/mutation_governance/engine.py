"""
backend.app.mutation_governance.engine
========================================
MUTATION_GOVERNANCE_EXECUTION_V1 — main governance gateway.

System pipeline:
  receive_intent
    → mode_engine           (MODE_ENGINE_EXECUTION_V2 — enforces structure)
    → ai_generates_mutation_proposal  (output parsed as MutationContract JSON)
    → mutation_contract_validation    (3-stage: structural → logical → scope)
    → mutation_enforcement_gate       (block if any stage fails)
    → audit_log                       (mandatory, blocking on DB failure)
    → return_proposal                 (structured object ONLY — no execution)

Governance invariants enforced:
  - ai_is_proposal_only          No mutation is executed; only proposals returned.
  - mutation_requires_validation All proposals pass the full 3-stage pipeline.
  - no_execution_in_phase_2      Execution boundary constants declared below.
  - all_outputs_structured       Free-text responses are rejected.
  - audit_is_mandatory           block_if_log_not_written.

Execution boundary (phase 2):
  - no_git_commit
  - no_file_write
  - no_deployment_trigger
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from backend.app.mode_engine import mode_engine_gateway, resolve_modes

from .audit import persist_mutation_audit_record
from .contract import (
    MutationContract,
    MutationGovernanceAuditRecord,
    MutationValidationResult,
)
from .gate import GateResult, mutation_enforcement_gate
from .validation import (
    stage_1_structural_validation,
    stage_2_logical_validation,
    stage_3_scope_validation,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution boundary constants (phase 2 — proposal only)
# ---------------------------------------------------------------------------

_EXECUTION_BOUNDARY: dict[str, bool] = {
    "no_git_commit": True,
    "no_file_write": True,
    "no_deployment_trigger": True,
}

# ---------------------------------------------------------------------------
# Mode engine alignment — enforced modes
# ---------------------------------------------------------------------------

_ENFORCED_MODES: list[str] = ["strict_mode", "prediction_mode", "builder_mode"]

# ---------------------------------------------------------------------------
# Mutation system prompt
# ---------------------------------------------------------------------------

_MUTATION_SYSTEM_PROMPT = """\
You are MUTATION_GOVERNANCE_EXECUTION_V1.

ROLE: Generate structured mutation proposals ONLY. You NEVER execute code,
write files, commit to version control, or trigger deployments.

OUTPUT FORMAT (MANDATORY — follow exactly):

ASSUMPTIONS: <comma-separated list of explicit assumptions>
ALTERNATIVES: <comma-separated list of alternative approaches>
CONFIDENCE: <number 0–1, or "low" / "medium" / "high">
MISSING_DATA: <comma-separated list, or "none">
SECTION_MUTATION_CONTRACT: mutation_proposal

{
  "target_files": ["<path under backend/, android/, or scripts/>"],
  "operation_type": "<create_file | update_file | delete_file>",
  "proposed_changes": "<non-empty description of the exact changes>",
  "assumptions": ["<assumption 1>"],
  "alternatives": ["<alternative 1>", "<alternative 2>"],
  "confidence": <0–1 or "low"/"medium"/"high">,
  "risks": ["<risk 1>"],
  "missing_data": ["<item>"]
}

SCOPE RULES:
- target_files MUST only reference paths under: backend/, android/, or scripts/
- NEVER reference .env, secrets/, or infra/credentials/

INVARIANTS:
- No guessing. If information is missing, declare it in MISSING_DATA.
- ASSUMPTIONS must list every assumption explicitly (no_undeclared_assumptions).
- ALTERNATIVES must offer at least one genuine alternative (no_single_path_bias).
- If you cannot produce a valid proposal, output INSUFFICIENT_DATA: <reason>.

EXAMPLE:
ASSUMPTIONS: The process() function exists and accepts a dict argument
ALTERNATIVES: Validate at the API layer, Add a separate validator class
CONFIDENCE: 0.85
MISSING_DATA: none
SECTION_MUTATION_CONTRACT: mutation_proposal

{
  "target_files": ["backend/app/example.py"],
  "operation_type": "update_file",
  "proposed_changes": "Add input validation to the process() function.",
  "assumptions": ["The process() function exists and accepts a dict argument"],
  "alternatives": ["Validate at the API layer", "Add a separate validator class"],
  "confidence": 0.85,
  "risks": ["Existing callers may fail with new validation"],
  "missing_data": ["none"]
}"""

# ---------------------------------------------------------------------------
# Structured governance result
# ---------------------------------------------------------------------------


@dataclass
class MutationGovernanceResult:
    """Structured output of the mutation governance pipeline.

    Always a proposal — NEVER an execution instruction.
    ``status`` is either ``"approved"`` or ``"blocked"``.
    """

    contract_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    governance_contract: str = "MUTATION_GOVERNANCE_EXECUTION_V1"
    status: str = "pending"
    mutation_proposal: dict[str, Any] | None = None
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    gate_result: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    execution_boundary: dict[str, bool] = field(
        default_factory=lambda: dict(_EXECUTION_BOUNDARY)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "governance_contract": self.governance_contract,
            "status": self.status,
            "mutation_proposal": self.mutation_proposal,
            "validation_results": self.validation_results,
            "gate_result": self.gate_result,
            "blocked_reason": self.blocked_reason,
            "audit_id": self.audit_id,
            "created_at": self.created_at,
            "execution_boundary": self.execution_boundary,
        }


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from *text*.

    Handles:
    - Pure JSON output.
    - JSON wrapped in ```json … ``` fences.
    - JSON embedded after a ``SECTION_MUTATION_CONTRACT:`` label.
    """
    # Strip markdown code fences.
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()

    # Attempt direct parse of the full cleaned text first.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Extract the outermost JSON object by brace matching.
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = cleaned[first : last + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Governance gateway
# ---------------------------------------------------------------------------


def mutation_governance_gateway(
    *,
    user_intent: str,
    modes: list[str] | None = None,
    ai_call: Callable[[str], str],
) -> MutationGovernanceResult:
    """Single mandatory entry/exit point for the mutation governance pipeline.

    Pipeline (MUTATION_GOVERNANCE_EXECUTION_V1):
      1. receive_intent        — validate user_intent is non-empty.
      2. mode_engine           — route through MODE_ENGINE_EXECUTION_V2 with
                                 enforced modes (strict + prediction + builder).
      3. ai_generates_proposal — parse mode engine output as MutationContract.
      4. contract_validation   — 3-stage pipeline (structural → logical → scope).
      5. enforcement_gate      — block if any stage failed.
      6. audit_log             — mandatory write (raises RuntimeError on failure).
      7. return_proposal       — structured MutationGovernanceResult only.

    Parameters
    ----------
    user_intent:
        Raw user intent describing the desired mutation.
    modes:
        Requested modes.  Enforced modes are always added; unknown modes
        are filtered by the mode engine.
    ai_call:
        ``(system_prompt: str) -> str`` callable that invokes the AI.
        Passed through to ``mode_engine_gateway``.

    Returns
    -------
    MutationGovernanceResult
        Always structured.  ``status="approved"`` or ``status="blocked"``.
        Never raises on validation failure.

    Raises
    ------
    RuntimeError
        If the database is configured and the audit write fails
        (``block_if_log_not_written``).
    """
    # ------------------------------------------------------------------
    # Enforce required modes (mode_engine_alignment)
    # ------------------------------------------------------------------
    requested_modes: list[str] = list(modes or [])
    for m in _ENFORCED_MODES:
        if m not in requested_modes:
            requested_modes.append(m)
    resolved_modes = resolve_modes(requested_modes)

    result = MutationGovernanceResult()
    audit = MutationGovernanceAuditRecord(
        contract_id=result.contract_id,
        audit_id=result.audit_id,
        user_intent=user_intent,
        selected_modes=resolved_modes,
    )

    # ------------------------------------------------------------------
    # Step 2: mode_engine_gateway
    # Enforces structured output, validates mode markers, retries on failure.
    # ------------------------------------------------------------------
    mode_output, _mode_audit = mode_engine_gateway(
        user_intent=user_intent,
        modes=resolved_modes,
        ai_call=ai_call,
        base_system_prompt=_MUTATION_SYSTEM_PROMPT,
    )

    # ------------------------------------------------------------------
    # Step 3: parse AI output as MutationContract JSON
    # ------------------------------------------------------------------
    raw_data = _extract_json(mode_output)
    if raw_data is None:
        parse_failure = MutationValidationResult(
            stage="structural",
            passed=False,
            failed_rules=["parse_failure:no_json_object_in_output"],
            correction_instructions=[
                "AI output did not contain a valid JSON mutation contract object"
            ],
        )
        return _build_blocked_result(
            result=result,
            audit=audit,
            validation_results=[parse_failure],
            blocked_reason="parse_failure:output_not_parseable_as_mutation_contract",
        )

    contract = MutationContract.from_dict(raw_data)

    # ------------------------------------------------------------------
    # Step 4: 3-stage validation pipeline
    # ------------------------------------------------------------------
    v1 = stage_1_structural_validation(contract)
    v2 = stage_2_logical_validation(contract)
    v3 = stage_3_scope_validation(contract)
    all_stages: list[MutationValidationResult] = [v1, v2, v3]
    result.validation_results = [vr.to_dict() for vr in all_stages]

    # ------------------------------------------------------------------
    # Step 5: Mutation enforcement gate
    # ------------------------------------------------------------------
    gate = mutation_enforcement_gate(all_stages)
    result.gate_result = gate.to_dict()

    if gate.passed:
        result.status = "approved"
        result.mutation_proposal = contract.to_dict()
    else:
        result.status = "blocked"
        result.blocked_reason = gate.blocked_reason

    # ------------------------------------------------------------------
    # Step 6: Audit log (mandatory — raises if DB write fails)
    # ------------------------------------------------------------------
    audit.mutation_proposal = result.mutation_proposal or {}
    audit.validation_results = result.validation_results
    audit.blocked_reason = result.blocked_reason
    audit.status = result.status
    persist_mutation_audit_record(audit)

    # ------------------------------------------------------------------
    # Step 7: Return structured proposal (no execution)
    # ------------------------------------------------------------------
    return result


def _build_blocked_result(
    *,
    result: MutationGovernanceResult,
    audit: MutationGovernanceAuditRecord,
    validation_results: list[MutationValidationResult],
    blocked_reason: str,
) -> MutationGovernanceResult:
    """Populate *result* as a blocked proposal and persist the audit record."""
    result.validation_results = [vr.to_dict() for vr in validation_results]
    gate = GateResult(
        passed=False,
        blocked_reason=blocked_reason,
        failed_stages=[vr.stage for vr in validation_results if not vr.passed],
    )
    result.gate_result = gate.to_dict()
    result.status = "blocked"
    result.blocked_reason = blocked_reason

    audit.validation_results = result.validation_results
    audit.blocked_reason = result.blocked_reason
    audit.status = "blocked"
    persist_mutation_audit_record(audit)
    return result
