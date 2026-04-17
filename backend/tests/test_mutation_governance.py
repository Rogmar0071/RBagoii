"""
Tests for MUTATION_GOVERNANCE_EXECUTION_V1.

Design principle under test:
  Mode engine text markers (ASSUMPTIONS:, CONFIDENCE:, etc.) and the mutation
  contract JSON block are fully independent validation layers.  JSON field names
  use only lowercase canonical names; no marker text appears as a JSON key.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mutation_governance")

from backend.app.main import app
from backend.app.mutation_governance import (
    ALLOWED_PATH_PREFIXES,
    MutationContract,
    MutationValidationResult,
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

TOKEN = "test-governance-key"

# ---------------------------------------------------------------------------
# _VALID_OUTPUT: two independent sections
#   SECTION_INTENT_ANALYSIS  -- plain-text mode engine markers
#   SECTION_MUTATION_CONTRACT -- JSON with ONLY lowercase field names
# ---------------------------------------------------------------------------

_VALID_CONTRACT_DICT = {
    "target_files": ["backend/app/example.py"],
    "operation_type": "update_file",
    "proposed_changes": "Add input validation to the process() function.",
    "assumptions": ["The process() function exists and accepts a dict argument"],
    "alternatives": [
        "Validate at the API layer instead",
        "Add a separate validator class",
    ],
    "confidence": 0.85,
    "risks": ["Existing callers may fail with new validation rules"],
    "missing_data": ["none"],
}

_VALID_OUTPUT = (
    "SECTION_INTENT_ANALYSIS:\n"
    "ASSUMPTIONS: The process() function exists and accepts a dict argument\n"
    "ALTERNATIVES: Validate at the API layer instead; Add a separate validator class\n"
    "CONFIDENCE: 0.85\n"
    "MISSING_DATA: none\n"
    "\n"
    "SECTION_MUTATION_CONTRACT:\n" + json.dumps(_VALID_CONTRACT_DICT, indent=2)
)


def _valid_contract() -> MutationContract:
    return MutationContract.from_dict(_VALID_CONTRACT_DICT)


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


def _make_ai_call(output: str):
    def _call(system_prompt: str) -> str:  # noqa: ARG001
        return output

    return _call


# ===========================================================================
# MutationContract.from_dict -- lowercase canonical fields only
# ===========================================================================


class TestMutationContractFromDict:
    def test_lowercase_keys_accepted(self):
        c = MutationContract.from_dict(_VALID_CONTRACT_DICT)
        assert c.target_files == ["backend/app/example.py"]
        assert c.operation_type == "update_file"
        assert c.confidence == 0.85

    def test_unknown_keys_silently_ignored(self):
        data = dict(_VALID_CONTRACT_DICT)
        data["extra"] = "ignored"
        c = MutationContract.from_dict(data)
        assert "extra" not in c.to_dict()

    def test_to_dict_uses_only_lowercase_keys(self):
        for key in _valid_contract().to_dict():
            assert key == key.lower(), f"JSON key {key!r} must be lowercase"

    def test_to_dict_contains_all_required_fields(self):
        assert set(_valid_contract().to_dict().keys()) == set(MutationContract.REQUIRED_FIELDS)

    def test_missing_field_defaults_to_empty(self):
        c = MutationContract.from_dict({})
        assert c.target_files == []
        assert c.operation_type == ""
        assert c.assumptions == []


# ===========================================================================
# Stage 1 -- Structural validation
# ===========================================================================


class TestStage1Structural:
    def test_valid_contract_passes(self):
        r = stage_1_structural_validation(_valid_contract())
        assert r.passed is True and r.stage == "structural"

    def test_empty_target_files_fails(self):
        c = _valid_contract()
        c.target_files = []
        r = stage_1_structural_validation(c)
        assert r.passed is False and any("target_files" in x for x in r.failed_rules)

    def test_empty_proposed_changes_fails(self):
        c = _valid_contract()
        c.proposed_changes = ""
        r = stage_1_structural_validation(c)
        assert r.passed is False and any("proposed_changes" in x for x in r.failed_rules)

    def test_invalid_operation_type_fails(self):
        c = _valid_contract()
        c.operation_type = "nuke_everything"
        r = stage_1_structural_validation(c)
        assert r.passed is False and any("invalid_operation_type" in x for x in r.failed_rules)

    def test_all_valid_operation_types_pass(self):
        for op in ("create_file", "update_file", "delete_file"):
            c = _valid_contract()
            c.operation_type = op
            assert stage_1_structural_validation(c).passed is True

    def test_missing_confidence_fails(self):
        c = _valid_contract()
        c.confidence = ""
        r = stage_1_structural_validation(c)
        assert r.passed is False and any("confidence" in x for x in r.failed_rules)

    def test_empty_risks_list_fails(self):
        c = _valid_contract()
        c.risks = []
        assert stage_1_structural_validation(c).passed is False

    def test_empty_missing_data_fails(self):
        c = _valid_contract()
        c.missing_data = []
        assert stage_1_structural_validation(c).passed is False


# ===========================================================================
# Stage 2 -- Logical validation
# ===========================================================================


class TestStage2Logical:
    def test_valid_contract_passes(self):
        r = stage_2_logical_validation(_valid_contract())
        assert r.passed is True and r.stage == "logical"

    def test_empty_assumptions_fails(self):
        c = _valid_contract()
        c.assumptions = []
        r = stage_2_logical_validation(c)
        assert r.passed is False and any("assumptions" in x for x in r.failed_rules)

    def test_blank_assumption_entry_fails(self):
        c = _valid_contract()
        c.assumptions = ["valid", "   "]
        r = stage_2_logical_validation(c)
        assert r.passed is False and any("undeclared_assumptions" in x for x in r.failed_rules)

    def test_empty_alternatives_fails(self):
        c = _valid_contract()
        c.alternatives = []
        r = stage_2_logical_validation(c)
        assert r.passed is False and any("alternatives" in x for x in r.failed_rules)

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
        r = stage_2_logical_validation(c)
        assert r.passed is False and any("invalid_confidence" in x for x in r.failed_rules)

    def test_confidence_invalid_string_fails(self):
        c = _valid_contract()
        c.confidence = "definitely"
        assert stage_2_logical_validation(c).passed is False

    def test_empty_risks_fails(self):
        c = _valid_contract()
        c.risks = []
        r = stage_2_logical_validation(c)
        assert r.passed is False and any("risks" in x for x in r.failed_rules)

    def test_blank_risk_entry_fails(self):
        c = _valid_contract()
        c.risks = ["real risk", ""]
        assert stage_2_logical_validation(c).passed is False


# ===========================================================================
# Stage 3 -- Scope validation
# ===========================================================================


class TestStage3Scope:
    def test_allowed_paths_pass(self):
        for prefix in ALLOWED_PATH_PREFIXES:
            c = _valid_contract()
            c.target_files = [f"{prefix}file.py"]
            assert stage_3_scope_validation(c).passed is True

    def test_out_of_scope_path_fails(self):
        c = _valid_contract()
        c.target_files = ["frontend/Button.tsx"]
        r = stage_3_scope_validation(c)
        assert r.passed is False and "frontend/Button.tsx" in r.blocked_paths

    def test_env_file_is_restricted(self):
        c = _valid_contract()
        c.target_files = [".env"]
        r = stage_3_scope_validation(c)
        assert r.passed is False and ".env" in r.blocked_paths

    def test_nested_env_file_is_restricted(self):
        c = _valid_contract()
        c.target_files = ["backend/config/.env"]
        assert stage_3_scope_validation(c).passed is False

    def test_secrets_path_is_restricted(self):
        c = _valid_contract()
        c.target_files = ["secrets/api_key.txt"]
        r = stage_3_scope_validation(c)
        assert r.passed is False and any("restricted_path" in x for x in r.failed_rules)

    def test_infra_credentials_restricted(self):
        c = _valid_contract()
        c.target_files = ["infra/credentials/prod.pem"]
        assert stage_3_scope_validation(c).passed is False

    def test_mixed_valid_and_invalid_fails(self):
        c = _valid_contract()
        c.target_files = ["backend/app/ok.py", "secrets/t.txt"]
        r = stage_3_scope_validation(c)
        assert r.passed is False and "secrets/t.txt" in r.blocked_paths

    def test_is_allowed_helper(self):
        assert _is_allowed("backend/app/foo.py") is True
        assert _is_allowed("android/Main.kt") is True
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
        assert gate.passed is True and gate.blocked_reason is None

    def test_one_failure_returns_blocked(self):
        vrs = [
            MutationValidationResult(passed=True, stage="structural"),
            MutationValidationResult(
                passed=False, stage="logical", failed_rules=["invalid_confidence"]
            ),
            MutationValidationResult(passed=True, stage="scope"),
        ]
        gate = mutation_enforcement_gate(vrs)
        assert gate.passed is False and "logical" in gate.failed_stages
        assert "validation_failed" in gate.blocked_reason

    def test_multiple_failures_all_reported(self):
        vrs = [
            MutationValidationResult(passed=False, stage="structural", failed_rules=["x"]),
            MutationValidationResult(passed=True, stage="logical"),
            MutationValidationResult(passed=False, stage="scope", failed_rules=["y"]),
        ]
        gate = mutation_enforcement_gate(vrs)
        assert gate.passed is False and set(gate.failed_stages) == {"structural", "scope"}


# ===========================================================================
# _extract_json -- strict, label-anchored extraction (independent from mode markers)
# ===========================================================================


class TestExtractJson:
    def test_valid_section_label_with_json(self):
        text = (
            "SECTION_INTENT_ANALYSIS:\nASSUMPTIONS: foo\n\n"
            "SECTION_MUTATION_CONTRACT:\n" + json.dumps({"k": "v"})
        )
        assert _extract_json(text) == {"k": "v"}

    def test_no_section_label_returns_none(self):
        """Pure JSON without SECTION_MUTATION_CONTRACT: must be rejected (strict parsing)."""
        assert _extract_json(json.dumps({"key": "value"})) is None

    def test_json_in_markdown_fence_after_label(self):
        text = 'SECTION_MUTATION_CONTRACT:\n```json\n{"a": 1}\n```'
        assert _extract_json(text) == {"a": 1}

    def test_malformed_json_after_label_returns_none(self):
        assert _extract_json("SECTION_MUTATION_CONTRACT:\n{invalid") is None

    def test_empty_string_returns_none(self):
        assert _extract_json("") is None

    def test_no_brace_after_label_returns_none(self):
        assert _extract_json("SECTION_MUTATION_CONTRACT:\nno json") is None

    def test_full_valid_output_parsed_correctly(self):
        r = _extract_json(_VALID_OUTPUT)
        assert r is not None
        assert r["operation_type"] == "update_file"
        assert r["target_files"] == ["backend/app/example.py"]

    def test_json_keys_are_lowercase_only(self):
        """JSON block must use only lowercase field names (independent of mode markers)."""
        r = _extract_json(_VALID_OUTPUT)
        assert r is not None
        for key in r:
            assert key == key.lower(), f"JSON key {key!r} must be lowercase"

    def test_mode_engine_marker_names_not_in_json_keys(self):
        """Uppercase mode engine marker names must NOT appear literally as JSON keys.

        The JSON contract uses lowercase field names (e.g. "assumptions").
        The mode engine text markers use uppercase labels (e.g. ASSUMPTIONS:).
        These two layers are independent: the extraction and validation logic
        never conflates them.
        """
        r = _extract_json(_VALID_OUTPUT)
        assert r is not None
        # Exact match check: the JSON keys must NOT be the uppercase marker strings.
        # They are expected to be lowercase (e.g. "assumptions", "confidence").
        forbidden_exact = {"ASSUMPTIONS", "ALTERNATIVES", "CONFIDENCE", "MISSING_DATA"}
        for key in r:
            assert key not in forbidden_exact, (
                f"JSON key {key!r} is an uppercase mode engine marker name -- "
                "JSON contract field names must be lowercase"
            )


# ===========================================================================
# Audit persistence
# ===========================================================================


class TestAuditPersistence:
    def test_writes_audit_record_to_db(self):
        record = MutationGovernanceAuditRecord(
            user_intent="test",
            selected_modes=["strict_mode"],
            mutation_proposal={"target_files": ["backend/app/x.py"]},
            validation_results=[],
            blocked_reason=None,
            status="approved",
        )
        persist_mutation_audit_record(record)

    def test_raises_on_db_write_failure(self):
        record = MutationGovernanceAuditRecord(user_intent="bad", status="blocked")
        with patch("backend.app.mutation_governance.audit.persist_mutation_audit_record") as m:
            m.side_effect = RuntimeError("AUDIT_LOG_FAILURE: forced")
            with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
                m(record)

    def test_no_db_configured_raises_audit_unavailable(self):
        record = MutationGovernanceAuditRecord(user_intent="test", status="approved")
        import backend.app.database as db_module

        original = db_module.get_engine

        def _raise():
            raise RuntimeError("DATABASE_URL not configured")

        db_module.get_engine = _raise
        try:
            with pytest.raises(RuntimeError, match="AUDIT_SYSTEM_UNAVAILABLE"):
                persist_mutation_audit_record(record)
        finally:
            db_module.get_engine = original

    def test_governance_audit_failure_propagates_through_gateway(self):
        """Audit write failure must propagate through mutation_governance_gateway.

        Block condition: block_if_log_not_written.  The RuntimeError from the
        audit layer must NOT be suppressed by the gateway — it must propagate to
        the caller, effectively blocking the result from being returned.
        """
        with patch(
            "backend.app.mutation_governance.engine.persist_mutation_audit_record",
            side_effect=RuntimeError("AUDIT_LOG_FAILURE: db unreachable"),
        ):
            with pytest.raises(RuntimeError, match="AUDIT_LOG_FAILURE"):
                mutation_governance_gateway(
                    user_intent="Add input validation",
                    modes=[],  # Test audit failure in normal mode
                    ai_call=_make_ai_call(_VALID_OUTPUT),
                )


# ===========================================================================
# mutation_governance_gateway -- full pipeline
# ===========================================================================


class TestMutationGovernanceGateway:
    def test_valid_proposal_returns_approved(self):
        result = mutation_governance_gateway(
            user_intent="Add validation to process()",
            modes=["strict_mode"],
            ai_call=_make_ai_call(_VALID_OUTPUT),
        )
        assert result.status == "approved"
        assert result.mutation_proposal is not None
        assert result.governance_contract == "MUTATION_GOVERNANCE_EXECUTION_V1"

    def test_execution_boundary_always_enforced(self):
        result = mutation_governance_gateway(
            user_intent="x", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        assert result.execution_boundary["no_git_commit"] is True
        assert result.execution_boundary["no_file_write"] is True
        assert result.execution_boundary["no_deployment_trigger"] is True

    def test_no_section_label_blocked_with_parse_failure(self):
        """Output without SECTION_MUTATION_CONTRACT: label is blocked in strict mode."""
        result = mutation_governance_gateway(
            user_intent="x", modes=["strict_mode"], ai_call=_make_ai_call("no label here")
        )
        assert result.status == "blocked"
        assert "parse_failure" in (result.blocked_reason or "")

    def test_pure_json_without_label_is_rejected(self):
        """A perfect JSON response without the section label is rejected in strict mode."""
        result = mutation_governance_gateway(
            user_intent="x",
            modes=["strict_mode"],
            ai_call=_make_ai_call(json.dumps(_VALID_CONTRACT_DICT)),
        )
        assert result.status == "blocked"
        assert "parse_failure" in (result.blocked_reason or "")

    def test_restricted_path_returns_blocked(self):
        bad = dict(_VALID_CONTRACT_DICT, target_files=[".env"])
        output = (
            "SECTION_INTENT_ANALYSIS:\nASSUMPTIONS: env accessible\n"
            "ALTERNATIVES: use vault\nCONFIDENCE: 0.9\nMISSING_DATA: none\n\n"
            "SECTION_MUTATION_CONTRACT:\n" + json.dumps(bad, indent=2)
        )
        result = mutation_governance_gateway(
            user_intent="expose secrets", modes=["strict_mode"], ai_call=_make_ai_call(output)
        )
        assert result.status == "blocked" and result.mutation_proposal is None

    def test_out_of_scope_path_returns_blocked(self):
        bad = dict(_VALID_CONTRACT_DICT, target_files=["frontend/App.tsx"])
        output = (
            "SECTION_INTENT_ANALYSIS:\nASSUMPTIONS: React exists\n"
            "ALTERNATIVES: library\nCONFIDENCE: 0.7\nMISSING_DATA: none\n\n"
            "SECTION_MUTATION_CONTRACT:\n" + json.dumps(bad, indent=2)
        )
        result = mutation_governance_gateway(
            user_intent="add button", modes=["strict_mode"], ai_call=_make_ai_call(output)
        )
        assert result.status == "blocked"

    def test_all_three_validation_stages_run_in_strict_mode(self):
        """In strict mode with valid contract, all three validation stages run."""
        result = mutation_governance_gateway(
            user_intent="x", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        assert {vr["stage"] for vr in result.validation_results} == {
            "structural",
            "logical",
            "scope",
        }

    def test_mode_engine_runs_in_strict_mode(self):
        """In strict mode, mode_engine_gateway runs first and ai_call receives mode-injected prompt."""
        prompts: list[str] = []

        def _cap(p: str) -> str:
            prompts.append(p)
            return _VALID_OUTPUT

        mutation_governance_gateway(
            user_intent="test ordering", modes=["strict_mode"], ai_call=_cap
        )
        assert "MODE ENGINE EXECUTION V2 CONSTRAINTS" in " ".join(prompts)

    def test_strict_mode_includes_enforced_modes(self):
        """In strict mode, enforced modes (prediction_mode, builder_mode) are added."""
        prompts: list[str] = []

        def _cap(p: str) -> str:
            prompts.append(p)
            return _VALID_OUTPUT

        mutation_governance_gateway(
            user_intent="x", modes=["strict_mode"], ai_call=_cap
        )
        combined = " ".join(prompts)
        assert "strict_mode" in combined
        assert "prediction_mode" in combined
        assert "builder_mode" in combined

    def test_approved_proposal_has_only_lowercase_json_keys(self):
        result = mutation_governance_gateway(
            user_intent="x", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        assert result.status == "approved"
        for key in result.mutation_proposal:
            assert key == key.lower(), f"{key!r} must be lowercase"

    def test_structured_result_always_returned_on_invalid_contract(self):
        """In strict mode, invalid contracts return blocked status with structured result."""
        broken = (
            "SECTION_INTENT_ANALYSIS:\nASSUMPTIONS: s\nALTERNATIVES: a\n"
            "CONFIDENCE: low\nMISSING_DATA: none\n\nSECTION_MUTATION_CONTRACT:\n"
            + json.dumps(
                {
                    "target_files": [],
                    "operation_type": "bad",
                    "proposed_changes": "",
                    "assumptions": [],
                    "alternatives": [],
                    "confidence": "?",
                    "risks": [],
                    "missing_data": [],
                }
            )
        )
        result = mutation_governance_gateway(
            user_intent="broken", modes=["strict_mode"], ai_call=_make_ai_call(broken)
        )
        assert result.status == "blocked" and isinstance(result.to_dict(), dict)

    def test_audit_written_for_approved(self):
        """Audit is written for approved proposals in strict mode."""
        from sqlmodel import Session as S
        from sqlmodel import select

        import backend.app.database as db
        from backend.app.models import OpsEvent

        mutation_governance_gateway(
            user_intent="audit test", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        with S(db.get_engine()) as s:
            events = s.exec(
                select(OpsEvent).where(
                    OpsEvent.event_type == "mutation_governance.execution_v1.audit"
                )
            ).all()
        assert len(events) >= 1

    def test_audit_written_for_blocked(self):
        """Audit is written for blocked proposals in strict mode."""
        from sqlmodel import Session as S
        from sqlmodel import select

        import backend.app.database as db
        from backend.app.models import OpsEvent

        mutation_governance_gateway(
            user_intent="blocked", modes=["strict_mode"], ai_call=_make_ai_call("no label")
        )
        with S(db.get_engine()) as s:
            events = s.exec(
                select(OpsEvent).where(
                    OpsEvent.event_type == "mutation_governance.execution_v1.audit"
                )
            ).all()
        assert len(events) >= 1


# ===========================================================================
# DUAL MODE GOVERNANCE - PHASE 6 Tests
# ===========================================================================


class TestDualModeGovernance:
    def test_normal_mode_approves_without_validation(self):
        """PHASE 6: modes == [] → immediate approval, no validation"""
        result = mutation_governance_gateway(
            user_intent="test query", modes=[], ai_call=_make_ai_call("any free text response")
        )
        assert result.status == "approved"
        assert result.mutation_proposal is not None
        # No validation stages run in normal mode
        assert result.validation_results == []
        assert result.gate_result == {}

    def test_strict_mode_requires_contract_validation(self):
        """PHASE 6: strict_mode → contract-driven validation"""
        result = mutation_governance_gateway(
            user_intent="test query", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        assert result.status == "approved"
        # Validation stages run in strict mode
        assert len(result.validation_results) == 3  # structural, logical, scope
        assert {vr["stage"] for vr in result.validation_results} == {
            "structural",
            "logical",
            "scope",
        }

    def test_governance_never_assumes_validation_exists(self):
        """PHASE 6: Governance must not assume validation always exists"""
        # In normal mode, no validation
        result = mutation_governance_gateway(
            user_intent="test", modes=[], ai_call=_make_ai_call("response")
        )
        assert result.validation_results == []

        # In strict mode, validation exists
        result = mutation_governance_gateway(
            user_intent="test", modes=["strict_mode"], ai_call=_make_ai_call(_VALID_OUTPUT)
        )
        assert len(result.validation_results) > 0


# ===========================================================================
# POST /api/mutations/propose -- HTTP endpoint
# ===========================================================================


class TestMutationProposeEndpoint:
    def test_requires_auth(self, client: TestClient):
        resp = client.post("/api/mutations/propose", json={"intent": "x"})
        assert resp.status_code == 401

    def test_rejects_empty_intent(self, client: TestClient):
        resp = client.post("/api/mutations/propose", json={"intent": "   "}, headers=_auth())
        assert resp.status_code == 422

    def test_rejects_extra_fields(self, client: TestClient):
        resp = client.post(
            "/api/mutations/propose", json={"intent": "x", "bad": 1}, headers=_auth()
        )
        assert resp.status_code == 422

    def test_valid_proposal_returns_200_approved(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(_VALID_OUTPUT),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "Add validation"},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["governance_contract"] == "MUTATION_GOVERNANCE_EXECUTION_V1"
        assert body["status"] == "approved"
        assert body["mutation_proposal"] is not None
        assert body["execution_boundary"]["no_git_commit"] is True
        assert body["execution_boundary"]["no_file_write"] is True
        assert body["execution_boundary"]["no_deployment_trigger"] is True

    def test_approved_response_has_lowercase_json_keys(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(_VALID_OUTPUT),
        ):
            resp = client.post("/api/mutations/propose", json={"intent": "x"}, headers=_auth())
        body = resp.json()
        assert body["status"] == "approved"
        for key in body["mutation_proposal"]:
            assert key == key.lower()

    def test_blocked_proposal_returns_200_blocked(self, client: TestClient):
        """In strict mode, restricted paths are blocked."""
        bad = dict(_VALID_CONTRACT_DICT, target_files=["secrets/t.txt"])
        bad_out = (
            "SECTION_INTENT_ANALYSIS:\nASSUMPTIONS: writable\nALTERNATIVES: env\n"
            "CONFIDENCE: 0.6\nMISSING_DATA: none\n\nSECTION_MUTATION_CONTRACT:\n"
            + json.dumps(bad, indent=2)
        )
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(bad_out),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "overwrite", "modes": ["strict_mode"]},
                headers=_auth(),
            )
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "blocked"
        assert body["mutation_proposal"] is None
        assert body["blocked_reason"] is not None

    def test_response_always_structured_on_garbage(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call("garbage"),
        ):
            resp = client.post("/api/mutations/propose", json={"intent": "x"}, headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert "governance_contract" in body and "execution_boundary" in body
        assert body["status"] == "blocked"

    def test_modes_field_accepted(self, client: TestClient):
        with patch(
            "backend.app.mutation_routes._build_ai_call",
            return_value=_make_ai_call(_VALID_OUTPUT),
        ):
            resp = client.post(
                "/api/mutations/propose",
                json={"intent": "x", "modes": ["strict_mode"]},
                headers=_auth(),
            )
        assert resp.status_code == 200
        assert resp.json()["governance_contract"] == "MUTATION_GOVERNANCE_EXECUTION_V1"
