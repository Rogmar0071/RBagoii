"""
Tests for MUTATION_SIMULATION_EXECUTION_V1.

Coverage:
  - dependency_surface_mapping: direct, indirect, incomplete
  - impact_analysis: structural, behavioral, data-flow
  - failure_prediction: failure types, alternative scenarios
  - risk_scoring: low / medium / high criteria
  - simulation_decision_gate: all blocking rules + override protocol
  - simulation_gateway: full pipeline (approved / blocked / invalid input)
  - API endpoint: POST /api/mutations/simulate
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mutation_simulation")

from backend.app.mutation_simulation import (
    FAILURE_BUILD,
    FAILURE_CONTRACT_VIOLATION,
    FAILURE_DEPENDENCY_BREAK,
    FAILURE_RUNTIME,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    DependencySurface,
    FailurePrediction,
    ImpactAnalysis,
    PredictedFailure,
    RiskScore,
    SimulationOverride,
    SimulationResult,
    analyze_impact,
    map_dependency_surface,
    predict_failures,
    score_risk,
    simulation_decision_gate,
    simulation_gateway,
)
from backend.app.mutation_simulation.gate import SimulationGateResult
from backend.app.main import app

TOKEN = "test-simulation-key"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_APPROVED_CONTRACT: dict[str, Any] = {
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

_APPROVED_GOVERNANCE_RESULT: dict[str, Any] = {
    "contract_id": "test-contract-id-001",
    "governance_contract": "MUTATION_GOVERNANCE_EXECUTION_V1",
    "status": "approved",
    "mutation_proposal": _APPROVED_CONTRACT,
    "validation_results": [],
    "gate_result": {"passed": True},
    "blocked_reason": None,
    "audit_id": "test-audit-id-001",
    "created_at": "2026-01-01T00:00:00+00:00",
    "execution_boundary": {
        "no_git_commit": True,
        "no_file_write": True,
        "no_deployment_trigger": True,
    },
}

_MULTI_MODULE_CONTRACT: dict[str, Any] = {
    "target_files": [
        "backend/app/main.py",
        "backend/app/models.py",
        "backend/app/mutation_governance/engine.py",
    ],
    "operation_type": "update_file",
    "proposed_changes": "Add a new field to models and update main.py routing.",
    "assumptions": ["All target files exist"],
    "alternatives": ["Add a migration instead"],
    "confidence": 0.7,
    "risks": [
        "Callers of main.py may break",
        "Database schema change may cause runtime errors",
    ],
    "missing_data": ["none"],
}

_DELETE_CONTRACT: dict[str, Any] = {
    "target_files": ["backend/app/legacy_module.py"],
    "operation_type": "delete_file",
    "proposed_changes": "Remove the legacy_module.py file entirely.",
    "assumptions": ["No active callers of legacy_module.py"],
    "alternatives": ["Deprecate the module instead of deleting"],
    "confidence": 0.6,
    "risks": [
        "Any remaining callers will crash at import time",
        "Build may fail if the file is referenced in __init__.py",
    ],
    "missing_data": ["none"],
}


def _approved_governance(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_id": "test-id",
        "governance_contract": "MUTATION_GOVERNANCE_EXECUTION_V1",
        "status": "approved",
        "mutation_proposal": contract,
        "validation_results": [],
        "gate_result": {"passed": True},
        "blocked_reason": None,
        "audit_id": "test-audit",
        "created_at": "2026-01-01T00:00:00+00:00",
        "execution_boundary": {
            "no_git_commit": True,
            "no_file_write": True,
            "no_deployment_trigger": True,
        },
    }


# ---------------------------------------------------------------------------
# dependency_surface_mapping
# ---------------------------------------------------------------------------


class TestDependencySurfaceMapping:
    def test_single_file_returns_complete_surface(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        assert surface.complete is True
        assert "backend/app/example.py" in surface.impacted_files

    def test_module_inferred_from_file_path(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        # backend/app/example.py → app.example
        assert any("example" in m for m in surface.impacted_modules)

    def test_known_file_resolves_direct_dependencies(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/models.py"])
        surface = map_dependency_surface(contract)
        assert surface.complete is True
        # models.py depends on database.py
        assert "backend/app/database.py" in surface.impacted_files
        direct_links = [l for l in surface.dependency_links if l["type"] == "direct"]
        assert any(l["target"] == "backend/app/database.py" for l in direct_links)

    def test_indirect_dependencies_resolved(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/main.py"])
        surface = map_dependency_surface(contract)
        # main.py → models.py → database.py (indirect)
        indirect_links = [l for l in surface.dependency_links if l["type"] == "indirect"]
        assert len(indirect_links) > 0

    def test_empty_target_files_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=[])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert surface.incomplete_reason is not None
        assert "dependency_graph_unavailable" in surface.incomplete_reason

    def test_multiple_target_files(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        assert surface.complete is True
        assert len(surface.impacted_files) >= 3
        assert len(surface.impacted_modules) >= 3


# ---------------------------------------------------------------------------
# impact_analysis
# ---------------------------------------------------------------------------


class TestImpactAnalysis:
    def test_update_file_structural_impact(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        assert any("existing_file_modified" in s for s in impact.structural_impact)

    def test_delete_file_structural_impact(self):
        surface = map_dependency_surface(_DELETE_CONTRACT)
        impact = analyze_impact(_DELETE_CONTRACT, surface)
        assert any("callers_may_break" in s for s in impact.structural_impact)

    def test_behavioral_impact_detected_from_proposed_changes(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        # "validation" keyword → validation_or_guard_affected
        assert any("validation" in b for b in impact.behavioral_impact)

    def test_cross_module_data_flow_detected(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        # Expect some cross-module data flow
        combined = " ".join(impact.data_flow_impact)
        assert "dependency" in combined or "data_flow" in combined

    def test_all_impact_categories_populated(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        assert isinstance(impact.structural_impact, list)
        assert isinstance(impact.behavioral_impact, list)
        assert isinstance(impact.data_flow_impact, list)
        # All must be non-empty
        assert impact.structural_impact
        assert impact.behavioral_impact
        assert impact.data_flow_impact


# ---------------------------------------------------------------------------
# failure_prediction
# ---------------------------------------------------------------------------


class TestFailurePrediction:
    def test_risks_produce_runtime_or_contract_failures(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        failures = predict_failures(_APPROVED_CONTRACT, impact, surface)
        types = failures.failure_types
        # "callers may fail" → runtime
        assert FAILURE_RUNTIME in types

    def test_delete_produces_build_failure(self):
        surface = map_dependency_surface(_DELETE_CONTRACT)
        impact = analyze_impact(_DELETE_CONTRACT, surface)
        failures = predict_failures(_DELETE_CONTRACT, impact, surface)
        assert FAILURE_BUILD in failures.failure_types

    def test_dependency_links_produce_dependency_break(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/models.py"])
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        assert FAILURE_DEPENDENCY_BREAK in failures.failure_types

    def test_alternative_scenario_present_when_risk_exists(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        failures = predict_failures(_APPROVED_CONTRACT, impact, surface)
        # At least one failure with an alternative scenario since risks exist
        with_alt = [f for f in failures.predicted_failures if f.alternative_scenario]
        assert len(with_alt) >= 1

    def test_missing_data_produces_contract_violation(self):
        contract = dict(
            _APPROVED_CONTRACT,
            missing_data=["database schema version unknown"],
        )
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        assert FAILURE_CONTRACT_VIOLATION in failures.failure_types

    def test_always_at_least_one_predicted_failure(self):
        contract = dict(
            _APPROVED_CONTRACT,
            risks=["none"],
            missing_data=["none"],
            target_files=["backend/app/new_module.py"],
            operation_type="create_file",
        )
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        assert len(failures.predicted_failures) >= 1


# ---------------------------------------------------------------------------
# risk_scoring
# ---------------------------------------------------------------------------


class TestRiskScoring:
    def test_low_risk_isolated_change(self):
        contract = dict(
            _APPROVED_CONTRACT,
            risks=["none"],
            missing_data=["none"],
            target_files=["backend/app/new_module.py"],
        )
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        risk = score_risk(surface, impact, failures)
        # Isolated file with no known deps — should be low or medium at most
        assert risk.level in (RISK_LOW, RISK_MEDIUM)

    def test_high_risk_cross_module_impact(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH

    def test_high_risk_delete_file(self):
        surface = map_dependency_surface(_DELETE_CONTRACT)
        impact = analyze_impact(_DELETE_CONTRACT, surface)
        failures = predict_failures(_DELETE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH

    def test_high_risk_incomplete_surface(self):
        surface = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        impact = ImpactAnalysis(
            structural_impact=["existing_file_modified"],
            behavioral_impact=["no_significant_behavioral_change_detected"],
            data_flow_impact=["no_cross_module_data_flow_impact"],
        )
        failures = FailurePrediction(
            predicted_failures=[
                PredictedFailure(
                    failure_type=FAILURE_RUNTIME,
                    description="low risk",
                    severity="low",
                )
            ],
            failure_types=[FAILURE_RUNTIME],
        )
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        assert any("unknown_dependencies" in c for c in risk.criteria_matched)

    def test_medium_risk_limited_deps(self):
        # 2 modules but not 3+
        contract = dict(
            _APPROVED_CONTRACT,
            target_files=["backend/app/models.py"],
        )
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level in (RISK_MEDIUM, RISK_HIGH)

    def test_risk_score_criteria_populated(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert isinstance(risk.criteria_matched, list)
        assert len(risk.criteria_matched) > 0


# ---------------------------------------------------------------------------
# simulation_decision_gate
# ---------------------------------------------------------------------------


class TestSimulationDecisionGate:
    def _low_risk_passing(self) -> tuple[RiskScore, FailurePrediction, DependencySurface]:
        contract = dict(
            _APPROVED_CONTRACT,
            risks=["none"],
            missing_data=["none"],
            target_files=["backend/app/new_module.py"],
        )
        surface = map_dependency_surface(contract)
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        risk = score_risk(surface, impact, failures)
        return risk, failures, surface

    def test_safe_to_execute_for_low_risk(self):
        risk, failures, surface = self._low_risk_passing()
        if risk.level == RISK_LOW:
            gate = simulation_decision_gate(risk, failures, surface)
            assert gate.safe_to_execute is True
            assert gate.blocked_reason is None

    def test_blocked_if_incomplete_dependency_analysis(self):
        incomplete = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        risk = RiskScore(level=RISK_LOW, criteria_matched=["isolated_change"])
        failures = FailurePrediction(
            predicted_failures=[
                PredictedFailure(FAILURE_RUNTIME, "test", "low")
            ],
            failure_types=[FAILURE_RUNTIME],
        )
        gate = simulation_decision_gate(risk, failures, incomplete)
        assert gate.safe_to_execute is False
        assert "dependency_analysis_incomplete" in gate.blocked_reason

    def test_blocked_high_risk_without_override(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        gate = simulation_decision_gate(risk, failures, surface, override=None)
        assert gate.safe_to_execute is False
        assert "risk_level_high_no_override" in gate.blocked_reason

    def test_override_allows_high_risk(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        override = SimulationOverride(
            justification="Approved by senior engineer for emergency release",
            accepted_risks=["cross_module_impact", "structural_changes"],
        )
        gate = simulation_decision_gate(risk, failures, surface, override=override)
        assert gate.safe_to_execute is True
        assert gate.override_used is True

    def test_incomplete_surface_not_overridable(self):
        """Rule 1 (incomplete dependency analysis) must never be overridable."""
        incomplete = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        risk = RiskScore(level=RISK_HIGH, criteria_matched=["unknown_dependencies"])
        failures = FailurePrediction(
            predicted_failures=[PredictedFailure(FAILURE_RUNTIME, "test", "low")],
            failure_types=[FAILURE_RUNTIME],
        )
        override = SimulationOverride(
            justification="I accept all risks",
            accepted_risks=["all"],
        )
        gate = simulation_decision_gate(risk, failures, incomplete, override=override)
        assert gate.safe_to_execute is False
        assert "dependency_analysis_incomplete" in gate.blocked_reason

    def test_high_severity_failures_block_without_override(self):
        surface = map_dependency_surface(_DELETE_CONTRACT)
        impact = analyze_impact(_DELETE_CONTRACT, surface)
        failures = predict_failures(_DELETE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        # No override — gate should block on high-severity failure (or high risk)
        gate = simulation_decision_gate(risk, failures, surface, override=None)
        assert gate.safe_to_execute is False


# ---------------------------------------------------------------------------
# simulation_gateway (full pipeline)
# ---------------------------------------------------------------------------


class TestSimulationGateway:
    def test_blocked_contract_not_validated(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, status="blocked")
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert "mutation_contract_not_validated" in result.blocked_reason

    def test_pending_contract_blocked(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, status="pending")
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False

    def test_missing_mutation_proposal_blocked(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, mutation_proposal=None)
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False

    def test_approved_single_file_returns_result(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert isinstance(result, SimulationResult)
        assert result.risk_level in (RISK_LOW, RISK_MEDIUM, RISK_HIGH)
        assert isinstance(result.impacted_files, list)
        assert isinstance(result.predicted_failures, list)
        assert isinstance(result.safe_to_execute, bool)
        assert isinstance(result.reasoning_summary, str)
        assert result.reasoning_summary

    def test_result_contains_all_required_fields(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        d = result.to_dict()
        # Required output fields per contract
        assert "impacted_files" in d
        assert "risk_level" in d
        assert "predicted_failures" in d
        assert "safe_to_execute" in d
        assert "reasoning_summary" in d

    def test_high_risk_blocked_without_override(self):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        result = simulation_gateway(governance_result=gov)
        assert result.risk_level == RISK_HIGH
        assert result.safe_to_execute is False
        assert result.blocked_reason is not None

    def test_high_risk_with_override_passes(self):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        override = {
            "justification": "Emergency production fix approved by CTO",
            "accepted_risks": ["cross_module_impact"],
        }
        result = simulation_gateway(governance_result=gov, override=override)
        assert result.risk_level == RISK_HIGH
        assert result.safe_to_execute is True
        assert result.override_used is True

    def test_execution_boundary_enforced(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert result.execution_boundary["no_file_write"] is True
        assert result.execution_boundary["no_git_commit"] is True
        assert result.execution_boundary["no_deployment_trigger"] is True

    def test_source_contract_id_populated(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert result.source_contract_id == "test-contract-id-001"

    def test_governance_contract_field(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert result.governance_contract == "MUTATION_SIMULATION_EXECUTION_V1"

    def test_audit_id_is_uuid_string(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        import uuid
        uuid.UUID(result.audit_id)  # raises if not valid UUID

    def test_system_context_accepted(self):
        """system_context should be accepted without error."""
        result = simulation_gateway(
            governance_result=_APPROVED_GOVERNANCE_RESULT,
            system_context={"env": "staging", "version": "1.2.3"},
        )
        assert isinstance(result, SimulationResult)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestSimulationAPI:
    @pytest.fixture()
    def client(self) -> TestClient:
        return TestClient(app, raise_server_exceptions=True)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def test_post_simulate_approved_contract(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": _APPROVED_GOVERNANCE_RESULT},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "safe_to_execute" in body
        assert "risk_level" in body
        assert "impacted_files" in body
        assert "predicted_failures" in body
        assert "reasoning_summary" in body

    def test_post_simulate_blocked_governance_result(self, client):
        gov = dict(_APPROVED_GOVERNANCE_RESULT, status="blocked")
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": gov},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["safe_to_execute"] is False
        assert body["blocked_reason"] is not None

    def test_post_simulate_high_risk_blocked(self, client):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": gov},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["risk_level"] == RISK_HIGH
        assert body["safe_to_execute"] is False

    def test_post_simulate_high_risk_with_override(self, client):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        payload = {
            "governance_result": gov,
            "override": {
                "justification": "Approved by senior engineer",
                "accepted_risks": ["cross_module_impact"],
            },
        }
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json=payload,
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["risk_level"] == RISK_HIGH
        assert body["safe_to_execute"] is True
        assert body["override_used"] is True

    def test_post_simulate_requires_auth(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": _APPROVED_GOVERNANCE_RESULT},
            )
        # Missing Authorization header → 401 (no Bearer prefix)
        assert resp.status_code == 401

    def test_post_simulate_rejects_missing_governance_result(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={},
                headers=self._headers(),
            )
        assert resp.status_code == 422

    def test_post_simulate_rejects_invalid_override_missing_justification(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={
                    "governance_result": _APPROVED_GOVERNANCE_RESULT,
                    "override": {"accepted_risks": ["something"]},
                },
                headers=self._headers(),
            )
        assert resp.status_code == 422

    def test_post_simulate_execution_boundary_in_response(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": _APPROVED_GOVERNANCE_RESULT},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        boundary = body["execution_boundary"]
        assert boundary["no_file_write"] is True
        assert boundary["no_git_commit"] is True
        assert boundary["no_deployment_trigger"] is True

    def test_post_simulate_with_system_context(self, client):
        payload = {
            "governance_result": _APPROVED_GOVERNANCE_RESULT,
            "system_context": {"env": "staging"},
        }
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json=payload,
                headers=self._headers(),
            )
        assert resp.status_code == 200

    def test_governance_contract_field_in_response(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": _APPROVED_GOVERNANCE_RESULT},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["governance_contract"] == "MUTATION_SIMULATION_EXECUTION_V1"
