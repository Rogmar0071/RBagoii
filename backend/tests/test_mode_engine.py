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
    _GATEWAY_COVERAGE,
    _MODE_CONFLICT_RULES,
    _build_feedback_prompt,
    _build_structured_failure,
    _check_response_contract,
    _persist_audit_record,
    apply_mode_conflict_resolution,
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
        assert len(audit.validation_results) == 4  # four stages: structural, logical, compliance, response_contract

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


# ---------------------------------------------------------------------------
# Req 1: Mandatory audit enforcement
# ---------------------------------------------------------------------------


class TestMandatoryAudit:
    """Enforce audit as mandatory — block_if_log_not_written."""

    def test_persist_audit_record_writes_to_db(self, tmp_path):
        """When DB is configured, _persist_audit_record writes an OpsEvent row."""
        import backend.app.database as db_module
        from sqlmodel import Session, select
        from backend.app.models import OpsEvent

        db_path = tmp_path / "audit_mandatory.db"
        db_module.reset_engine(f"sqlite:///{db_path}")
        db_module.init_db()

        record = ModeEngineAuditRecord(user_intent="test audit write")
        _persist_audit_record(record)

        with Session(db_module.get_engine()) as s:
            rows = s.exec(
                select(OpsEvent).where(
                    OpsEvent.event_type == "mode_engine.execution_v2.audit"
                )
            ).all()
        assert len(rows) == 1
        assert record.audit_id in rows[0].details_json["audit_id"]

        db_module.reset_engine()

    def test_persist_audit_record_raises_when_db_write_fails(
        self, monkeypatch, tmp_path
    ):
        """_persist_audit_record raises RuntimeError('AUDIT_LOG_FAILURE') when write fails."""
        import backend.app.database as db_module
        from sqlmodel import Session

        db_path = tmp_path / "audit_fail.db"
        db_module.reset_engine(f"sqlite:///{db_path}")
        db_module.init_db()

        def _bad_commit(self):
            raise Exception("simulated DB failure")

        monkeypatch.setattr(Session, "commit", _bad_commit)

        record = ModeEngineAuditRecord(user_intent="will fail")
        with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
            _persist_audit_record(record)

        db_module.reset_engine()

    def test_gateway_raises_on_audit_failure(self, monkeypatch):
        """mode_engine_gateway propagates AUDIT_LOG_FAILURE (block_if_log_not_written)."""
        import backend.app.mode_engine as me

        def _fail_persist(rec):
            raise RuntimeError("AUDIT_LOG_FAILURE: simulated")

        monkeypatch.setattr(me, "_persist_audit_record", _fail_persist)

        with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
            mode_engine_gateway(
                user_intent="test",
                modes=[MODE_STRICT],
                ai_call=lambda sp: "Valid response.",
                base_system_prompt="",
            )

    def test_post_chat_returns_500_when_audit_fails(self, monkeypatch):
        """POST /api/chat returns HTTP 500 when audit write fails (block_if_log_not_written)."""
        # Use raise_server_exceptions=False so unhandled RuntimeError becomes HTTP 500.
        non_raising_client = TestClient(app, raise_server_exceptions=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.mode_engine as me

        def _fail_persist(rec):
            raise RuntimeError("AUDIT_LOG_FAILURE: simulated")

        monkeypatch.setattr(me, "_persist_audit_record", _fail_persist)

        resp = non_raising_client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=_auth(),
        )
        assert resp.status_code == 500

    def test_audit_record_no_silent_fallback_when_db_configured(
        self, monkeypatch, tmp_path
    ):
        """Audit failure is never silently swallowed when DB is configured."""
        import backend.app.database as db_module
        from sqlmodel import Session

        db_path = tmp_path / "audit_no_fallback.db"
        db_module.reset_engine(f"sqlite:///{db_path}")
        db_module.init_db()

        write_called = {"n": 0}

        original_commit = Session.commit

        def _counting_commit(self):
            write_called["n"] += 1
            raise Exception("forced DB error")

        monkeypatch.setattr(Session, "commit", _counting_commit)

        record = ModeEngineAuditRecord(user_intent="no fallback test")
        with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
            _persist_audit_record(record)

        assert write_called["n"] >= 1, "commit must be attempted before raising"

        db_module.reset_engine()


# ---------------------------------------------------------------------------
# Req 2: Stub path goes through full validation (not a bypass)
# ---------------------------------------------------------------------------


class TestStubPathThroughGateway:
    """Confirm the stub path (no OPENAI_API_KEY) is NOT a bypass of the gateway."""

    def test_stub_path_calls_gateway(self, client: TestClient, monkeypatch):
        """With no OpenAI key, mode_engine_gateway is called for every chat request."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        gateway_calls: list[dict] = []
        original_gw = cr.mode_engine_gateway

        def _tracking_gateway(**kwargs):
            gateway_calls.append({"user_intent": kwargs.get("user_intent")})
            return original_gw(**kwargs)

        monkeypatch.setattr(cr, "mode_engine_gateway", _tracking_gateway)

        resp = client.post(
            "/api/chat",
            json={"message": "Stub gateway test"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert len(gateway_calls) == 1
        assert gateway_calls[0]["user_intent"] == "Stub gateway test"

    def test_stub_passes_strict_mode_validation(self):
        """Stub reply passes all four validation stages in strict_mode."""
        from backend.app.chat_routes import _stub_reply

        stub = _stub_reply("hello")
        v1 = stage_1_structural_validation(stub, [MODE_STRICT])
        v2 = stage_2_logical_validation(stub, [MODE_STRICT])
        v3 = stage_3_compliance_validation(stub, [MODE_STRICT])
        v4 = _check_response_contract(stub, [MODE_STRICT])

        assert v1.passed, f"stage_1 failed: {v1.failed_rules}"
        assert v2.passed, f"stage_2 failed: {v2.failed_rules}"
        assert v3.passed, f"stage_3 failed: {v3.failed_rules}"
        assert v4.passed, f"response_contract failed: {v4.failed_rules}"

    def test_stub_audit_record_is_written(self, client: TestClient, monkeypatch):
        """Stub path writes an audit record via the gateway."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from sqlmodel import Session, select
        import backend.app.database as db_module
        from backend.app.models import OpsEvent

        resp = client.post(
            "/api/chat",
            json={"message": "audit stub check"},
            headers=_auth(),
        )
        assert resp.status_code == 200

        with Session(db_module.get_engine()) as s:
            rows = s.exec(
                select(OpsEvent).where(
                    OpsEvent.event_type == "mode_engine.execution_v2.audit"
                )
            ).all()
        assert len(rows) >= 1

    def test_stub_path_stage_0_runs_in_gateway(self):
        """Stage 0 pre-generation constraints are enforced inside the gateway on the
        stub path — an empty user_intent is blocked before AI is ever called."""
        ai_call = MagicMock(return_value="should not be called")
        output, audit = mode_engine_gateway(
            user_intent="",  # empty — stage 0 must block
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        parsed = json.loads(output)
        assert parsed["error"] == "PRE_GENERATION_BLOCKED"
        assert "missing_required_input" in parsed["reason"]
        ai_call.assert_not_called()


# ---------------------------------------------------------------------------
# Req 3: Mode stacking enforces strict priority and conflict resolution
# ---------------------------------------------------------------------------


class TestModeStackingConflictResolution:
    """Verify mode stacking enforces strict priority and conflict resolution."""

    def test_strict_has_highest_priority(self):
        modes = resolve_modes([MODE_BUILDER, MODE_PREDICTION, MODE_STRICT])
        assert modes[0] == MODE_STRICT

    def test_conflict_rules_registered(self):
        """strict_vs_prediction conflict rule is registered."""
        key = frozenset({MODE_STRICT, MODE_PREDICTION})
        assert key in _MODE_CONFLICT_RULES
        rule = _MODE_CONFLICT_RULES[key]
        assert "assumptions_allowed_only_if_flagged" in rule["resolution"]
        assert "insufficient_data_must_be_returned_if_required" in rule["resolution"]

    def test_apply_conflict_resolution_returns_same_list(self):
        modes = [MODE_STRICT, MODE_PREDICTION, MODE_DEBUG]
        result = apply_mode_conflict_resolution(modes)
        assert result == modes

    def test_conflict_constraints_injected_into_prompt(self):
        """strict + prediction → conflict resolution text in injected prompt."""
        prompt = build_mode_system_prompt_injection([MODE_STRICT, MODE_PREDICTION])
        assert "CONFLICT RESOLUTION" in prompt
        assert "assumptions_allowed_only_if_flagged" in prompt

    def test_strict_prevents_guessing_within_prediction_output(self):
        """With strict + prediction, guessing language anywhere in the response fails."""
        output = (
            "I think these are the right assumptions\n"
            "ASSUMPTIONS: some assumptions\n"
            "ALTERNATIVES: option A, option B\n"
            "CONFIDENCE: 0.6\n"
            "MISSING_DATA: none"
        )
        v3 = stage_3_compliance_validation(output, [MODE_STRICT, MODE_PREDICTION])
        assert v3.passed is False
        assert "strict_mode:guessing_detected" in v3.failed_rules

    def test_gateway_applies_conflict_resolution(self):
        """mode_engine_gateway calls apply_mode_conflict_resolution."""
        import backend.app.mode_engine as me

        conflict_calls: list[list[str]] = []
        original = me.apply_mode_conflict_resolution

        def _tracking(modes):
            conflict_calls.append(modes)
            return original(modes)

        # Patch at module level so gateway's call uses the patched version.
        original_fn = me.apply_mode_conflict_resolution
        me.apply_mode_conflict_resolution = _tracking
        try:
            mode_engine_gateway(
                user_intent="test conflict",
                modes=[MODE_STRICT, MODE_PREDICTION],
                ai_call=lambda sp: (
                    "ASSUMPTIONS: A\nALTERNATIVES: B\nCONFIDENCE: 0.5\nMISSING_DATA: none"
                ),
                base_system_prompt="",
            )
        finally:
            me.apply_mode_conflict_resolution = original_fn

        assert len(conflict_calls) == 1
        assert MODE_STRICT in conflict_calls[0]
        assert MODE_PREDICTION in conflict_calls[0]

    def test_mode_priority_all_five_modes(self):
        """Priority order is strict > prediction > debug > audit > builder."""
        from backend.app.mode_engine import MODE_PRIORITY_ORDER

        assert MODE_PRIORITY_ORDER[0] == MODE_STRICT
        assert MODE_PRIORITY_ORDER[1] == MODE_PREDICTION
        assert MODE_PRIORITY_ORDER[2] == MODE_DEBUG
        assert MODE_PRIORITY_ORDER[3] == MODE_AUDIT
        assert MODE_PRIORITY_ORDER[4] == MODE_BUILDER


# ---------------------------------------------------------------------------
# Req 4: Structured validation enforced even if response remains string-based
# ---------------------------------------------------------------------------


class TestResponseContractEnforcement:
    """Structured validation is enforced even though reply stays a plain string."""

    def test_check_response_contract_passes_for_strict_only(self):
        """strict_mode is not a structured mode; free text is allowed."""
        result = _check_response_contract("Any free text response.", [MODE_STRICT])
        assert result.passed is True

    def test_check_response_contract_fails_for_prediction_free_text(self):
        """Prediction mode + pure free text → response contract rejected."""
        result = _check_response_contract("Plain answer with no markers.", [MODE_PREDICTION])
        assert result.passed is False
        assert "response_contract:free_text_in_structured_mode" in result.failed_rules

    def test_check_response_contract_passes_when_any_marker_present(self):
        """Even one structural marker satisfies the response contract guard."""
        result = _check_response_contract(
            "ASSUMPTIONS: some\nOther content.", [MODE_PREDICTION]
        )
        assert result.passed is True

    def test_check_response_contract_fails_for_debug_free_text(self):
        result = _check_response_contract("Just a plain answer.", [MODE_DEBUG])
        assert result.passed is False

    def test_check_response_contract_passes_for_debug_with_step(self):
        result = _check_response_contract("STEP_1: do this first.", [MODE_DEBUG])
        assert result.passed is True

    def test_response_contract_is_4th_stage_in_gateway(self):
        """Gateway runs exactly four validation stages; v4 is response_contract."""
        ai_call = MagicMock(return_value="Clean response.")
        _output, audit = mode_engine_gateway(
            user_intent="test stages",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert len(audit.validation_results) == 4
        stages = [r["stage"] for r in audit.validation_results]
        assert stages == ["structural", "logical", "compliance", "response_contract"]

    def test_partial_prediction_response_rejected(self):
        """A response with SOME but not all prediction markers still fails stage_1."""
        partial = "ASSUMPTIONS: some\nALTERNATIVES: A"  # missing CONFIDENCE and MISSING_DATA
        v1 = stage_1_structural_validation(partial, [MODE_PREDICTION])
        assert v1.passed is False
        assert any("CONFIDENCE:" in m for m in v1.missing_fields)
        assert any("MISSING_DATA:" in m for m in v1.missing_fields)

    def test_structured_failure_string_is_valid_json(self, monkeypatch):
        """When all retries fail, the reply string is valid JSON (structured failure)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        with patch.object(cr, "_call_openai_chat", return_value="plain text no markers"):
            # Using prediction_mode which requires structural markers.
            output, audit = mode_engine_gateway(
                user_intent="test",
                modes=[MODE_PREDICTION],
                ai_call=lambda sp: "plain text no markers",
                base_system_prompt="",
            )

        parsed = json.loads(output)
        assert parsed["error"] == "VALIDATION_FAILED"
        assert isinstance(parsed["failed_rules"], list)


# ---------------------------------------------------------------------------
# Req 5: All AI calls routed exclusively through mode_engine_gateway
# ---------------------------------------------------------------------------


class TestAllAICallsExclusivelyThroughGateway:
    """Confirm all AI calls for POST /api/chat flow exclusively through the gateway."""

    def test_gateway_coverage_constant_declares_post_chat(self):
        """_GATEWAY_COVERAGE explicitly lists POST /api/chat."""
        assert "POST /api/chat" in _GATEWAY_COVERAGE

    def test_stub_path_uses_gateway_exclusively(
        self, client: TestClient, monkeypatch
    ):
        """No API key: only the gateway produces the reply; no direct AI call bypass."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        gateway_returns = ["GATEWAY_CONTROLLED_RESPONSE"]

        def _mock_gateway(**kwargs):
            return gateway_returns[0], ModeEngineAuditRecord(
                user_intent=kwargs["user_intent"]
            )

        monkeypatch.setattr(cr, "mode_engine_gateway", _mock_gateway)

        resp = client.post(
            "/api/chat",
            json={"message": "test gateway exclusive"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "GATEWAY_CONTROLLED_RESPONSE"

    def test_openai_path_uses_gateway_exclusively(
        self, client: TestClient, monkeypatch
    ):
        """API key present: only the gateway produces the reply; _call_openai_chat
        is never called outside the gateway-managed ai_call closure."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        gateway_called = {"n": 0}

        def _mock_gateway(**kwargs):
            gateway_called["n"] += 1
            return "GATEWAY_LIVE_RESPONSE", ModeEngineAuditRecord(
                user_intent=kwargs["user_intent"]
            )

        monkeypatch.setattr(cr, "mode_engine_gateway", _mock_gateway)

        # Ensure _call_openai_chat is never reached directly outside the gateway.
        direct_call_made = {"flag": False}

        def _detect_direct_call(*args, **kwargs):
            direct_call_made["flag"] = True
            return "DIRECT_CALL"

        monkeypatch.setattr(cr, "_call_openai_chat", _detect_direct_call)

        resp = client.post(
            "/api/chat",
            json={"message": "live gateway test"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "GATEWAY_LIVE_RESPONSE"
        assert gateway_called["n"] == 1
        # _call_openai_chat must NOT have been called directly from the handler —
        # it is only reachable via the gateway's ai_call closure.
        assert direct_call_made["flag"] is False

    def test_intent_endpoint_not_in_gateway_coverage(self):
        """POST /api/chat/intent is governed by INTERACTION_LAYER_V2, not this gateway."""
        assert "POST /api/chat/intent" not in _GATEWAY_COVERAGE

