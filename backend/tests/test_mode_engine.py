"""
Tests for MODE_ENGINE_EXECUTION_V2 under the dual-mode architecture.

Supported execution paths:
- NORMAL mode: no active modes, no validation, no prompt injection
- AGOII mode: strict_mode only, with validation and prompt injection
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mode_engine")

from backend.app.main import app  # noqa: E402
from backend.app.mode_engine import (  # noqa: E402
    _GATEWAY_COVERAGE,
    MAX_RETRIES,
    MODE_STRICT,
    ModeEngineAuditRecord,
    ValidationResult,
    _build_feedback_prompt,
    _build_structured_failure,
    _check_response_contract,
    _persist_audit_record,
    build_mode_system_prompt_injection,
    effective_mode,
    mode_engine_gateway,
    resolve_modes,
    stage_0_pre_generation_constraints,
    stage_1_structural_validation,
    stage_2_logical_validation,
    stage_3_compliance_validation,
)
from backend.tests.test_utils import _chat_payload  # noqa: E402

TOKEN = "test-secret-key"
EXPECTED_VALIDATION_STAGES = 4


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestResolveModes:
    def test_unknown_modes_filtered_to_empty(self):
        assert resolve_modes(["unknown_mode"]) == []

    def test_empty_list_stays_empty(self):
        assert resolve_modes([]) == []

    def test_deduplication_keeps_single_strict_mode(self):
        result = resolve_modes([MODE_STRICT, MODE_STRICT])
        assert result == [MODE_STRICT]

    def test_strict_mode_is_preserved(self):
        assert resolve_modes([MODE_STRICT]) == [MODE_STRICT]


class TestEffectiveMode:
    def test_strict_mode_selected(self):
        assert effective_mode([MODE_STRICT]) == MODE_STRICT

    def test_empty_returns_none(self):
        assert effective_mode([]) is None


class TestBuildModeSystemPromptInjection:
    def test_strict_mode_injection(self):
        prompt = build_mode_system_prompt_injection([MODE_STRICT])
        assert "STRICT MODE" in prompt
        assert "INSUFFICIENT_DATA" in prompt
        assert "Active modes:" in prompt
        assert MODE_STRICT in prompt


class TestStage0:
    def test_empty_input_blocked_in_strict_mode(self):
        ok, reason = stage_0_pre_generation_constraints("", [MODE_STRICT])
        assert ok is False
        assert "missing_required_input" in reason

    def test_whitespace_only_blocked_in_strict_mode(self):
        ok, _reason = stage_0_pre_generation_constraints("   ", [MODE_STRICT])
        assert ok is False

    def test_valid_input_passes_in_strict_mode(self):
        ok, reason = stage_0_pre_generation_constraints("Hello world", [MODE_STRICT])
        assert ok is True
        assert reason == ""

    def test_empty_modes_skip_stage_0(self):
        ok, reason = stage_0_pre_generation_constraints("", [])
        assert ok is True
        assert reason == ""


class TestStage1StructuralValidation:
    def test_strict_mode_empty_output_fails(self):
        result = stage_1_structural_validation("", [MODE_STRICT])
        assert result.passed is False
        assert "strict_mode:empty_output" in result.failed_rules

    def test_strict_mode_structured_output_passes(self):
        result = stage_1_structural_validation(
            "ARTIFACT_SUMMARY: Valid response.", [MODE_STRICT]
        )
        assert result.passed is True

    def test_strict_mode_free_text_fails_structural_contract(self):
        result = stage_1_structural_validation("Valid response.", [MODE_STRICT])
        assert result.passed is False
        assert "strict_mode:structured_output_required" in result.failed_rules

    def test_empty_modes_skip_validation(self):
        result = stage_1_structural_validation("", [])
        assert result.passed is True
        assert result.failed_rules == []


class TestStage2LogicalValidation:
    def test_strict_mode_valid_artifact_output_passes(self):
        result = stage_2_logical_validation("ARTIFACT_SUMMARY: any output", [MODE_STRICT])
        assert result.passed is True

    def test_strict_mode_empty_artifact_value_fails(self):
        result = stage_2_logical_validation("ARTIFACT_SUMMARY:   ", [MODE_STRICT])
        assert result.passed is False
        assert "strict_mode:malformed_artifact_section" in result.failed_rules

    def test_empty_modes_skip_validation(self):
        result = stage_2_logical_validation("I think this works", [])
        assert result.passed is True
        assert result.failed_rules == []


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
        result = stage_3_compliance_validation(
            "I think this might work. INSUFFICIENT_DATA: no context provided.",
            [MODE_STRICT],
        )
        assert result.passed is True

    def test_empty_modes_skip_validation(self):
        result = stage_3_compliance_validation("I think this might work.", [])
        assert result.passed is True
        assert result.failed_rules == []


class TestResponseContractEnforcement:
    def test_check_response_contract_rejects_free_text_for_strict_only(self):
        result = _check_response_contract("Any free text response.", [MODE_STRICT])
        assert result.passed is False
        assert "response_contract:free_text_in_strict_mode" in result.failed_rules
        assert result.correction_instructions

    def test_check_response_contract_passes_for_structured_strict_output(self):
        result = _check_response_contract(
            "ARTIFACT_SUMMARY: structured response.", [MODE_STRICT]
        )
        assert result.passed is True

    def test_check_response_contract_detects_mixed_output(self):
        result = _check_response_contract(
            "ARTIFACT_SUMMARY: structured response.\nExtra free text.",
            [MODE_STRICT],
        )
        assert result.passed is False
        assert (
            "response_contract:mixed_free_text_and_structured_output"
            in result.failed_rules
        )

    def test_check_response_contract_passes_for_empty_modes(self):
        result = _check_response_contract("Any free text response.", [])
        assert result.passed is True


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
        assert result["correction_instructions"] == ["fix it"]
        assert result["retry_count"] == 2


class TestDualModeInvariantLock:
    def test_normal_mode_skips_validation_and_returns_free_text(self):
        ai_call = MagicMock(return_value="free text")

        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[],
            ai_call=ai_call,
            base_system_prompt="BASE",
        )

        assert output == "free text"
        assert audit.final_output == "free text"
        assert audit.validation_results == []
        assert audit.retry_count == 0
        ai_call.assert_called_once_with("BASE")

    def test_agoii_mode_free_text_triggers_structured_failure(self):
        ai_call = MagicMock(return_value="free text")

        output, _audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )

        failure = json.loads(output)
        assert failure["error"] == "VALIDATION_FAILED"
        assert failure["failed_rules"]
        assert failure["correction_instructions"]
        assert ai_call.call_count == MAX_RETRIES + 1

    def test_agoii_mode_structured_response_passes_without_failure(self):
        ai_call = MagicMock(return_value="ARTIFACT_SUMMARY: valid structured response")

        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )

        assert output == "ARTIFACT_SUMMARY: valid structured response"
        assert audit.final_output == output
        assert len(audit.validation_results) == EXPECTED_VALIDATION_STAGES
        assert all(result["passed"] for result in audit.validation_results)
        ai_call.assert_called_once()

    def test_no_mode_leakage_between_normal_and_agoii_paths(self):
        assert resolve_modes([]) == []
        assert resolve_modes([MODE_STRICT]) == [MODE_STRICT]

    def test_structured_failure_format_is_valid_json_with_required_fields(self):
        ai_call = MagicMock(return_value="free text")

        output, _audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )

        failure = json.loads(output)
        assert isinstance(failure, dict)
        assert "failed_rules" in failure
        assert "correction_instructions" in failure
        assert isinstance(failure["failed_rules"], list)
        assert isinstance(failure["correction_instructions"], list)


class TestModeEngineGateway:
    def test_valid_strict_output_passes_through(self):
        ai_call = MagicMock(return_value="ARTIFACT_SUMMARY: A valid response.")
        output, audit = mode_engine_gateway(
            user_intent="Hello",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="System prompt.",
        )
        assert output == "ARTIFACT_SUMMARY: A valid response."
        assert audit.final_output == "ARTIFACT_SUMMARY: A valid response."
        assert audit.retry_count == 0
        assert len(audit.validation_results) == EXPECTED_VALIDATION_STAGES
        ai_call.assert_called_once()

    def test_empty_user_intent_returns_pre_generation_blocked(self):
        ai_call = MagicMock(return_value="irrelevant")
        output, _audit = mode_engine_gateway(
            user_intent="",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        failure = json.loads(output)
        assert failure["error"] == "PRE_GENERATION_BLOCKED"
        ai_call.assert_not_called()

    def test_mode_constraints_injected_into_prompt_for_strict_mode(self):
        received_prompts: list[str] = []

        def ai_call(system_prompt: str) -> str:
            received_prompts.append(system_prompt)
            return "ARTIFACT_SUMMARY: Response."

        mode_engine_gateway(
            user_intent="test",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="BASE",
        )
        assert len(received_prompts) == 1
        assert "MODE ENGINE EXECUTION V2 CONSTRAINTS" in received_prompts[0]
        assert "BASE" in received_prompts[0]

    def test_empty_modes_do_not_inject_or_validate(self):
        received_prompts: list[str] = []

        def ai_call(system_prompt: str) -> str:
            received_prompts.append(system_prompt)
            return "I think maybe probably this is right."

        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[],
            ai_call=ai_call,
            base_system_prompt="BASE",
        )
        assert output == "I think maybe probably this is right."
        assert received_prompts == ["BASE"]
        assert audit.transformed_prompt == "BASE"
        assert audit.validation_results == []
        assert audit.retry_count == 0

    def test_retry_on_validation_failure_in_strict_mode(self):
        call_count = {"n": 0}

        def ai_call(system_prompt: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "I think this is correct."
            return "ARTIFACT_SUMMARY: This is definitely correct."

        output, audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        assert call_count["n"] == 2
        assert output == "ARTIFACT_SUMMARY: This is definitely correct."
        assert audit.retry_count == 1

    def test_structured_failure_after_exhausted_retries_in_strict_mode(self):
        ai_call = MagicMock(return_value="I think maybe probably this is right.")
        output, _audit = mode_engine_gateway(
            user_intent="question",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        failure = json.loads(output)
        assert failure["error"] == "VALIDATION_FAILED"
        assert failure["correction_instructions"]
        assert failure["retry_count"] == MAX_RETRIES
        assert ai_call.call_count == MAX_RETRIES + 1

    def test_audit_record_fields_populated_in_strict_mode(self):
        ai_call = MagicMock(return_value="ARTIFACT_SUMMARY: Clean response.")
        output, audit = mode_engine_gateway(
            user_intent="user query",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="base",
        )
        assert audit.user_intent == "user query"
        assert MODE_STRICT in audit.selected_modes
        assert "MODE ENGINE" in audit.transformed_prompt
        assert audit.raw_ai_output == "ARTIFACT_SUMMARY: Clean response."
        assert output == "ARTIFACT_SUMMARY: Clean response."
        assert len(audit.validation_results) == EXPECTED_VALIDATION_STAGES


class TestChatEndpointModeEngine:
    def test_default_request_uses_normal_mode(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        captured: dict[str, object] = {}

        def _gateway(**kwargs):
            captured["modes"] = kwargs["modes"]
            return "ok", ModeEngineAuditRecord(user_intent=kwargs["user_intent"])

        monkeypatch.setattr(cr, "mode_engine_gateway", _gateway)
        resp = client.post("/api/chat", json=_chat_payload("Hello"), headers=_auth())
        assert resp.status_code == 200
        assert captured["modes"] == []

    def test_agent_mode_enables_strict_mode(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        captured: dict[str, object] = {}

        def _gateway(**kwargs):
            captured["modes"] = kwargs["modes"]
            return "ok", ModeEngineAuditRecord(user_intent=kwargs["user_intent"])

        monkeypatch.setattr(cr, "mode_engine_gateway", _gateway)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", agent_mode=True),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert captured["modes"] == [MODE_STRICT]

    def test_modes_field_is_ignored_without_agent_mode(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        captured: dict[str, object] = {}

        def _gateway(**kwargs):
            captured["modes"] = kwargs["modes"]
            return "ok", ModeEngineAuditRecord(user_intent=kwargs["user_intent"])

        monkeypatch.setattr(cr, "mode_engine_gateway", _gateway)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", modes=[MODE_STRICT]),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert captured["modes"] == []

    def test_modes_field_rejected_non_list(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat",
            json=_chat_payload("Hello", modes="strict_mode"),
            headers=_auth(),
        )
        assert resp.status_code == 422


class TestMandatoryAudit:
    def test_persist_audit_record_writes_to_db(self, tmp_path):
        from sqlmodel import Session, select

        import backend.app.database as db_module
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
        from sqlmodel import Session

        import backend.app.database as db_module

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
        non_raising_client = TestClient(app, raise_server_exceptions=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.mode_engine as me

        def _fail_persist(rec):
            raise RuntimeError("AUDIT_LOG_FAILURE: simulated")

        monkeypatch.setattr(me, "_persist_audit_record", _fail_persist)

        resp = non_raising_client.post(
            "/api/chat",
            json=_chat_payload("Hello"),
            headers=_auth(),
        )
        assert resp.status_code == 500

    def test_audit_record_no_silent_fallback_when_db_configured(
        self, monkeypatch, tmp_path
    ):
        from sqlmodel import Session

        import backend.app.database as db_module

        db_path = tmp_path / "audit_no_fallback.db"
        db_module.reset_engine(f"sqlite:///{db_path}")
        db_module.init_db()

        write_called = {"n": 0}

        def _counting_commit(self):
            write_called["n"] += 1
            raise Exception("forced DB error")

        monkeypatch.setattr(Session, "commit", _counting_commit)

        record = ModeEngineAuditRecord(user_intent="no fallback test")
        with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
            _persist_audit_record(record)

        assert write_called["n"] >= 1
        db_module.reset_engine()


class TestStubPathThroughGateway:
    def test_stub_path_calls_gateway(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        gateway_calls: list[dict[str, object]] = []
        original_gw = cr.mode_engine_gateway

        def _tracking_gateway(**kwargs):
            gateway_calls.append(
                {"user_intent": kwargs.get("user_intent"), "modes": kwargs.get("modes")}
            )
            return original_gw(**kwargs)

        monkeypatch.setattr(cr, "mode_engine_gateway", _tracking_gateway)

        resp = client.post(
            "/api/chat",
            json=_chat_payload("Stub gateway test"),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert len(gateway_calls) == 1
        assert gateway_calls[0]["user_intent"] == "Stub gateway test"
        assert gateway_calls[0]["modes"] == []

    def test_stub_fails_strict_mode_validation(self):
        from backend.app.chat_routes import _stub_reply

        stub = _stub_reply("hello")
        v1 = stage_1_structural_validation(stub, [MODE_STRICT])
        v2 = stage_2_logical_validation(stub, [MODE_STRICT])
        v3 = stage_3_compliance_validation(stub, [MODE_STRICT])
        v4 = _check_response_contract(stub, [MODE_STRICT])

        assert v1.passed is False
        assert v2.passed is True
        assert v3.passed is True
        assert v4.passed is False

    def test_stub_audit_record_is_written(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from sqlmodel import Session, select

        import backend.app.database as db_module
        from backend.app.models import OpsEvent

        resp = client.post(
            "/api/chat",
            json=_chat_payload("audit stub check"),
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

    def test_stub_path_stage_0_runs_in_gateway_for_strict_mode(self):
        ai_call = MagicMock(return_value="should not be called")
        output, _audit = mode_engine_gateway(
            user_intent="",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        parsed = json.loads(output)
        assert parsed["error"] == "PRE_GENERATION_BLOCKED"
        assert "missing_required_input" in parsed["reason"]
        ai_call.assert_not_called()


class TestAllAICallsExclusivelyThroughGateway:
    def test_gateway_coverage_constant_declares_post_chat(self):
        assert "POST /api/chat" in _GATEWAY_COVERAGE

    def test_stub_path_uses_gateway_exclusively(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        import backend.app.chat_routes as cr

        def _mock_gateway(**kwargs):
            return "GATEWAY_CONTROLLED_RESPONSE", ModeEngineAuditRecord(
                user_intent=kwargs["user_intent"]
            )

        monkeypatch.setattr(cr, "mode_engine_gateway", _mock_gateway)

        resp = client.post(
            "/api/chat",
            json=_chat_payload("test gateway exclusive"),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "GATEWAY_CONTROLLED_RESPONSE"

    def test_openai_path_uses_gateway_exclusively(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        import backend.app.chat_routes as cr

        gateway_called = {"n": 0}

        def _mock_gateway(**kwargs):
            gateway_called["n"] += 1
            return "GATEWAY_LIVE_RESPONSE", ModeEngineAuditRecord(
                user_intent=kwargs["user_intent"]
            )

        monkeypatch.setattr(cr, "mode_engine_gateway", _mock_gateway)

        direct_call_made = {"flag": False}

        def _detect_direct_call(*args, **kwargs):
            direct_call_made["flag"] = True
            return "DIRECT_CALL"

        monkeypatch.setattr(cr, "_call_openai_chat", _detect_direct_call)

        resp = client.post(
            "/api/chat",
            json=_chat_payload("live gateway test"),
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "GATEWAY_LIVE_RESPONSE"
        assert gateway_called["n"] == 1
        assert direct_call_made["flag"] is False

    def test_intent_endpoint_not_in_gateway_coverage(self):
        assert "POST /api/chat/intent" not in _GATEWAY_COVERAGE
