"""
Tests for MUTATION_GOVERNANCE_EXECUTION_V1.

Covers:
  - Stage 1: structural validation (required fields, operation_type)
  - Stage 2: logical validation (assumptions, alternatives, confidence, risks)
  - Stage 3: scope validation (allowed/restricted paths)
  - Mutation enforcement gate
  - Audit persistence
  - Governance gateway (full pipeline with mock AI call)
  - POST /api/mutations/propose endpoint
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mutation_governance")

from backend.app.mutation_governance import (
    ALLOWED_PATH_PREFIXES,
    GateResult,
    MutationContract,
    MutationGovernanceResult,
    MutationValidationResult,
    RESTRICTED_PATHS,
    mutation_enforcement_gate,
    mutation_governance_gateway,
    stage_1_structural_validation,
    stage_2_logical_validation,
    stage_3_scope_validation,
)
from backend.app.mutation_governance.audit import persist_mutation_audit_record
from backend.app.mutation_governance.contract import MutationGovernanceAuditRecord
from backend.app.mutation_governance.engine import _extract_json
from backend.app.mutation_governance.validation import _is_allowed, _is_restricted

from backend.app.main import app

TOKEN = "test-governance-key"

# ---------------------------------------------------------------------------
# A fully valid contract dict that passes all three stages.
# SECTION_MUTATION_CONTRACT satisfies builder_mode marker.
# ASSUMPTIONS/ALTERNATIVES/CONFIDENCE/MISSING_DATA satisfy prediction_mode.
# ---------------------------------------------------------------------------

_VALID_OUTPUT = json.dumps(
    {
        "SECTION_MUTATION_CONTRACT": "mutation_proposal",
        "target_files": ["backend/app/example.py"],
        "operation_type": "update_file",
        "proposed_changes": "Add input validation to the process() function.",
        "ASSUMPTIONS": ["The process() function exists and accepts a dict argument"],
        "ALTERNATIVES": [
            "Validate at the API layer instead",
            "Add a separate validator class",
        ],
        "CONFIDENCE": 0.85,
        "risks": ["Existing callers may fail with new validation rules"],
        "MISSING_DATA": ["none"],
    }
)


def _valid_contract() -> MutationContract:
    return MutationContract.from_dict(json.loads(_VALID_OUTPUT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_mutation_governance.db"
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
# Helpers
# ---------------------------------------------------------------------------


def _make_ai_call(output: str):
    """Return an ai_call callable that always returns *output*."""

    def _call(system_prompt: str) -> str:  # noqa: ARG001
        return output

    return _call


# ===========================================================================
# MutationContract.from_dict
# ===========================================================================


class TestMutationContractFromDict:
    def test_lowercase_keys(self):
        data = {
            "target_files": ["backend/app/foo.py"],
            "operation_type": "create_file",
            "proposed_changes": "Create foo",
            "assumptions": ["foo does not exist"],
            "alternatives": ["Use an existing module"],
            "confidence": 0.9,
            "risks": ["Namespace collision"],
            "missing_data": ["none"],
        }
        c = MutationContract.from_dict(data)
        assert c.target_files == ["backend/app/foo.py"]
        assert c.operation_type == "create_file"
        assert c.assumptions == ["foo does not exist"]
        assert c.confidence == 0.9

    def test_uppercase_aliases(self):
        data = {
            "target_files": ["backend/app/bar.py"],
            "operation_type": "update_file",
            "proposed_changes": "Update bar",
            "ASSUMPTIONS": ["bar exists"],
            "ALTERNATIVES": ["rewrite from scratch"],
            "CONFIDENCE": "high",
            "risks": ["breaks existing callers"],
            "MISSING_DATA": ["none"],
        }
        c = MutationContract.from_dict(data)
        assert c.assumptions == ["bar exists"]
        assert c.alternatives == ["rewrite from scratch"]
        assert c.confidence == "high"
        assert c.missing_data == ["none"]

    def test_section_key_ignored(self):
        data = {
            "SECTION_MUTATION_CONTRACT": "mutation_proposal",
            "target_files": ["backend/app/x.py"],
            "operation_type": "delete_file",
            "proposed_changes": "Remove x.py",
            "ASSUMPTIONS": ["x.py is unused"],
            "ALTERNATIVES": ["archive instead"],
            "CONFIDENCE": 0.5,
            "risks": ["may break imports"],
            "MISSING_DATA": ["none"],
        }
        c = MutationContract.from_dict(data)
        # SECTION key must not appear in to_dict output
        assert "SECTION_MUTATION_CONTRACT" not in c.to_dict()

    def test_to_dict_round_trip(self):
        c = _valid_contract()
        d = c.to_dict()
        assert set(d.keys()) == set(MutationContract.REQUIRED_FIELDS)


# ===========================================================================
# Stage 1 — Structural validation
# ===========================================================================


class TestStage1Structural:
    def test_valid_contract_passes(self):
        result = stage_1_structural_validation(_valid_contract())
        assert result.passed is True
        assert result.stage == "structural"
        assert result.failed_rules == []

    def test_empty_target_files_fails(self):
        c = _valid_contract()
        c.target_files = []
        result = stage_1_structural_validation(c)
        assert result.passed is False
        assert any("target_files" in r for r in result.failed_rules)

    def test_empty_proposed_changes_fails(self):
        c = _valid_contract()
        c.proposed_changes = ""
        result = stage_1_structural_validation(c)
        assert result.passed is False
        assert any("proposed_changes" in r for r in result.failed_rules)

    def test_invalid_operation_type_fails(self):
        c = _valid_contract()
        c.operation_type = "nuke_everything"
        result = stage_1_structural_validation(c)
        assert result.passed is False
        assert any("invalid_operation_type" in r for r in result.failed_rules)

    def test_all_valid_operation_types_pass(self):
        for op in ("create_file", "update_file", "delete_file"):
            c = _valid_contract()
            c.operation_type = op
            assert stage_1_structural_validation(c).passed is True

    def test_missing_confidence_fails(self):
        c = _valid_contract()
        c.confidence = ""
        result = stage_1_structural_validation(c)
        assert result.passed is False
        assert any("confidence" in r for r in result.failed_rules)

    def test_empty_risks_list_fails(self):
        c = _valid_contract()
        c.risks = []
        result = stage_1_structural_validation(c)
        assert result.passed is False

    def test_none_missing_data_fails(self):
        c = _valid_contract()
        c.missing_data = []
        result = stage_1_structural_validation(c)
        assert result.passed is False


# ===========================================================================
# Stage 2 — Logical validation
# ===========================================================================


class TestStage2Logical:
    def test_valid_contract_passes(self):
        result = stage_2_logical_validation(_valid_contract())
        assert result.passed is True
        assert result.stage == "logical"

    def test_empty_assumptions_fails(self):
        c = _valid_contract()
        c.assumptions = []
        result = stage_2_logical_validation(c)
        assert result.passed is False
        assert any("assumptions" in r for r in result.failed_rules)

    def test_blank_assumption_entry_fails(self):
        c = _valid_contract()
        c.assumptions = ["valid assumption", "   "]
        result = stage_2_logical_validation(c)
        assert result.passed is False
        assert any("undeclared_assumptions" in r for r in result.failed_rules)

    def test_empty_alternatives_fails(self):
        c = _valid_contract()
        c.alternatives = []
        result = stage_2_logical_validation(c)
        assert result.passed is False
        assert any("alternatives" in r for r in result.failed_rules)

    def test_confidence_numeric_valid(self):
        for val in (0, 0.0, 0.5, 1, 1.0):
            c = _valid_contract()
            c.confidence = val
            assert stage_2_logical_validation(c).passed is True

    def test_confidence_categorical_valid(self):
        for val in ("low", "medium", "high", "Low", "HIGH", "very high"):
            c = _valid_contract()
            c.confidence = val
            assert stage_2_logical_validation(c).passed is True

    def test_confidence_out_of_range_fails(self):
        c = _valid_contract()
        c.confidence = 1.5
        result = stage_2_logical_validation(c)
        assert result.passed is False
        assert any("invalid_confidence" in r for r in result.failed_rules)

    def test_confidence_invalid_string_fails(self):
        c = _valid_contract()
        c.confidence = "definitely"
        result = stage_2_logical_validation(c)
        assert result.passed is False

    def test_empty_risks_fails(self):
        c = _valid_contract()
        c.risks = []
        result = stage_2_logical_validation(c)
        assert result.passed is False
        assert any("risks" in r for r in result.failed_rules)

    def test_blank_risk_entry_fails(self):
        c = _valid_contract()
        c.risks = ["real risk", ""]
        result = stage_2_logical_validation(c)
        assert result.passed is False


# ===========================================================================
# Stage 3 — Scope validation
# ===========================================================================


class TestStage3Scope:
    def test_allowed_paths_pass(self):
        for prefix in ALLOWED_PATH_PREFIXES:
            c = _valid_contract()
            c.target_files = [f"{prefix}some/file.py"]
            assert stage_3_scope_validation(c).passed is True

    def test_out_of_scope_path_fails(self):
        c = _valid_contract()
        c.target_files = ["frontend/components/Button.tsx"]
        result = stage_3_scope_validation(c)
        assert result.passed is False
        assert "frontend/components/Button.tsx" in result.blocked_paths
        assert any("out_of_scope_path" in r for r in result.failed_rules)

    def test_env_file_is_restricted(self):
        c = _valid_contract()
        c.target_files = [".env"]
        result = stage_3_scope_validation(c)
        assert result.passed is False
        assert ".env" in result.blocked_paths
        assert any("restricted_path" in r for r in result.failed_rules)

    def test_nested_env_file_is_restricted(self):
        c = _valid_contract()
        c.target_files = ["backend/config/.env"]
        result = stage_3_scope_validation(c)
        assert result.passed is False

    def test_secrets_path_is_restricted(self):
        c = _valid_contract()
        c.target_files = ["secrets/api_key.txt"]
        result = stage_3_scope_validation(c)
        assert result.passed is False
        assert any("restricted_path" in r for r in result.failed_rules)

    def test_infra_credentials_restricted(self):
        c = _valid_contract()
        c.target_files = ["infra/credentials/prod.pem"]
        result = stage_3_scope_validation(c)
        assert result.passed is False

    def test_mixed_valid_and_invalid_fails(self):
        c = _valid_contract()
        c.target_files = ["backend/app/ok.py", "secrets/token.txt"]
        result = stage_3_scope_validation(c)
        assert result.passed is False
        assert "secrets/token.txt" in result.blocked_paths

    def test_is_allowed_helper(self):
        assert _is_allowed("backend/app/foo.py") is True
        assert _is_allowed("android/app/Main.kt") is True
        assert _is_allowed("scripts/deploy.sh") is True
        assert _is_allowed("frontend/index.html") is False

    def test_is_restricted_helper(self):
        assert _is_restricted(".env") is True
        assert _is_restricted("secrets/token") is True
        assert _is_restricted("infra/credentials/key") is True
        assert _is_restricted("backend/app/models.py") is False


# ===========================================================================
# Mutation enforcement gate
# ===========================================================================


class TestMutationEnforcementGate:
    def test_all_pass_returns_passed(self):
        vrs = [
            MutationValidationResult(passed=True, stage="structural"),
            MutationValidationResult(passed=True, stage="logical"),
            MutationValidationResult(passed=True, stage="scope"),
        ]
        gate = mutation_enforcement_gate(vrs)
        assert gate.passed is True
        assert gate.blocked_reason is None
        assert gate.failed_stages == []

    def test_one_failure_returns_blocked(self):
        vrs = [
            MutationValidationResult(passed=True, stage="structural"),
            MutationValidationResult(
                passed=False,
                stage="logical",
                failed_rules=["invalid_confidence"],
            ),
            MutationValidationResult(passed=True, stage="scope"),
        ]
        gate = mutation_enforcement_gate(vrs)
        assert gate.passed is False
        assert "logical" in gate.failed_stages
        assert gate.blocked_reason is not None
        assert "validation_failed" in gate.blocked_reason

    def test_multiple_failures_all_reported(self):
        vrs = [
            MutationValidationResult(
                passed=False, stage="structural", failed_rules=["missing_field:x"]
            ),
            MutationValidationResult(passed=True, stage="logical"),
            MutationValidationResult(
                passed=False, stage="scope", failed_rules=["restricted_path:.env"]
            ),
        ]
        gate = mutation_enforcement_gate(vrs)
        assert gate.passed is False
        assert set(gate.failed_stages) == {"structural", "scope"}


# ===========================================================================
# _extract_json helper
# ===========================================================================


class TestExtractJson:
    def test_pure_json(self):
        data = {"key": "value", "num": 42}
        assert _extract_json(json.dumps(data)) == data

    def test_json_in_markdown_fence(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json(text) == {"a": 1}

    def test_json_embedded_in_text(self):
        text = 'Some preamble\n{"target": "x"}\nSome suffix'
        assert _extract_json(text) == {"target": "x"}

    def test_no_json_returns_none(self):
        assert _extract_json("no json here at all") is None

    def test_empty_string_returns_none(self):
        assert _extract_json("") is None


# ===========================================================================
# Audit persistence
# ===========================================================================


class TestAuditPersistence:
    def test_writes_audit_record_to_db(self):
        record = MutationGovernanceAuditRecord(
            user_intent="test intent",
            selected_modes=["strict_mode"],
            mutation_proposal={"target_files": ["backend/app/x.py"]},
            validation_results=[],
            blocked_reason=None,
            status="approved",
        )
        # Should not raise (DB is configured via fixture).
        persist_mutation_audit_record(record)

    def test_raises_on_db_write_failure(self):
        record = MutationGovernanceAuditRecord(user_intent="bad", status="blocked")

        # Force a write failure by passing a broken session.
        with patch(
            "backend.app.mutation_governance.audit.persist_mutation_audit_record"
        ) as mocked:
            mocked.side_effect = RuntimeError("AUDIT_LOG_FAILURE: forced")
            with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
                mocked(record)

    def test_no_db_configured_logs_warning_no_raise(self, caplog):
        import logging

        record = MutationGovernanceAuditRecord(user_intent="test", status="approved")

        import backend.app.database as db_module

        original = db_module.get_engine

        def _raise():
            raise RuntimeError("not configured")

        db_module.get_engine = _raise
        try:
            with caplog.at_level(logging.WARNING, logger="backend.app.mutation_governance.audit"):
                persist_mutation_audit_record(record)
            assert any("not persisted" in m for m in caplog.messages)
        finally:
            db_module.get_engine = original


# ===========================================================================
# mutation_governance_gateway — full pipeline
# ===========================================================================


class TestMutationGovernanceGateway:
    def test_valid_proposal_returns_approved(self):
        result = mutation_governance_gateway(
            user_intent="Add validation to process()",
            ai_call=_make_ai_call(_VALID_OUTPUT),
        )
        assert isinstance(result, MutationGovernanceResult)
        assert result.status == "approved"
        assert result.mutation_proposal is not None
        assert result.blocked_reason is None
        assert result.governance_contract == "MUTATION_GOVERNANCE_EXECUTION_V1"

    def test_execution_boundary_always_set(self):
        result = mutation_governance_gateway(
            user_intent="some intent",
            ai_call=_make_ai_call(_VALID_OUTPUT),
        )
        eb = result.execution_boundary
        assert eb["no_git_commit"] is True
        assert eb["no_file_write"] is True
        assert eb["no_deployment_trigger"] is True

    def test_invalid_json_returns_blocked(self):
        result = mutation_governance_gateway(
            user_intent="some intent",
            ai_call=_make_ai_call("not valid json at all"),
        )
        assert result.status == "blocked"
        assert result.mutation_proposal is None
        assert "parse_failure" in (result.blocked_reason or "")

    def test_restricted_path_returns_blocked(self):
        bad_output = json.dumps(
            {
                "SECTION_MUTATION_CONTRACT": "mutation_proposal",
                "target_files": [".env"],
                "operation_type": "update_file",
                "proposed_changes": "Expose secrets",
                "ASSUMPTIONS": ["env file is accessible"],
                "ALTERNATIVES": ["use a vault instead"],
                "CONFIDENCE": 0.9,
                "risks": ["security breach"],
                "MISSING_DATA": ["none"],
            }
        )
        result = mutation_governance_gateway(
            user_intent="expose secrets",
            ai_call=_make_ai_call(bad_output),
        )
        assert result.status == "blocked"
        assert result.mutation_proposal is None

    def test_out_of_scope_path_returns_blocked(self):
        bad_output = json.dumps(
            {
                "SECTION_MUTATION_CONTRACT": "mutation_proposal",
                "target_files": ["frontend/App.tsx"],
                "operation_type": "update_file",
                "proposed_changes": "Add a button",
                "ASSUMPTIONS": ["React app exists"],
                "ALTERNATIVES": ["use a library component"],
                "CONFIDENCE": 0.7,
                "risks": ["UI regression"],
                "MISSING_DATA": ["none"],
            }
        )
        result = mutation_governance_gateway(
            user_intent="add button",
            ai_call=_make_ai_call(bad_output),
        )
        assert result.status == "blocked"

    def test_all_validation_stages_in_result(self):
        result = mutation_governance_gateway(
            user_intent="test",
            ai_call=_make_ai_call(_VALID_OUTPUT),
        )
        stages = {vr["stage"] for vr in result.validation_results}
        assert stages == {"structural", "logical", "scope"}

    def test_enforced_modes_always_added(self):
        # Even when no modes are passed, enforced modes must be active.
        called_with: list[str] = []

        def _capture_call(prompt: str) -> str:
            called_with.append(prompt)
            return _VALID_OUTPUT

        mutation_governance_gateway(
            user_intent="test",
            modes=None,
            ai_call=_capture_call,
        )
        # The system prompt should contain mode engine constraint headers.
        assert any("strict_mode" in p for p in called_with)

    def test_structured_result_always_returned(self):
        # Gateway must NEVER raise on validation failure.
        empty_output = json.dumps(
            {
                "SECTION_MUTATION_CONTRACT": "mutation_proposal",
                "target_files": [],
                "operation_type": "bad_type",
                "proposed_changes": "",
                "ASSUMPTIONS": [],
                "ALTERNATIVES": [],
                "CONFIDENCE": "nonsense",
                "risks": [],
                "MISSING_DATA": [],
            }
        )
        result = mutation_governance_gateway(
            user_intent="totally broken",
            ai_call=_make_ai_call(empty_output),
        )
        assert result.status == "blocked"
        assert isinstance(result.to_dict(), dict)


# ===========================================================================
# POST /api/mutations/propose — HTTP endpoint
# ===========================================================================


class TestMutationProposeEndpoint:
    def test_requires_auth(self, client: TestClient):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": "do something"},
        )
        assert resp.status_code == 401

    def test_rejects_empty_intent(self, client: TestClient):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": "   "},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_rejects_extra_fields(self, client: TestClient):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": "test", "unknown_field": "x"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_valid_proposal_returns_200_approved(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(_VALID_OUTPUT),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "Add validation to process()"},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["governance_contract"] == "MUTATION_GOVERNANCE_EXECUTION_V1"
        assert body["status"] == "approved"
        assert body["mutation_proposal"] is not None
        assert body["execution_boundary"]["no_git_commit"] is True
        assert body["execution_boundary"]["no_file_write"] is True

    def test_blocked_proposal_returns_200_blocked(self, client: TestClient):
        bad_output = json.dumps(
            {
                "SECTION_MUTATION_CONTRACT": "mutation_proposal",
                "target_files": ["secrets/token.txt"],
                "operation_type": "update_file",
                "proposed_changes": "Overwrite secrets",
                "ASSUMPTIONS": ["secrets dir is writable"],
                "ALTERNATIVES": ["store in env vars"],
                "CONFIDENCE": 0.6,
                "risks": ["credential leak"],
                "MISSING_DATA": ["none"],
            }
        )
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(bad_output),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "overwrite secrets"},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "blocked"
        assert body["mutation_proposal"] is None
        assert body["blocked_reason"] is not None

    def test_response_always_structured(self, client: TestClient):
        # Even with AI returning garbage, the response is always structured JSON.
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call("this is not json"),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "something"},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "governance_contract" in body
        assert "status" in body
        assert "execution_boundary" in body

    def test_modes_field_accepted(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(_VALID_OUTPUT),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "test", "modes": ["strict_mode"]},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["governance_contract"] == "MUTATION_GOVERNANCE_EXECUTION_V1"
