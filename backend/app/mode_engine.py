"""
backend.app.mode_engine
========================
MODE_ENGINE_EXECUTION_V2 — Enforces deterministic AI reasoning through
mode-driven constraints, post-generation validation, retry control, and
mandatory audit logging.

Contract ID   : MODE_ENGINE_EXECUTION_V2
Class         : STRUCTURAL
Status        : LOCKED
Reversibility : REVERSIBLE
Depends on    : MODE_ENGINE_ENFORCEMENT_PATCH_V1

Strict-mode responses are validated before exit; non-strict responses pass through unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from backend.app.contract_construction import ContractObject, construct_contract
from backend.app.intent_extraction import extract_intent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

MODE_STRICT = "strict_mode"
MODE_PREDICTION = "prediction_mode"
MODE_DEBUG = "debug_mode"
MODE_BUILDER = "builder_mode"
MODE_AUDIT = "audit_mode"

SUPPORTED_MODES = frozenset({MODE_STRICT, MODE_PREDICTION, MODE_DEBUG, MODE_BUILDER, MODE_AUDIT})

# Lower index = higher priority (strict_mode has highest priority).
MODE_PRIORITY_ORDER = [MODE_STRICT, MODE_PREDICTION, MODE_DEBUG, MODE_AUDIT, MODE_BUILDER]

_MODE_PRIORITY: dict[str, int] = {m: i for i, m in enumerate(MODE_PRIORITY_ORDER)}

# Maximum number of validation retries before structured failure is returned.
MAX_RETRIES = 2
STRICT_MODE_ARTIFACT_PREFIX = "ARTIFACT_"
STRICT_MODE_INSUFFICIENT_DATA_PREFIX = "INSUFFICIENT_DATA:"
STRICT_MODE_INSUFFICIENT_GROUNDED_KNOWLEDGE = "INSUFFICIENT GROUNDED KNOWLEDGE"
STRICT_MODE_MIN_CONFIDENCE = 0.6
CERTAINTY_LANGUAGE_MIN_CONFIDENCE = 0.95


def resolve_modes(requested: list[str]) -> list[str]:
    """Filter modes without inserting, reordering, or deduplicating entries."""
    return [mode for mode in requested if mode in SUPPORTED_MODES]


def effective_mode(modes: list[str]) -> str | None:
    """Return the highest-priority active mode from *modes*, or ``None``."""
    resolved = resolve_modes(modes)
    return resolved[0] if resolved else None


# ---------------------------------------------------------------------------
# Mode stacking conflict resolution
# ---------------------------------------------------------------------------

# Known conflict pairs and their authoritative resolution descriptions.
# Contract rules:
#   - higher_priority_overrides_lower
#   - conflicts_resolve_to_stricter_behavior
_MODE_CONFLICT_RULES: dict[frozenset, dict[str, list[str]]] = {
    frozenset({MODE_STRICT, MODE_PREDICTION}): {
        "description": "strict_vs_prediction",
        "resolution": [
            "assumptions_allowed_only_if_flagged",
            "insufficient_data_must_be_returned_if_required",
        ],
    },
}


def apply_mode_conflict_resolution(modes: list[str]) -> list[str]:
    """Apply mode-stacking conflict resolution rules.

    Contract rules enforced here:
    - ``higher_priority_overrides_lower``: strict_mode's constraints are applied
      before and override conflicting lower-priority mode constraints.
    - ``conflicts_resolve_to_stricter_behavior``: when two modes conflict the
      stricter rule wins.  Both modes remain active; the higher-priority mode's
      constraints additionally restrict the lower-priority mode's output.

    All detected conflicts are logged at DEBUG level.  The mode list is returned
    unchanged — modes are never removed; conflict enforcement happens at
    validation time (all active modes' rules run simultaneously).
    """
    mode_set = frozenset(modes)
    for conflict_pair, rule in _MODE_CONFLICT_RULES.items():
        if conflict_pair.issubset(mode_set):
            logger.debug(
                "mode_engine: conflict resolved — %s: %s",
                rule["description"],
                ", ".join(rule["resolution"]),
            )
    return modes


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


@dataclass
class ModeEngineAuditRecord:
    """Full audit trail for a single mode-engine-gated AI interaction."""

    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_intent: str = ""
    selected_modes: list[str] = field(default_factory=list)
    transformed_prompt: str = ""
    raw_ai_output: str = ""
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    retry_count: int = 0
    final_output: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a single validation stage."""

    passed: bool
    stage: str = ""
    failed_rules: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    correction_instructions: list[str] = field(default_factory=list)
    contract_reference: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "stage": self.stage,
            "passed": self.passed,
            "failed_rules": self.failed_rules,
            "missing_fields": self.missing_fields,
            "correction_instructions": self.correction_instructions,
        }
        if self.contract_reference:
            result["contract_reference"] = self.contract_reference
        return result


# ---------------------------------------------------------------------------
# Stage 0: Pre-generation constraints
# ---------------------------------------------------------------------------


def stage_0_pre_generation_constraints(
    user_intent: str,
    modes: list[str],  # noqa: ARG001 — reserved for future context checks
) -> tuple[bool, str]:
    """Enforce pre-generation constraints.

    Returns
    -------
    (ok, reason)
        *ok* is ``False`` when generation must be blocked; *reason* explains why.
    """
    if MODE_STRICT not in modes:
        return True, ""
    if not user_intent or not user_intent.strip():
        return False, "missing_required_input: query must not be empty"
    return True, ""


def build_mode_system_prompt_injection(modes: list[str]) -> str:
    """Return mode-specific constraint text to append to the system prompt.

    Includes conflict-resolution constraints when conflicting mode combinations
    are active (e.g. strict_mode + prediction_mode).
    """
    lines = ["\n\n--- MODE ENGINE EXECUTION V2 CONSTRAINTS ---"]
    lines.append(f"Active modes: {', '.join(modes)}")

    if MODE_STRICT in modes:
        lines.append(
            "STRICT MODE CONTAINMENT:\n"
            "1) Output MUST be a JSON object only (no markdown, no prose wrappers).\n"
            "2) JSON must include: claims (array), uncertainties (array), "
            "generation_mode, mode_label.\n"
            "3) Each claim object must include: statement, confidence (0..1), "
            "source_type, verifiability.\n"
            "4) mode_label must be one of RETRIEVED, INFERRED, GENERATED.\n"
            "5) If the contract cannot be met, respond with exactly: "
            f"{STRICT_MODE_INSUFFICIENT_GROUNDED_KNOWLEDGE}\n"
            f"(legacy acceptance: {STRICT_MODE_INSUFFICIENT_DATA_PREFIX} <reason>)\n"
            "6) Never present unverified output as reality."
        )

    if MODE_PREDICTION in modes:
        lines.append(
            "PREDICTION MODE: Your response MUST contain all four of these labeled "
            "sections on their own lines: "
            "ASSUMPTIONS: <list>, ALTERNATIVES: <list>, "
            "CONFIDENCE: <number 0-1 or low/medium/high>, "
            "MISSING_DATA: <list or none>."
        )

    if MODE_DEBUG in modes:
        lines.append(
            "DEBUG MODE: Your response MUST include stepwise reasoning. "
            "Label each step as STEP_1:, STEP_2:, etc."
        )

    if MODE_AUDIT in modes:
        lines.append(
            "AUDIT MODE: Your response MUST include a RISK_IDENTIFICATION: <risks or none> section."
        )

    if MODE_BUILDER in modes:
        lines.append(
            "BUILDER MODE: Your response MUST be organized into named sections "
            "using SECTION_<NAME>: prefixes (e.g. SECTION_OVERVIEW:)."
        )

    # Conflict resolution: strict + prediction → additional constraints
    # (assumptions_allowed_only_if_flagged,
    #  insufficient_data_must_be_returned_if_required)
    if MODE_STRICT in modes and MODE_PREDICTION in modes:
        lines.append(
            "STRICT+PREDICTION CONFLICT RESOLUTION "
            "(assumptions_allowed_only_if_flagged): "
            "All assumptions MUST be explicitly declared in the ASSUMPTIONS: section. "
            "Implicit or in-line assumptions are prohibited. "
            "If required data is unavailable you MUST state INSUFFICIENT_DATA: <reason> "
            "instead of guessing within any section."
        )

    lines.append("--- END MODE ENGINE CONSTRAINTS ---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 1: Structural validation
# ---------------------------------------------------------------------------

# Markers that must appear in the AI output for each mode.
_REQUIRED_MARKERS: dict[str, list[str]] = {
    MODE_PREDICTION: ["ASSUMPTIONS:", "ALTERNATIVES:", "CONFIDENCE:", "MISSING_DATA:"],
    MODE_DEBUG: ["STEP_"],
    MODE_AUDIT: ["RISK_IDENTIFICATION:"],
    MODE_BUILDER: ["SECTION_"],
}

_STRICT_ARTIFACT_LINE = re.compile(r"^(ARTIFACT_[A-Z0-9_]+):\s*(.*)$")


def _strict_mode_uses_insufficient_data(ai_output: str) -> bool:
    stripped = ai_output.strip()
    return stripped.startswith(STRICT_MODE_INSUFFICIENT_DATA_PREFIX) or (
        stripped == STRICT_MODE_INSUFFICIENT_GROUNDED_KNOWLEDGE
    )


def _strict_mode_artifact_lines(ai_output: str) -> list[str]:
    return [
        line.strip()
        for line in ai_output.splitlines()
        if line.strip().startswith(STRICT_MODE_ARTIFACT_PREFIX)
    ]


def _parse_typed_output(ai_output: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(ai_output)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _required_top_level_fields_missing(
    parsed: dict[str, Any], contract: ContractObject
) -> list[str]:
    missing: list[str] = []
    for section in contract.required_sections:
        if section not in parsed:
            missing.append(section)
    return missing


def _confidence_threshold(contract: ContractObject) -> float:
    if contract.truth_requirement == "strict":
        return 0.8
    if contract.domain_risk == "high":
        return 0.75
    return STRICT_MODE_MIN_CONFIDENCE


def _allowed_mode_labels() -> set[str]:
    return {"RETRIEVED", "INFERRED", "GENERATED"}


def stage_1_structural_validation(
    ai_output: str, modes: list[str], contract: ContractObject | None = None
) -> ValidationResult:
    """Validate that required fields are present in *ai_output*.

    CONTRACT-DRIVEN VALIDATION (DUAL_MODE_GOVERNANCE_AND_INTENT_BINDING_V1):
    - When modes == []: skip validation completely
    - When modes == ["strict_mode"] and contract exists: validate against contract
    - When modes == ["strict_mode"] without contract: block (invalid state)
    """
    # NORMAL MODE: skip all validation
    if MODE_STRICT not in modes:
        return ValidationResult(stage="structural", passed=True)

    # STRICT MODE WITHOUT CONTRACT: invalid state, block
    if contract is None:
        return ValidationResult(
            stage="structural",
            passed=False,
            failed_rules=["strict_mode_without_contract"],
            correction_instructions=[
                "strict_mode requires a contract to be generated for this request"
            ],
        )

    # Special handling: allow strict fallback sentinel as valid response
    uses_insufficient_data = _strict_mode_uses_insufficient_data(ai_output)
    if uses_insufficient_data:
        return ValidationResult(
            stage="structural",
            passed=True,
            contract_reference=contract.to_dict(),
        )

    # Backward-compatible path for legacy contracts used by existing tests.
    if contract.output_format != "typed_structured_json":
        missing: list[str] = []
        failed: list[str] = []
        corrections: list[str] = []
        for section in contract.required_sections:
            if section not in ai_output:
                missing.append(section)
                failed.append(f"missing_required_section:{section}")
                corrections.append(
                    f"Contract requires section '{section}' to be present in response"
                )
        if not ai_output.strip():
            failed.append("strict_mode:empty_output")
            missing.append("non_empty_output")
            corrections.append("Provide a non-empty response")
        return ValidationResult(
            stage="structural",
            passed=len(failed) == 0,
            failed_rules=failed,
            missing_fields=missing,
            correction_instructions=corrections,
            contract_reference=contract.to_dict(),
        )

    failed: list[str] = []
    missing: list[str] = []
    corrections: list[str] = []

    parsed = _parse_typed_output(ai_output)
    if parsed is None:
        failed.append("typed_output_required")
        corrections.append(
            "Respond with JSON object only, or use "
            "INSUFFICIENT GROUNDED KNOWLEDGE when contract cannot be met"
        )
        return ValidationResult(
            stage="structural",
            passed=False,
            failed_rules=failed,
            missing_fields=missing,
            correction_instructions=corrections,
            contract_reference=contract.to_dict(),
        )

    missing = _required_top_level_fields_missing(parsed, contract)
    for section in missing:
        failed.append(f"missing_required_section:{section}")

    claims = parsed.get("claims")
    if not isinstance(claims, list):
        failed.append("claims_not_array")
        corrections.append("claims must be an array")
    elif len(claims) == 0:
        failed.append("claims_empty")
        corrections.append(
            "claims must contain at least one claim, or return "
            "INSUFFICIENT GROUNDED KNOWLEDGE"
        )
    else:
        for i, claim in enumerate(claims):
            if not isinstance(claim, dict):
                failed.append(f"claim_not_object:{i}")
                continue
            for key in ("statement", "confidence", "source_type", "verifiability"):
                if key not in claim:
                    failed.append(f"missing_claim_field:{i}:{key}")

    if "mode_label" in parsed and parsed["mode_label"] not in _allowed_mode_labels():
        failed.append("invalid_mode_label")
        corrections.append("mode_label must be RETRIEVED, INFERRED, or GENERATED")

    return ValidationResult(
        stage="structural",
        passed=len(failed) == 0,
        failed_rules=failed,
        missing_fields=missing,
        correction_instructions=corrections,
        contract_reference=contract.to_dict(),
    )


# ---------------------------------------------------------------------------
# Stage 2: Logical validation
# ---------------------------------------------------------------------------


def stage_2_logical_validation(
    ai_output: str, modes: list[str], contract: ContractObject | None = None
) -> ValidationResult:
    """Validate logical consistency of *ai_output*.

    CONTRACT-DRIVEN VALIDATION:
    - When modes == []: skip validation
    - When modes == ["strict_mode"] with contract: validate required elements
    """
    # NORMAL MODE: skip all validation
    if MODE_STRICT not in modes:
        return ValidationResult(stage="logical", passed=True)

    # STRICT MODE WITHOUT CONTRACT: invalid state
    if contract is None:
        return ValidationResult(
            stage="logical",
            passed=False,
            failed_rules=["strict_mode_without_contract"],
            correction_instructions=["strict_mode requires a contract"],
        )

    # Allow INSUFFICIENT_DATA to bypass logical validation
    uses_insufficient_data = _strict_mode_uses_insufficient_data(ai_output)
    if uses_insufficient_data:
        return ValidationResult(
            stage="logical",
            passed=True,
            contract_reference=contract.to_dict(),
        )

    # Backward-compatible path for legacy contracts used by existing tests.
    if contract.output_format != "typed_structured_json":
        failed: list[str] = []
        corrections: list[str] = []
        for rule in contract.validation_rules:
            if rule == "assumptions_explicit" and "ASSUMPTIONS:" in ai_output:
                assumptions_text = ai_output.split("ASSUMPTIONS:", 1)[1].split("\n")[0].strip()
                if not assumptions_text:
                    failed.append("undeclared_assumptions")
                    corrections.append(
                        "Contract requires ASSUMPTIONS: section to contain explicit content"
                    )
            elif rule == "confidence_valid" and "CONFIDENCE:" in ai_output:
                conf_text = ai_output.split("CONFIDENCE:", 1)[1].split("\n")[0].strip().lower()
                is_numeric = False
                try:
                    val = float(conf_text)
                    is_numeric = 0.0 <= val <= 1.0
                except ValueError:
                    pass
                if not is_numeric and conf_text not in {"low", "medium", "high"}:
                    failed.append("invalid_confidence")
                    corrections.append("Contract requires CONFIDENCE: to be 0-1 or low/medium/high")
            elif rule == "alternatives_present" and "ALTERNATIVES:" in ai_output:
                alt_section = ai_output.split("ALTERNATIVES:", 1)[1].strip()
                if not alt_section:
                    failed.append("alternatives_not_distinct")
                    corrections.append("Contract requires ALTERNATIVES: to list alternatives")
        return ValidationResult(
            stage="logical",
            passed=len(failed) == 0,
            failed_rules=failed,
            correction_instructions=corrections,
            contract_reference=contract.to_dict(),
        )

    parsed = _parse_typed_output(ai_output)
    if parsed is None:
        return ValidationResult(
            stage="logical",
            passed=False,
            failed_rules=["typed_output_required"],
            correction_instructions=["Respond with typed JSON or INSUFFICIENT GROUNDED KNOWLEDGE"],
            contract_reference=contract.to_dict(),
        )

    failed: list[str] = []
    corrections: list[str] = []
    claims = parsed.get("claims", [])
    threshold = _confidence_threshold(contract)

    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        conf = claim.get("confidence")
        if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            failed.append(f"invalid_confidence:{i}")
            corrections.append("Each claim confidence must be a number between 0 and 1")
            continue
        if float(conf) < threshold:
            failed.append(f"confidence_below_threshold:{i}")
            corrections.append(
                "Claims in this contract require confidence >= "
                f"{threshold:.2f}, otherwise use INSUFFICIENT GROUNDED KNOWLEDGE"
            )

        if contract.output_class == "fact" and claim.get(
            "verifiability"
        ) != "externally_verifiable":
            failed.append(f"fact_without_verifiability:{i}")
            corrections.append(
                "Fact-class output requires claim "
                "verifiability=externally_verifiable"
            )

    generation_mode = str(parsed.get("generation_mode", "")).lower()
    mode_label = parsed.get("mode_label")
    if generation_mode == "retrieved" and mode_label != "RETRIEVED":
        failed.append("mode_mismatch:retrieved")
    if generation_mode == "inferred" and mode_label != "INFERRED":
        failed.append("mode_mismatch:inferred")
    if generation_mode == "generated" and mode_label != "GENERATED":
        failed.append("mode_mismatch:generated")

    if contract.intent_type == "decide" and generation_mode == "generated":
        failed.append("intent_contract_mismatch:decide_vs_generated")
        corrections.append("Decide intent cannot be delivered as purely GENERATED output")

    return ValidationResult(
        stage="logical",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
        contract_reference=contract.to_dict(),
    )


# ---------------------------------------------------------------------------
# Stage 3: Compliance validation
# ---------------------------------------------------------------------------

_GUESSING_INDICATORS = frozenset(
    {
        "i think",
        "i believe",
        "probably",
        "i assume",
        "i guess",
        "maybe",
        "i'm not sure but",
        "i would guess",
    }
)
# Tokens used to detect simulated certainty in claim statements.
_CERTAINTY_TOKENS = frozenset({"definitely", "certainly", "undeniably", "guaranteed", "proven"})


def stage_3_compliance_validation(
    ai_output: str, modes: list[str], contract: ContractObject | None = None
) -> ValidationResult:
    """Validate mode-specific compliance rules on *ai_output*.

    CONTRACT-DRIVEN VALIDATION:
    - When modes == []: skip validation
    - When modes == ["strict_mode"] with contract: validate compliance per contract
    """
    # NORMAL MODE: skip all validation
    if MODE_STRICT not in modes:
        return ValidationResult(stage="compliance", passed=True)

    # STRICT MODE WITHOUT CONTRACT: invalid state
    if contract is None:
        return ValidationResult(
            stage="compliance",
            passed=False,
            failed_rules=["strict_mode_without_contract"],
            correction_instructions=["strict_mode requires a contract"],
        )

    # Allow INSUFFICIENT_DATA to bypass compliance validation
    uses_insufficient_data = _strict_mode_uses_insufficient_data(ai_output)
    if uses_insufficient_data:
        return ValidationResult(
            stage="compliance",
            passed=True,
            contract_reference=contract.to_dict(),
        )

    # Backward-compatible path for legacy contracts used by existing tests.
    if contract.output_format != "typed_structured_json":
        failed: list[str] = []
        corrections: list[str] = []
        lower = ai_output.lower()
        guesses = [ind for ind in _GUESSING_INDICATORS if ind in lower]
        if guesses:
            failed.append("strict_mode:guessing_detected")
            corrections.append("Contract prohibits guessing in strict mode")
        return ValidationResult(
            stage="compliance",
            passed=len(failed) == 0,
            failed_rules=failed,
            correction_instructions=corrections,
            contract_reference=contract.to_dict(),
        )

    failed: list[str] = []
    corrections: list[str] = []

    parsed = _parse_typed_output(ai_output)
    if parsed is None:
        return ValidationResult(
            stage="compliance",
            passed=False,
            failed_rules=["typed_output_required"],
            correction_instructions=["Respond with typed JSON or INSUFFICIENT GROUNDED KNOWLEDGE"],
            contract_reference=contract.to_dict(),
        )

    lower = ai_output.lower()
    guesses = [ind for ind in _GUESSING_INDICATORS if ind in lower]
    if guesses:
        failed.append("strict_mode:guessing_detected")
        corrections.append(
            "Strict mode prohibits guessing. Use INSUFFICIENT GROUNDED KNOWLEDGE when uncertain."
        )

    claims = parsed.get("claims", [])
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        statement = str(claim.get("statement", "")).lower()
        conf = claim.get("confidence")
        if any(tok in statement for tok in _CERTAINTY_TOKENS):
            if not isinstance(conf, (int, float)) or float(conf) < (
                CERTAINTY_LANGUAGE_MIN_CONFIDENCE
            ):
                failed.append(f"simulated_certainty:{i}")
                corrections.append(
                    "Do not use certainty language unless confidence is "
                    "near-certain and verifiable."
                )

    return ValidationResult(
        stage="compliance",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
        contract_reference=contract.to_dict(),
    )


# ---------------------------------------------------------------------------
# Response contract: no_free_text_for_structured_modes / partial_responses_rejected
# ---------------------------------------------------------------------------

# Modes that require structured output (free-text responses are rejected).
_STRUCTURED_MODES = frozenset({MODE_PREDICTION, MODE_DEBUG, MODE_AUDIT, MODE_BUILDER})


def _check_response_contract(
    ai_output: str, modes: list[str], contract: ContractObject | None = None
) -> ValidationResult:
    """Enforce response_contract invariants based on contract.

    CONTRACT-DRIVEN VALIDATION:
    - When modes == []: skip validation
    - When modes == ["strict_mode"] with contract: check output_format compliance
    """
    # NORMAL MODE: skip all validation
    if MODE_STRICT not in modes:
        return ValidationResult(stage="response_contract", passed=True)

    # STRICT MODE WITHOUT CONTRACT: invalid state
    if contract is None:
        return ValidationResult(
            stage="response_contract",
            passed=False,
            failed_rules=["strict_mode_without_contract"],
            correction_instructions=["strict_mode requires a contract"],
        )

    # Allow INSUFFICIENT_DATA as valid response
    uses_insufficient_data = _strict_mode_uses_insufficient_data(ai_output)
    if uses_insufficient_data:
        return ValidationResult(
            stage="response_contract",
            passed=True,
            contract_reference=contract.to_dict(),
        )

    # Backward-compatible path for legacy contracts used by existing tests.
    if contract.output_format != "typed_structured_json":
        failed: list[str] = []
        corrections: list[str] = []
        if contract.output_format in {"structured_json", "labeled_sections"}:
            for section in contract.required_sections:
                if section not in ai_output:
                    failed.append(f"missing_required_section:{section}")
                    corrections.append(f"Contract requires {section} section in output")
        return ValidationResult(
            stage="response_contract",
            passed=len(failed) == 0,
            failed_rules=failed,
            correction_instructions=corrections,
            contract_reference=contract.to_dict(),
        )

    parsed = _parse_typed_output(ai_output)
    if parsed is None:
        return ValidationResult(
            stage="response_contract",
            passed=False,
            failed_rules=["typed_output_required"],
            correction_instructions=["Respond with typed JSON or INSUFFICIENT GROUNDED KNOWLEDGE"],
            contract_reference=contract.to_dict(),
        )

    failed: list[str] = []
    corrections: list[str] = []

    mode_label = parsed.get("mode_label")
    if mode_label not in _allowed_mode_labels():
        failed.append("mode_label_required")
        corrections.append("mode_label must be RETRIEVED, INFERRED, or GENERATED")

    source_types: set[str] = set()
    for claim in parsed.get("claims", []):
        if isinstance(claim, dict):
            source_type = str(claim.get("source_type", "")).strip().lower()
            if source_type:
                source_types.add(source_type)

    if len(source_types) > 1:
        failed.append("mixed_source_types_without_explicit_marking")
        corrections.append(
            "Do not mix retrieval/inference/generation source types in one response."
        )

    allowed_sources_by_mode = {
        "RETRIEVED": {"retrieved", "external_retrieval", "external"},
        "INFERRED": {"inferred", "logical_inference"},
        "GENERATED": {"generated", "creative_generation", "creative"},
    }
    allowed_sources = allowed_sources_by_mode.get(str(mode_label), set())

    if mode_label in allowed_sources_by_mode and source_types and not source_types.issubset(
        allowed_sources
    ):
        failed.append(f"mode_label_source_mismatch:{str(mode_label).lower()}")

    return ValidationResult(
        stage="response_contract",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
        contract_reference=contract.to_dict(),
    )


def _build_feedback_prompt(
    base_prompt: str,
    validation_results: list[ValidationResult],
) -> str:
    """Append correction feedback to *base_prompt* for the re-prompt."""
    all_failed = [r for vr in validation_results for r in vr.failed_rules]
    all_missing = [r for vr in validation_results for r in vr.missing_fields]
    all_corrections = [r for vr in validation_results for r in vr.correction_instructions]

    feedback = (
        "\n\n--- MODE ENGINE CORRECTION FEEDBACK ---\n"
        "Your previous response failed validation. Please correct it.\n"
        f"Failed rules: {', '.join(all_failed)}\n"
    )
    if all_missing:
        feedback += f"Missing fields: {', '.join(all_missing)}\n"
    if all_corrections:
        feedback += "Required corrections:\n" + "\n".join(f"  - {c}" for c in all_corrections)
    feedback += "\n--- END CORRECTION FEEDBACK ---"
    return base_prompt + feedback


def _build_structured_failure(
    validation_results: list[ValidationResult],
    retry_count: int,
) -> dict[str, Any]:
    """Return a structured failure dict after retry exhaustion."""
    all_failed = list(dict.fromkeys(r for vr in validation_results for r in vr.failed_rules)) or [
        "validation_failed:unknown"
    ]
    all_missing = list(dict.fromkeys(r for vr in validation_results for r in vr.missing_fields))
    all_corrections = list(
        dict.fromkeys(r for vr in validation_results for r in vr.correction_instructions)
    ) or ["Respond with typed JSON object or INSUFFICIENT GROUNDED KNOWLEDGE"]
    return {
        "error": "VALIDATION_FAILED",
        "failed_rules": all_failed,
        "missing_fields": all_missing,
        "correction_instructions": all_corrections,
        "retry_count": retry_count,
    }


# ---------------------------------------------------------------------------
# Audit persistence — mandatory (block_if_log_not_written)
# ---------------------------------------------------------------------------


def _persist_audit_record(record: ModeEngineAuditRecord) -> None:
    """Write the audit record to the ops_events table.

    Enforcement — ``block_if_log_not_written``:
    - If the database IS configured, this write MUST succeed.  On any failure
      a ``RuntimeError("AUDIT_LOG_FAILURE: ...")`` is raised.  This propagates
      through the gateway and blocks the AI response from being returned.
    - If the database is NOT configured at all (``RuntimeError`` from
      ``get_engine``), a warning is logged and the function returns without
      blocking.  This is a deployment/configuration concern, not a runtime
      failure, and prevents tests without a DB from breaking.
    """
    try:
        from backend.app.database import get_engine

        engine = get_engine()
    except RuntimeError:
        logger.warning(
            "mode_engine: database not configured; audit record %s not persisted",
            record.audit_id,
        )
        return

    # Database IS configured — the write MUST succeed or the gateway is blocked.
    try:
        from sqlmodel import Session as _Session

        from backend.app.models import OpsEvent

        event = OpsEvent(
            source="backend",
            level="info",
            event_type="mode_engine.execution_v2.audit",
            message=f"MODE_ENGINE_EXECUTION_V2 [{record.audit_id}]",
            details_json={
                "audit_id": record.audit_id,
                "user_intent": record.user_intent[:500],
                "selected_modes": record.selected_modes,
                "transformed_prompt": record.transformed_prompt[:1000],
                "raw_ai_output": record.raw_ai_output[:2000],
                "validation_results": record.validation_results,
                "retry_count": record.retry_count,
                "final_output": record.final_output[:2000],
                "created_at": record.created_at,
            },
        )
        with _Session(engine) as session:
            session.add(event)
            session.commit()
    except Exception as exc:
        raise RuntimeError(f"AUDIT_LOG_FAILURE: {exc}") from exc


# ---------------------------------------------------------------------------
# Gateway coverage declaration
# ---------------------------------------------------------------------------

# MANDATORY: All AI calls for the listed endpoint MUST flow through
# mode_engine_gateway.  No AI response exits without passing full validation
# and having its audit record written.
#
# Covered by MODE_ENGINE_EXECUTION_V2:
#   POST /api/chat  — both stub path (no OPENAI_API_KEY) and live path (OPENAI_API_KEY)
#
# NOT covered here (governed by separate contracts):
#   POST /api/chat/intent — governed by INTERACTION_LAYER_V2
#
_GATEWAY_COVERAGE = frozenset({"POST /api/chat"})


# ---------------------------------------------------------------------------
# Hard boundary gate / Gateway — single mandatory entry/exit point
# ---------------------------------------------------------------------------


def mode_engine_gateway(
    *,
    user_intent: str,
    modes: list[str],
    ai_call: Callable[[str], str],
    base_system_prompt: str,
) -> tuple[str, ModeEngineAuditRecord]:
    """Single mandatory entry/exit point for all AI interactions on POST /api/chat.

    STRICT_MODE_EXECUTION_SPINE_LOCK_V1:
    - Mode isolation: modes is ONLY source of truth
    - is_strict = (modes != None AND "strict_mode" IN modes)
    - IF is_strict == FALSE: BYPASS all validation, return raw AI output
    - IF is_strict == TRUE: FULL pipeline MUST execute

    Both the stub path (no ``OPENAI_API_KEY``) and the live OpenAI path pass
    through this function.

    - When ``strict_mode`` is active, mode constraints are injected and the
      full validation pipeline runs before output exits the system.
    - When ``strict_mode`` is absent, the base prompt is passed through
      unchanged and validation is skipped.
    - An audit record is always written.

    Parameters
    ----------
    user_intent:
        The user's raw query/message.
    modes:
        Final mode list for this request. Empty or non-strict lists bypass
        mode injection and validation.
    ai_call:
        Callable ``(system_prompt: str) -> str`` that invokes the AI and
        returns raw text.  Exceptions propagate.
    base_system_prompt:
        Base system prompt without mode injection.  Mode constraints and
        conflict-resolution constraints are appended automatically.

    Returns
    -------
    (final_output, audit_record)
        *final_output* is the validated AI response string, or a
        JSON-serialised structured-failure dict when all retries are
        exhausted.

    Raises
    ------
    RuntimeError
        If the database is configured but the audit write fails
        (``block_if_log_not_written`` invariant).
    """
    # PHASE 1 — MODE ISOLATION LOCK
    # modes is the ONLY source of truth
    # strict_mode is ACTIVE only if: is_strict = (modes != None AND "strict_mode" IN modes)
    active_modes = resolve_modes(modes) if modes is not None else []
    is_strict = MODE_STRICT in active_modes

    if is_strict:
        # Log any configured mode conflicts; execution continues on active_modes.
        apply_mode_conflict_resolution(active_modes)

    audit = ModeEngineAuditRecord(
        user_intent=user_intent,
        selected_modes=list(active_modes),
    )

    # ------------------------------------------------------------------
    # PHASE 1 — MODE ISOLATION: NORMAL MODE PATH
    # IF is_strict == FALSE:
    #   - BYPASS: intent extraction, contract construction, validation, governance blocking
    #   - RETURN raw AI output
    #   - HTTP status MUST be 200 (handled by caller)
    # ------------------------------------------------------------------
    if not is_strict:
        audit.transformed_prompt = base_system_prompt
        raw_output = ai_call(base_system_prompt)
        audit.raw_ai_output = raw_output
        audit.final_output = raw_output
        _persist_audit_record(audit)
        return raw_output, audit

    # ------------------------------------------------------------------
    # PHASE 2 — CONTRACT BOUNDARY LOCK (STRICT MODE ONLY)
    # Contract MUST exist BEFORE validation
    # Contract MUST be validated BEFORE use
    # ------------------------------------------------------------------
    # PHASE 1 — Intent Extraction (for strict_mode only)
    intent_obj = extract_intent(user_intent)

    # PHASE 2 — Contract Construction (per request, not reused)
    contract_obj = construct_contract(intent_obj)

    # CONTRACT BOUNDARY — Contract Validation Gate (MANDATORY)
    # IF contract == None OR invalid:
    #   RETURN structured error:
    #     error = "CONTRACT_VALIDATION_FAILED"
    #     retry_count = 0
    #   TERMINATE (NO AI CALL)
    from backend.app.contract_construction import validate_contract

    contract_validation = validate_contract(contract_obj)

    if not contract_validation.passed or contract_obj is None:
        # PHASE 6 — FAILURE TAXONOMY LOCK: use "contract_validation_failure"
        import json as _json

        failure: dict[str, Any] = {
            "error": "VALIDATION_FAILED",
            "stage": "contract_boundary",
            "status": "blocked",
            "blocked_reason": "contract_validation_failure",
            "failed_rules": contract_validation.failed_rules,
            "missing_fields": contract_validation.missing_fields,
            "correction_instructions": contract_validation.correction_instructions,
            "retry_count": 0,
        }
        audit.final_output = _json.dumps(failure)
        audit.validation_results = [contract_validation.to_dict()]
        _persist_audit_record(audit)
        return audit.final_output, audit

    # ------------------------------------------------------------------
    # Stage 0: pre-generation constraints
    # ------------------------------------------------------------------
    ok, reason = stage_0_pre_generation_constraints(user_intent, active_modes)
    if not ok:
        import json as _json

        # PHASE 6 — FAILURE TAXONOMY: standardize error
        failure: dict[str, Any] = {
            "error": "PRE_GENERATION_BLOCKED",
            "status": "blocked",
            "blocked_reason": "validation_failure",
            "reason": reason,
            "retry_count": 0,
        }
        audit.final_output = _json.dumps(failure)
        _persist_audit_record(audit)
        return audit.final_output, audit

    # ------------------------------------------------------------------
    # PHASE 10 — PROMPT INJECTION LOCK
    # Build mode-injected system prompt (includes MODE ENGINE EXECUTION V2 CONSTRAINTS)
    # Enforced modes: strict_mode, builder_mode, prediction_mode
    # ------------------------------------------------------------------
    mode_injection = build_mode_system_prompt_injection(active_modes)
    transformed_prompt = base_system_prompt + mode_injection
    audit.transformed_prompt = transformed_prompt

    # ------------------------------------------------------------------
    # PHASE 7 — RETRY ENGINE LOCK
    # Retry loop: generate → PARSE FIRST → validate (3 stages) → retry on failure
    # MAX_RETRIES = 2 (constant)
    # PHASE 4 — PARSE-FIRST ENFORCEMENT
    # Flow: raw_output = ai_call(...) → parsed = parse_output(raw_output)
    #       IF parse FAILS: RETURN blocked with parse_failure, retry_count = MAX_RETRIES
    # PHASE 5 — VALIDATION PIPELINE LOCK
    # Order (MANDATORY): structural → logical → compliance
    # ALL stages MUST execute IF parse succeeds
    # ------------------------------------------------------------------
    current_prompt = transformed_prompt
    raw_output = ""
    last_validation_results: list[ValidationResult] = []
    attempt = 0

    # Loop for initial attempt + MAX_RETRIES retries (total MAX_RETRIES + 1 attempts)
    while attempt <= MAX_RETRIES:
        # AI generation — routed through ai_call (stub or OpenAI closure).
        raw_output = ai_call(current_prompt)
        audit.raw_ai_output = raw_output
        audit.retry_count = attempt

        # PHASE 4 — PARSE-FIRST ENFORCEMENT
        # Check if output can be parsed before validation
        # For typed_structured_json contracts, verify JSON parsing works
        parse_failed = False
        if contract_obj and contract_obj.output_format == "typed_structured_json":
            # Check for INSUFFICIENT_DATA fallback first (allowed bypass)
            uses_insufficient_data = _strict_mode_uses_insufficient_data(raw_output)
            if not uses_insufficient_data:
                parsed_output = _parse_typed_output(raw_output)
                if parsed_output is None:
                    # PHASE 6 — FAILURE TAXONOMY: "parse_failure"
                    # Mark as parse failure but continue to allow retries
                    parse_failed = True

        # PHASE 5 — VALIDATION PIPELINE LOCK
        # Three-stage CONTRACT-DRIVEN validation pipeline (MANDATORY ORDER)
        # 1. structural  2. logical  3. compliance
        # ALL stages MUST execute IF parse succeeds
        # EACH stage MUST return: stage, passed, failed_rules, correction_instructions
        if parse_failed:
            # Create a parse failure validation result
            parse_failure_result = ValidationResult(
                stage="parse",
                passed=False,
                failed_rules=["parse_failure"],
                correction_instructions=[
                    "Output must be valid JSON or INSUFFICIENT GROUNDED KNOWLEDGE"
                ],
            )
            last_validation_results = [parse_failure_result]
        else:
            v1 = stage_1_structural_validation(raw_output, active_modes, contract_obj)
            v2 = stage_2_logical_validation(raw_output, active_modes, contract_obj)
            v3 = stage_3_compliance_validation(raw_output, active_modes, contract_obj)
            last_validation_results = [v1, v2, v3]

        audit.validation_results = [vr.to_dict() for vr in last_validation_results]

        if all(vr.passed for vr in last_validation_results):
            # PHASE 5 — IF ALL pass: status = "approved"
            # All stages passed — exit retry loop.
            break

        # PHASE 5 — IF ANY stage fails: status = "blocked"
        # Increment attempt counter after failed validation
        attempt += 1

        if attempt <= MAX_RETRIES:
            # Build corrective feedback for the next attempt.
            current_prompt = _build_feedback_prompt(transformed_prompt, last_validation_results)

    # Check if we exhausted retries
    if not all(vr.passed for vr in last_validation_results):
        # PHASE 6 — FAILURE TAXONOMY: "validation_failure"
        # PHASE 7 — RETRY ENGINE: FINAL FAILURE MUST RETURN retry_count = MAX_RETRIES
        # No Silent Completion Rule: strict mode must return explicit insufficiency
        import json as _json

        # Collect failed_rules and correction_instructions from all validation results
        all_failed = list(
            dict.fromkeys(r for vr in last_validation_results for r in vr.failed_rules)
        )
        all_corrections = list(
            dict.fromkeys(r for vr in last_validation_results for r in vr.correction_instructions)
        )

        audit.retry_count = MAX_RETRIES
        failure_dict = {
            "error": "VALIDATION_FAILED",
            "status": "blocked",
            "blocked_reason": "validation_failure",
            "failed_rules": all_failed or ["validation_failed"],
            "correction_instructions": all_corrections or [
                "Respond with typed JSON object or INSUFFICIENT GROUNDED KNOWLEDGE"
            ],
            "validation_results": audit.validation_results,
            "retry_count": MAX_RETRIES,
            "insufficiency_message": STRICT_MODE_INSUFFICIENT_GROUNDED_KNOWLEDGE,
        }
        audit.final_output = _json.dumps(failure_dict)
        _persist_audit_record(audit)  # raises if DB configured + write fails
        return audit.final_output, audit

    # ------------------------------------------------------------------
    # PHASE 8 — SUCCESS PATH LOCK
    # IF status == "approved":
    #   - RETURN CLEAN output (NO error wrapper)
    #   - NO JSON failure structure
    #   - NO retry metadata
    # Hard boundary gate: only validated output exits the system.
    # ------------------------------------------------------------------
    audit.final_output = raw_output
    _persist_audit_record(audit)  # raises if DB configured + write fails
    return raw_output, audit
