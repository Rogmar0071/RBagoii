"""
Tests for MODE_ENGINE_EXECUTION_V2 (backend.app.mode_engine).

Covers:
- Mode resolution and priority ordering
- Stage 0: pre-generation constraints
- Stage 1: structural validation
- Stage 2: logical validation
- Stage 3: compliance validation
- Retry engine behaviour
- Hard boundary gate / gateway function
- Audit record creation
- Integration with POST /api/chat (modes field wired through gateway)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mode_engine")

from backend.app.mode_engine import (
    MAX_RETRIES,
    MODE_AUDIT,
    MODE_BUILDER,
    MODE_DEBUG,
    MODE_PREDICTION,
    MODE_STRICT,
    ModeEngineAuditRecord,
    ValidationResult,
    _build_feedback_prompt,
    _build_structured_failure,
    build_mode_system_prompt_injection,
    effective_mode,
    mode_engine_gateway,
    resolve_modes,
    stage_0_pre_generation_constraints,
    stage_1_structural_validation,
    stage_2_logical_validation,
    stage_3_compliance_validation,
)

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_mode_engine.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# resolve_modes
# ---------------------------------------------------------------------------


class TestResolveModes:
    def test_unknown_modes_filtered(self):
        assert resolve_modes(["unknown_mode"]) == [MODE_STRICT]

    def test_empty_list_defaults_to_strict(self):
        assert resolve_modes([]) == [MODE_STRICT]

    def test_priority_order(self):
        modes = resolve_modes([MODE_BUILDER, MODE_STRICT, MODE_PREDICTION])
        assert modes[0] == MODE_STRICT
        assert modes[1] == MODE_PREDICTION
        assert modes[2] == MODE_BUILDER

    def test_deduplication(self):
        result = resolve_modes([MODE_STRICT, MODE_STRICT])
        assert result.count(MODE_STRICT) == 1

    def test_single_valid_mode(self):
        assert resolve_modes([MODE_PREDICTION]) == [MODE_PREDICTION]

    def test_all_supported_modes_accepted(self):
        all_modes = [MODE_STRICT, MODE_PREDICTION, MODE_DEBUG, MODE_BUILDER, MODE_AUDIT]
        result = resolve_modes(all_modes)
        assert set(result) == set(all_modes)


class TestEffectiveMode:
    def test_strict_overrides_prediction(self):
        assert effective_mode([MODE_PREDICTION, MODE_STRICT]) == MODE_STRICT

    def test_single_mode(self):
        assert effective_mode([MODE_DEBUG]) == MODE_DEBUG

    def test_empty_falls_back_to_strict(self):
        assert effective_mode([]) == MODE_STRICT


# ---------------------------------------------------------------------------
# build_mode_system_prompt_injection
# ---------------------------------------------------------------------------


class TestBuildModeSystemPromptInjection:
    def test_strict_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_STRICT])
        assert "STRICT MODE" in prompt
        assert "INSUFFICIENT_DATA" in prompt

    def test_prediction_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_PREDICTION])
        assert "PREDICTION MODE" in prompt
        assert "ASSUMPTIONS:" in prompt
        assert "ALTERNATIVES:" in prompt
        assert "CONFIDENCE:" in prompt
        assert "MISSING_DATA:" in prompt

    def test_debug_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_DEBUG])
        assert "DEBUG MODE" in prompt
        assert "STEP_" in prompt

    def test_audit_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_AUDIT])
        assert "AUDIT MODE" in prompt
        assert "RISK_IDENTIFICATION:" in prompt

    def test_builder_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_BUILDER])
        assert "BUILDER MODE" in prompt
        assert "SECTION_" in prompt

    def test_contains_active_modes_list(self):
        prompt = build_mode_system_prompt_injection([MODE_STRICT, MODE_DEBUG])
        assert "strict_mode" in prompt
        assert "debug_mode" in prompt


# ---------------------------------------------------------------------------
# Stage 0
# ---------------------------------------------------------------------------


class TestStage0:
    def test_empty_input_blocked(self):
        ok, reason = stage_0_pre_generation_constraints("", [MODE_STRICT])
        assert ok is False
        assert "missing_required_input" in reason

    def test_whitespace_only_blocked(self):
        ok, reason = stage_0_pre_generation_constraints("   ", [MODE_STRICT])
        assert ok is False

    def test_valid_input_passes(self):
        ok, reason = stage_0_pre_generation_constraints("Hello world", [MODE_STRICT])
        assert ok is True
        assert reason == ""


# ---------------------------------------------------------------------------
# Stage 1: Structural validation
# ---------------------------------------------------------------------------


class TestStage1StructuralValidation:
    def test_prediction_mode_all_fields_present(self):
        output = (
            "ASSUMPTIONS: none\n"
            "ALTERNATIVES: A, B\n"
            "CONFIDENCE: 0.8\n"
            "MISSING_DATA: none"
        )
        result = stage_1_structural_validation(output, [MODE_PREDICTION])
        assert result.passed is True

    def test_prediction_mode_missing_assumptions(self):
        output = "ALTERNATIVES: A\nCONFIDENCE: 0.5\nMISSING_DATA: none"
        result = stage_1_structural_validation(output, [MODE_PREDICTION])
        assert result.passed is False
        assert any("ASSUMPTIONS:" in r for r in result.missing_fields)

    def test_prediction_mode_missing_multiple_fields(self):
        result = stage_1_structural_validation("Just some text", [MODE_PREDICTION])
        assert result.passed is False
        assert len(result.missing_fields) >= 4

    def test_strict_mode_empty_output_fails(self):
        result = stage_1_structural_validation("", [MODE_STRICT])
        assert result.passed is False
        assert "strict_mode:empty_output" in result.failed_rules

    def test_strict_mode_non_empty_passes(self):
        result = stage_1_structural_validation("Valid response.", [MODE_STRICT])
        assert result.passed is True

    def test_debug_mode_requires_step_marker(self):
        result = stage_1_structural_validation("No steps here.", [MODE_DEBUG])
        assert result.passed is False

    def test_debug_mode_step_marker_present(self):
        result = stage_1_structural_validation("STEP_1: first step", [MODE_DEBUG])
        assert result.passed is True

    def test_audit_mode_requires_risk_identification(self):
        result = stage_1_structural_validation("Some output.", [MODE_AUDIT])
        assert result.passed is False
        assert any("RISK_IDENTIFICATION:" in m for m in result.missing_fields)

    def test_audit_mode_with_risk_identification(self):
        result = stage_1_structural_validation(
            "RISK_IDENTIFICATION: low", [MODE_AUDIT]
        )
        assert result.passed is True

    def test_builder_mode_requires_section_marker(self):
        result = stage_1_structural_validation("No sections.", [MODE_BUILDER])
        assert result.passed is False

    def test_builder_mode_with_section(self):
        result = stage_1_structural_validation("SECTION_OVERVIEW: intro", [MODE_BUILDER])
        assert result.passed is True


# ---------------------------------------------------------------------------
# Stage 2: Logical validation
# ---------------------------------------------------------------------------


class TestStage2LogicalValidation:
    def test_no_prediction_mode_always_passes(self):
        result = stage_2_logical_validation("any output", [MODE_STRICT])
        assert result.passed is True

    def test_prediction_mode_valid_confidence_numeric(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: 0.75\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is True

    def test_prediction_mode_valid_confidence_categorical(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: high\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is True

    def test_prediction_mode_invalid_confidence(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: not-a-number\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is False
        assert "invalid_confidence" in result.failed_rules

    def test_prediction_mode_empty_assumptions(self):
        output = "ASSUMPTIONS:\nALTERNATIVES: x\nCONFIDENCE: 0.5\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is False
        assert "undeclared_assumptions" in result.failed_rules

    def test_prediction_mode_out_of_range_confidence(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: 1.5\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is False

    def test_prediction_mode_confidence_boundary_0(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: 0\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is True

    def test_prediction_mode_confidence_boundary_1(self):
        output = "ASSUMPTIONS: a\nALTERNATIVES: x\nCONFIDENCE: 1\nMISSING_DATA: none"
        result = stage_2_logical_validation(output, [MODE_PREDICTION])
        assert result.passed is True


# ---------------------------------------------------------------------------
# Stage 3: Compliance validation
# ---------------------------------------------------------------------------


class TestStage3ComplianceValidation:
    def test_strict_mode_no_guessing_passes(self):
        result = stage_3_compliance_validation(
            "The system uses a REST API.", [MODE_STRICT]
        )
        assert result.passed is True

    def test_strict_mode_guessing_detected(self):
        result = stage_3_compliance_validation(
            "I think the file is in /src.", [MODE_STRICT]
        )
        assert result.passed is False
        assert "strict_mode:guessing_detected" in result.failed_rules

    def test_strict_mode_insufficient_data_allows_hedging(self):
        # If INSUFFICIENT_DATA is declared, guessing indicators are acceptable.
        result = stage_3_compliance_validation(
            "I think this might work. INSUFFICIENT_DATA: no context provided.",
            [MODE_STRICT],
        )
        assert result.passed is True

    def test_prediction_mode_requires_alternatives(self):
        result = stage_3_compliance_validation(
            "Some output without alternatives.", [MODE_PREDICTION]
        )
        assert result.passed is False
        assert "prediction_mode:multiple_paths_absent" in result.failed_rules

    def test_prediction_mode_with_alternatives_passes(self):
        result = stage_3_compliance_validation(
            "ALTERNATIVES: A or B", [MODE_PREDICTION]
        )
        assert result.passed is True

    def test_debug_mode_requires_step(self):
        result = stage_3_compliance_validation("No step here.", [MODE_DEBUG])
        assert result.passed is False

    def test_debug_mode_with_step_passes(self):
        result = stage_3_compliance_validation("STEP_1: do this", [MODE_DEBUG])
        assert result.passed is True

    def test_audit_mode_requires_risk_identification(self):
        result = stage_3_compliance_validation("Output.", [MODE_AUDIT])
        assert result.passed is False

    def test_audit_mode_with_risk_identification_passes(self):
        result = stage_3_compliance_validation(
            "RISK_IDENTIFICATION: none", [MODE_AUDIT]
        )
        assert result.passed is True


# ---------------------------------------------------------------------------
# Retry engine helpers
# ---------------------------------------------------------------------------


class TestBuildFeedbackPrompt:
    def test_feedback_contains_failed_rules(self):
        vr = ValidationResult(
            stage="structural",
            passed=False,
            failed_rules=["strict_mode:empty_output"],
            missing_fields=["non_empty_output"],
            correction_instructions=["Provide a non-empty response"],
        )
        prompt = _build_feedback_prompt("base prompt", [vr])
        assert "strict_mode:empty_output" in prompt
        assert "non_empty_output" in prompt
        assert "Provide a non-empty response" in prompt

    def test_feedback_appended_to_base(self):
        vr = ValidationResult(stage="logical", passed=False, failed_rules=["x"])
        prompt = _build_feedback_prompt("BASE", [vr])
        assert prompt.startswith("BASE")
        assert "x" in prompt


class TestBuildStructuredFailure:
    def test_structure(self):
        vr = ValidationResult(
            stage="compliance",
            passed=False,
            failed_rules=["strict_mode:guessing_detected"],
            correction_instructions=["fix it"],
        )
        result = _build_structured_failure([vr], retry_count=2)
        assert result["error"] == "VALIDATION_FAILED"
        assert "strict_mode:guessing_detected" in result["failed_rules"]
        assert "fix it" in result["suggested_fix"]
        assert result["retry_count"] == 2


# ---------------------------------------------------------------------------
# mode_engine_gateway
# ---------------------------------------------------------------------------


class TestModeEngineGateway:
    def test_valid_output_passes_through(self):
        ai_call = MagicMock(return_value="A valid response.")
        output, audit = mode_engine_gateway(
            user_intent="Hello",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="System prompt.",
        )
        assert output == "A valid response."
        assert audit.final_output == "A valid response."
        assert audit.retry_count == 0
        ai_call.assert_called_once()

    def test_empty_user_intent_returns_pre_generation_blocked(self):
        ai_call = MagicMock(return_value="irrelevant")
        output, audit = mode_engine_gateway(
            user_intent="",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        failure = json.loads(output)
        assert failure["error"] == "PRE_GENERATION_BLOCKED"
        ai_call.assert_not_called()

    def test_mode_constraints_injected_into_prompt(self):
        received_prompts: list[str] = []

        def ai_call(system_prompt: str) -> str:
            received_prompts.append(system_prompt)
            return "Response."

        mode_engine_gateway(
            user_intent="test",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="BASE",
        )
        assert received_prompts, "ai_call must be called at least once"
        assert "MODE ENGINE EXECUTION V2 CONSTRAINTS" in received_prompts[0]
        assert "BASE" in received_prompts[0]

    def test_retry_on_validation_failure(self):
        call_count = {"n": 0}

        def ai_call(system_prompt: str) -> str:
            call_count["n"] += 1
            # Second call returns valid output; first fails strict_mode guessing check.
            if call_count["n"] == 1:
                return "I think this is correct."
            return "This is definitely correct."

        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert call_count["n"] == 2
        assert output == "This is definitely correct."
        assert audit.retry_count == 1

    def test_structured_failure_after_exhausted_retries(self):
        # Always return guessing language so every attempt fails strict_mode.
        ai_call = MagicMock(return_value="I think maybe probably this is right.")
        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        failure = json.loads(output)
        assert failure["error"] == "VALIDATION_FAILED"
        assert failure["retry_count"] == MAX_RETRIES
        assert ai_call.call_count == MAX_RETRIES + 1

    def test_audit_record_fields_populated(self):
        ai_call = MagicMock(return_value="Clean response.")
        output, audit = mode_engine_gateway(
            user_intent="user query",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="base",
        )
        assert audit.user_intent == "user query"
        assert MODE_STRICT in audit.selected_modes
        assert "MODE ENGINE" in audit.transformed_prompt
        assert audit.raw_ai_output == "Clean response."
        assert audit.final_output == "Clean response."
        assert len(audit.validation_results) == 3  # three stages

    def test_unknown_modes_resolved_to_strict(self):
        ai_call = MagicMock(return_value="Valid response.")
        output, audit = mode_engine_gateway(
            user_intent="query",
            modes=["nonexistent_mode"],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert MODE_STRICT in audit.selected_modes

    def test_prediction_mode_valid_output(self):
        valid_prediction = (
            "ASSUMPTIONS: none assumed\n"
            "ALTERNATIVES: option A or option B\n"
            "CONFIDENCE: 0.7\n"
            "MISSING_DATA: none"
        )
        ai_call = MagicMock(return_value=valid_prediction)
        output, audit = mode_engine_gateway(
            user_intent="predict something",
            modes=[MODE_PREDICTION],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert output == valid_prediction

    def test_feedback_prompt_sent_on_retry(self):
        received_prompts: list[str] = []

        def ai_call(system_prompt: str) -> str:
            received_prompts.append(system_prompt)
            if len(received_prompts) == 1:
                return "I think I know the answer."  # fails strict_mode
            return "The answer is documented in spec A."  # passes

        mode_engine_gateway(
            user_intent="query",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert len(received_prompts) == 2
        assert "MODE ENGINE CORRECTION FEEDBACK" in received_prompts[1]


# ---------------------------------------------------------------------------
# Integration: POST /api/chat with modes field
# ---------------------------------------------------------------------------


class TestChatEndpointModeEngine:
    def test_default_modes_strict_no_api_key(self, client: TestClient, monkeypatch):
        """Without modes field, strict_mode is the default and stub passes validation."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["reply"]

    def test_explicit_strict_mode(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "modes": ["strict_mode"]},
            headers=_auth(),
        )
        assert resp.status_code == 200

    def test_unknown_modes_ignored(self, client: TestClient, monkeypatch):
        """Unknown mode names are silently dropped; falls back to strict_mode."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "modes": ["unknown_mode_xyz"]},
            headers=_auth(),
        )
        assert resp.status_code == 200

    def test_modes_field_rejected_non_list(self, client: TestClient, monkeypatch):
        """Passing a non-list for modes should result in a 422 validation error."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "modes": "strict_mode"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_prediction_mode_with_mocked_openai(self, client: TestClient, monkeypatch):
        """prediction_mode: mocked OpenAI returns valid prediction output."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        valid_prediction = (
            "ASSUMPTIONS: none\n"
            "ALTERNATIVES: option A, option B\n"
            "CONFIDENCE: 0.8\n"
            "MISSING_DATA: none"
        )

        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value=valid_prediction):
            resp = client.post(
                "/api/chat",
                json={"message": "Predict the outcome", "modes": ["prediction_mode"]},
                headers=_auth(),
            )

        assert resp.status_code == 200
        assert "ASSUMPTIONS:" in resp.json()["reply"]

    def test_validation_failure_returns_structured_error_string(
        self, client: TestClient, monkeypatch
    ):
        """When AI always fails validation, reply contains the structured failure JSON."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        # Always return output that fails prediction_mode structural validation.
        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value="plain answer"):
            resp = client.post(
                "/api/chat",
                json={"message": "predict", "modes": ["prediction_mode"]},
                headers=_auth(),
            )

        assert resp.status_code == 200
        reply = resp.json()["reply"]
        parsed = json.loads(reply)
        assert parsed["error"] == "VALIDATION_FAILED"
