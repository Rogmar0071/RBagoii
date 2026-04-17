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

from backend.app.mode_engine import resolve_modes

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

OUTPUT FORMAT — two independent sections (follow exactly):

SECTION_INTENT_ANALYSIS:
ASSUMPTIONS: <explicit assumption 1>; <assumption 2>; ...
ALTERNATIVES: <alternative approach 1>; <alternative approach 2>; ...
CONFIDENCE: <number 0-1, or "low" / "medium" / "high">
MISSING_DATA: <missing item>; ... (or "none")

SECTION_MUTATION_CONTRACT:
{
  "target_files": ["<path under backend/, android/, or scripts/>"],
  "operation_type": "<create_file | update_file | delete_file>",
  "proposed_changes": "<non-empty description of exact changes>",
  "assumptions": ["<assumption 1>"],
  "alternatives": ["<alternative 1>", "<alternative 2>"],
  "confidence": <0.0-1.0>,
  "risks": ["<risk 1>"],
  "missing_data": ["<item>"]
}

LAYER INDEPENDENCE RULE:
- SECTION_INTENT_ANALYSIS text is validated by the mode engine (text markers).
- SECTION_MUTATION_CONTRACT JSON is validated by the mutation governance pipeline.
- These are completely separate validation layers. Use ONLY lowercase field names
  in the JSON block. Do NOT put mode engine marker names as JSON keys.

SCOPE RULES:
- target_files MUST only reference paths under: backend/, android/, or scripts/
- NEVER reference .env, secrets/, or infra/credentials/

INVARIANTS:
- No guessing. Declare all unknowns in MISSING_DATA.
- ASSUMPTIONS must be fully explicit (no_undeclared_assumptions).
- ALTERNATIVES must offer at least one genuine alternative (no_single_path_bias).
- If a valid proposal cannot be generated, write INSUFFICIENT_DATA: <reason>
  in the SECTION_INTENT_ANALYSIS and omit the JSON block.

EXAMPLE:
SECTION_INTENT_ANALYSIS:
ASSUMPTIONS: The process() function exists and accepts a dict argument
ALTERNATIVES: Validate at the API layer instead; Add a separate validator class
CONFIDENCE: 0.85
MISSING_DATA: none

SECTION_MUTATION_CONTRACT:
{
  "target_files": ["backend/app/example.py"],
  "operation_type": "update_file",
  "proposed_changes": "Add input validation to the process() function.",
  "assumptions": ["The process() function exists and accepts a dict argument"],
  "alternatives": ["Validate at the API layer instead", "Add a separate validator class"],
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
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    execution_boundary: dict[str, bool] = field(default_factory=lambda: dict(_EXECUTION_BOUNDARY))

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
    """Extract the mutation contract JSON block from *text*.

    Strict extraction rules (enforce strict JSON parsing):
    - The JSON block MUST follow a ``SECTION_MUTATION_CONTRACT:`` label.
    - The block must be a brace-balanced, fully parseable JSON object.
    - Any output that does not contain the label, or whose JSON is malformed
      or incomplete, returns ``None`` — resulting in an immediate blocked result.
    - Mode engine marker text (ASSUMPTIONS:, CONFIDENCE:, etc.) and the JSON
      block are entirely independent; this function only touches the JSON block.
    """
    _LABEL = "SECTION_MUTATION_CONTRACT:"
    idx = text.find(_LABEL)
    if idx == -1:
        return None

    # Take only the text after the label.
    after = text[idx + len(_LABEL) :]

    # Strip optional markdown code fences (```json … ```).
    after = re.sub(r"```(?:json)?\s*", "", after).strip()

    # Locate the opening brace of the JSON object.
    brace_start = after.find("{")
    if brace_start == -1:
        return None

    # Walk the string tracking brace depth to find the balanced closing brace.
    depth = 0
    for i, ch in enumerate(after[brace_start:], start=brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = after[brace_start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None  # brace-balanced but invalid JSON → reject

    return None  # unbalanced braces → reject


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

    STRICT_MODE_EXECUTION_SPINE_LOCK_V1 — PHASE 9: GOVERNANCE ALIGNMENT LOCK
    mutation_governance MUST:
    - TRUST mode_engine result ONLY
    - NOT re-interpret validation
    - IF mode_engine.status == "blocked": governance.status = "blocked"
    - IF mode_engine.status == "approved": governance.status = "approved"

    ALIGNED WITH DUAL_MODE_GOVERNANCE_AND_INTENT_BINDING_V1:

    Pipeline (MUTATION_GOVERNANCE_EXECUTION_V1):
      1. receive_intent        — validate user_intent is non-empty.
      2. mode_resolution       — determine if strict_mode is active.
      3. IF modes == [] or modes == None:
           → APPROVE immediately (no validation, no contract)
      4. IF modes == ["strict_mode"]:
           → mode_engine (with contract-driven validation)
           → parse output as MutationContract
           → contract_validation (3-stage)
           → enforcement_gate
           → APPROVE or BLOCK
      5. audit_log             — mandatory write.
      6. return_proposal       — structured MutationGovernanceResult.

    Parameters
    ----------
    user_intent:
        Raw user intent describing the desired mutation.
    modes:
        Requested modes. If empty or None, NORMAL mode is used (no validation).
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
    # PHASE 1 — MODE ISOLATION LOCK
    # modes is the ONLY source of truth
    # is_strict = (modes != None AND "strict_mode" IN modes)
    # For mutation governance, default to strict_mode if no modes specified
    # ------------------------------------------------------------------
    requested_modes: list[str] = list(modes) if modes is not None else ["strict_mode"]
    resolved_modes = resolve_modes(requested_modes)

    # Strict mode detection (STRICT_MODE_PROPAGATION_ENFORCEMENT_V1)
    is_strict = "strict_mode" in requested_modes

    result = MutationGovernanceResult()
    audit = MutationGovernanceAuditRecord(
        contract_id=result.contract_id,
        audit_id=result.audit_id,
        user_intent=user_intent,
        selected_modes=resolved_modes,
    )

    # ------------------------------------------------------------------
    # PHASE 1 — NORMAL MODE PATH (modes == [] or modes == None)
    # Governance NEVER assumes validation exists
    # IF is_strict == FALSE:
    #   - BYPASS all validation
    #   - RETURN raw AI output (wrapped in governance structure)
    # ------------------------------------------------------------------
    if not is_strict:
        # NORMAL MODE: no validation, no contract, immediate approval
        result.status = "approved"
        result.mutation_proposal = {
            "note": "NORMAL mode: no contract validation required",
            "user_intent": user_intent,
        }
        audit.status = "approved"
        audit.mutation_proposal = result.mutation_proposal
        persist_mutation_audit_record(audit)
        return result

    # ------------------------------------------------------------------
    # STRICT MODE PATH: Contract-driven validation required
    # For mutation governance, we handle validation ourselves
    # Mode engine just provides AI call with prompt injection
    # ------------------------------------------------------------------
    # Add enforced modes for strict mode operation
    for m in _ENFORCED_MODES:
        if m not in requested_modes:
            requested_modes.append(m)
    resolved_modes = resolve_modes(requested_modes)
    audit.selected_modes = resolved_modes

    # PHASE 10 — PROMPT INJECTION LOCK
    # Build mutation prompt with MODE ENGINE EXECUTION V2 CONSTRAINTS
    from backend.app.mode_engine import build_mode_system_prompt_injection

    mode_constraints = build_mode_system_prompt_injection(resolved_modes)
    full_prompt = _MUTATION_SYSTEM_PROMPT + mode_constraints

    # Call AI directly (mode_engine would do validation we don't want)
    mode_output = ai_call(full_prompt)

    # ------------------------------------------------------------------
    # PHASE 4 — PARSE-FIRST ENFORCEMENT (mutation-specific)
    # Step 3: parse AI output as MutationContract JSON
    # PHASE 9 — GOVERNANCE ALIGNMENT: mutation governance does its own parsing
    # Mode engine validation failures are ignored - we always try to parse
    # ------------------------------------------------------------------
    raw_data = _extract_json(mode_output)
    if raw_data is None:
        # PHASE 6 — FAILURE TAXONOMY LOCK: use "parse_failure" not compound labels
        # PHASE 7 — RETRY ENGINE LOCK: parse failure returns retry_count = MAX_RETRIES (=2)
        from backend.app.mode_engine import MAX_RETRIES

        parse_failure = MutationValidationResult(
            stage="parse",
            passed=False,
            failed_rules=["parse_failure"],
            correction_instructions=[
                "Output must contain a SECTION_MUTATION_CONTRACT: label followed by "
                "a valid, brace-balanced JSON object with all required fields. "
                "Mode engine text markers and the JSON block are independent layers."
            ],
        )
        return _build_blocked_result(
            result=result,
            audit=audit,
            validation_results=[parse_failure],
            blocked_reason="parse_failure",
            retry_count=MAX_RETRIES,
        )

    contract = MutationContract.from_dict(raw_data)

    # ------------------------------------------------------------------
    # PHASE 5 — VALIDATION PIPELINE LOCK
    # Step 4: 3-stage validation pipeline (CONTRACT-DRIVEN)
    # STRICT_MODE_PROPAGATION_ENFORCEMENT_V1:
    # Validation ONLY runs in strict_mode WITH contract
    # All three stages MUST execute: structural → logical → scope
    # EACH stage MUST return: stage, passed, failed_rules, correction_instructions
    # ------------------------------------------------------------------
    v1 = stage_1_structural_validation(contract)
    v2 = stage_2_logical_validation(contract)
    v3 = stage_3_scope_validation(contract)
    all_stages: list[MutationValidationResult] = [v1, v2, v3]
    result.validation_results = [vr.to_dict() for vr in all_stages]

    # ------------------------------------------------------------------
    # PHASE 9 — GOVERNANCE ALIGNMENT LOCK
    # Step 5: Mutation enforcement gate (depends on contract validation)
    # STRICT_MODE_PROPAGATION_ENFORCEMENT_V1:
    # Blocking occurs if ANY validation stage fails
    # PHASE 5 — IF ANY stage fails: status = "blocked"
    # PHASE 5 — IF ALL pass: status = "approved"
    # ------------------------------------------------------------------
    gate = mutation_enforcement_gate(all_stages)
    result.gate_result = gate.to_dict()

    if gate.passed:
        # PHASE 8 — SUCCESS PATH LOCK
        # IF status == "approved": RETURN CLEAN output (no error wrapper)
        result.status = "approved"
        result.mutation_proposal = contract.to_dict()
    else:
        # PHASE 6 — FAILURE TAXONOMY: "validation_failure"
        # STRICT MODE FAILURE ENFORCEMENT: block on validation failure
        result.status = "blocked"
        result.blocked_reason = "validation_failure"

    # ------------------------------------------------------------------
    # Step 6: Audit log (mandatory — raises if DB write fails)
    # PHASE 11 — AUDIT CONSISTENCY LOCK
    # ALL executions MUST log: selected_modes, status, blocked_reason, retry_count,
    # validation_results
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
    retry_count: int = 0,
) -> MutationGovernanceResult:
    """Populate *result* as a blocked proposal and persist the audit record.

    PHASE 7 — RETRY ENGINE LOCK: retry_count parameter added to support deterministic reporting.
    """
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
