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

No AI response may exit the system without passing full validation.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

MODE_STRICT = "strict_mode"
MODE_PREDICTION = "prediction_mode"
MODE_DEBUG = "debug_mode"
MODE_BUILDER = "builder_mode"
MODE_AUDIT = "audit_mode"

SUPPORTED_MODES = frozenset(
    {MODE_STRICT, MODE_PREDICTION, MODE_DEBUG, MODE_BUILDER, MODE_AUDIT}
)

# Lower index = higher priority (strict_mode has highest priority).
MODE_PRIORITY_ORDER = [MODE_STRICT, MODE_PREDICTION, MODE_DEBUG, MODE_AUDIT, MODE_BUILDER]

_MODE_PRIORITY: dict[str, int] = {m: i for i, m in enumerate(MODE_PRIORITY_ORDER)}

# Maximum number of validation retries before structured failure is returned.
MAX_RETRIES = 2


def resolve_modes(requested: list[str]) -> list[str]:
    """Validate, deduplicate, and sort modes by priority.

    Unknown modes are silently filtered out.  If the resulting list is empty,
    falls back to ``[strict_mode]``.
    """
    valid = sorted(
        {m for m in requested if m in SUPPORTED_MODES},
        key=lambda m: _MODE_PRIORITY.get(m, 999),
    )
    return valid if valid else [MODE_STRICT]


def effective_mode(modes: list[str]) -> str:
    """Return the highest-priority active mode from *modes*."""
    resolved = resolve_modes(modes)
    return resolved[0]


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
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "failed_rules": self.failed_rules,
            "missing_fields": self.missing_fields,
            "correction_instructions": self.correction_instructions,
        }


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
    if not user_intent or not user_intent.strip():
        return False, "missing_required_input: query must not be empty"
    return True, ""


def build_mode_system_prompt_injection(modes: list[str]) -> str:
    """Return mode-specific constraint text to append to the system prompt."""
    lines = ["\n\n--- MODE ENGINE EXECUTION V2 CONSTRAINTS ---"]
    lines.append(f"Active modes: {', '.join(modes)}")

    if MODE_STRICT in modes:
        lines.append(
            "STRICT MODE: You MUST NOT guess or assume. "
            "If you lack sufficient information respond with exactly: "
            "INSUFFICIENT_DATA: <reason>. Hallucination is prohibited."
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
            "AUDIT MODE: Your response MUST include a "
            "RISK_IDENTIFICATION: <risks or none> section."
        )

    if MODE_BUILDER in modes:
        lines.append(
            "BUILDER MODE: Your response MUST be organized into named sections "
            "using SECTION_<NAME>: prefixes (e.g. SECTION_OVERVIEW:)."
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


def stage_1_structural_validation(ai_output: str, modes: list[str]) -> ValidationResult:
    """Validate that required fields are present in *ai_output*."""
    missing: list[str] = []
    failed: list[str] = []
    corrections: list[str] = []

    for mode in modes:
        for marker in _REQUIRED_MARKERS.get(mode, []):
            if marker not in ai_output:
                missing.append(marker)
                failed.append(f"{mode}:missing_field:{marker}")
                corrections.append(f"Include a {marker} section in your response")

    if MODE_STRICT in modes and not ai_output.strip():
        failed.append("strict_mode:empty_output")
        missing.append("non_empty_output")
        corrections.append(
            "Provide a non-empty response or state INSUFFICIENT_DATA: <reason>"
        )

    return ValidationResult(
        stage="structural",
        passed=len(failed) == 0,
        failed_rules=failed,
        missing_fields=missing,
        correction_instructions=corrections,
    )


# ---------------------------------------------------------------------------
# Stage 2: Logical validation
# ---------------------------------------------------------------------------


def stage_2_logical_validation(ai_output: str, modes: list[str]) -> ValidationResult:
    """Validate logical consistency of *ai_output*."""
    failed: list[str] = []
    corrections: list[str] = []

    if MODE_PREDICTION in modes:
        # Assumptions must be explicit (non-empty after the label).
        if "ASSUMPTIONS:" in ai_output:
            assumptions_text = (
                ai_output.split("ASSUMPTIONS:", 1)[1].split("\n")[0].strip()
            )
            if not assumptions_text:
                failed.append("undeclared_assumptions")
                corrections.append("ASSUMPTIONS: section must contain explicit content")

        # Confidence must be numeric [0,1] or a recognised categorical value.
        if "CONFIDENCE:" in ai_output:
            conf_text = (
                ai_output.split("CONFIDENCE:", 1)[1].split("\n")[0].strip().lower()
            )
            _VALID_CATEGORICAL = {"low", "medium", "high", "very low", "very high"}
            is_numeric = False
            try:
                val = float(conf_text)
                is_numeric = 0.0 <= val <= 1.0
            except ValueError:
                pass
            is_categorical = any(cat in conf_text for cat in _VALID_CATEGORICAL)
            if not is_numeric and not is_categorical:
                failed.append("invalid_confidence")
                corrections.append(
                    "CONFIDENCE: must be a number between 0 and 1, or "
                    "one of: low / medium / high"
                )

        # Alternatives section must be non-empty.
        if "ALTERNATIVES:" in ai_output:
            alt_section = ai_output.split("ALTERNATIVES:", 1)[1]
            alt_lines = [
                ln.strip()
                for ln in alt_section.split("\n")
                if ln.strip() and not ln.strip().endswith(":")
            ]
            if not alt_lines:
                failed.append("alternatives_not_distinct")
                corrections.append(
                    "ALTERNATIVES: must list at least one distinct alternative"
                )

    return ValidationResult(
        stage="logical",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
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


def stage_3_compliance_validation(ai_output: str, modes: list[str]) -> ValidationResult:
    """Validate mode-specific compliance rules on *ai_output*."""
    failed: list[str] = []
    corrections: list[str] = []

    if MODE_STRICT in modes:
        lower = ai_output.lower()
        has_insufficient_data = "INSUFFICIENT_DATA:" in ai_output
        guesses = [ind for ind in _GUESSING_INDICATORS if ind in lower]
        if guesses and not has_insufficient_data:
            failed.append("strict_mode:guessing_detected")
            corrections.append(
                "Strict mode prohibits guessing. "
                "Replace uncertain statements with INSUFFICIENT_DATA: <reason>"
            )

    if MODE_PREDICTION in modes and "ALTERNATIVES:" not in ai_output:
        failed.append("prediction_mode:multiple_paths_absent")
        corrections.append(
            "prediction_mode requires an ALTERNATIVES: section with multiple paths"
        )

    if MODE_DEBUG in modes and "STEP_" not in ai_output and "STEP 1" not in ai_output:
        failed.append("debug_mode:stepwise_reasoning_absent")
        corrections.append(
            "debug_mode requires stepwise reasoning "
            "(e.g. STEP_1: …, STEP_2: …)"
        )

    if MODE_AUDIT in modes and "RISK_IDENTIFICATION:" not in ai_output:
        failed.append("audit_mode:risk_identification_absent")
        corrections.append(
            "audit_mode requires a RISK_IDENTIFICATION: section"
        )

    return ValidationResult(
        stage="compliance",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
    )


# ---------------------------------------------------------------------------
# Retry engine helpers
# ---------------------------------------------------------------------------


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
        feedback += "Required corrections:\n" + "\n".join(
            f"  - {c}" for c in all_corrections
        )
    feedback += "\n--- END CORRECTION FEEDBACK ---"
    return base_prompt + feedback


def _build_structured_failure(
    validation_results: list[ValidationResult],
    retry_count: int,
) -> dict[str, Any]:
    """Return a structured failure dict after retry exhaustion."""
    all_failed = [r for vr in validation_results for r in vr.failed_rules]
    all_missing = [r for vr in validation_results for r in vr.missing_fields]
    all_corrections = [r for vr in validation_results for r in vr.correction_instructions]
    return {
        "error": "VALIDATION_FAILED",
        "failed_rules": all_failed,
        "missing_fields": all_missing,
        "suggested_fix": all_corrections,
        "retry_count": retry_count,
    }


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------


def _persist_audit_record(record: ModeEngineAuditRecord) -> None:
    """Write the audit record to the ops_events table.  Never raises."""
    try:
        from backend.app.ops_log import log_event

        log_event(
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
    except Exception as exc:  # pragma: no cover
        logger.warning("mode_engine: failed to persist audit record: %s", exc)


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
    """Single mandatory entry/exit point for all AI interactions.

    Parameters
    ----------
    user_intent:
        The user's raw query/message.
    modes:
        Requested mode list.  Unknown entries are filtered; empty list
        falls back to ``[strict_mode]``.
    ai_call:
        Callable ``(system_prompt: str) -> str`` that invokes the AI and
        returns raw text.  Must not raise; exceptions are propagated.
    base_system_prompt:
        Base system prompt without mode injection.  Mode constraints are
        appended automatically.

    Returns
    -------
    (final_output, audit_record)
        *final_output* is the validated AI response string, or a JSON-serialised
        structured-failure dict when all retries are exhausted.
    """
    resolved_modes = resolve_modes(modes)
    audit = ModeEngineAuditRecord(
        user_intent=user_intent,
        selected_modes=resolved_modes,
    )

    # ------------------------------------------------------------------
    # Stage 0: pre-generation constraints
    # ------------------------------------------------------------------
    ok, reason = stage_0_pre_generation_constraints(user_intent, resolved_modes)
    if not ok:
        import json as _json

        failure: dict[str, Any] = {"error": "PRE_GENERATION_BLOCKED", "reason": reason}
        audit.final_output = _json.dumps(failure)
        _persist_audit_record(audit)
        return audit.final_output, audit

    # ------------------------------------------------------------------
    # Build mode-injected system prompt
    # ------------------------------------------------------------------
    mode_injection = build_mode_system_prompt_injection(resolved_modes)
    transformed_prompt = base_system_prompt + mode_injection
    audit.transformed_prompt = transformed_prompt

    # ------------------------------------------------------------------
    # Retry loop: generate → validate → retry on failure
    # ------------------------------------------------------------------
    current_prompt = transformed_prompt
    raw_output = ""
    last_validation_results: list[ValidationResult] = []

    for attempt in range(MAX_RETRIES + 1):
        # AI generation
        raw_output = ai_call(current_prompt)
        audit.raw_ai_output = raw_output
        audit.retry_count = attempt

        # Validation pipeline
        v1 = stage_1_structural_validation(raw_output, resolved_modes)
        v2 = stage_2_logical_validation(raw_output, resolved_modes)
        v3 = stage_3_compliance_validation(raw_output, resolved_modes)
        last_validation_results = [v1, v2, v3]
        audit.validation_results = [vr.to_dict() for vr in last_validation_results]

        if all(vr.passed for vr in last_validation_results):
            # All stages passed — exit retry loop.
            break

        if attempt < MAX_RETRIES:
            # Build corrective feedback for the next attempt.
            current_prompt = _build_feedback_prompt(
                transformed_prompt, last_validation_results
            )
        else:
            # ----------------------------------------------------------
            # Retry exhaustion — return structured failure (hard gate).
            # ----------------------------------------------------------
            import json as _json

            failure_dict = _build_structured_failure(last_validation_results, attempt)
            audit.final_output = _json.dumps(failure_dict)
            _persist_audit_record(audit)
            return audit.final_output, audit

    # ------------------------------------------------------------------
    # Hard boundary gate: only validated output exits the system.
    # ------------------------------------------------------------------
    audit.final_output = raw_output
    _persist_audit_record(audit)
    return raw_output, audit
