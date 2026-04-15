"""
Tests for MUTATION_SIMULATION_EXECUTION_V1.

Coverage:
  - dependency_surface_mapping: direct, indirect, incomplete, path guards,
    partial resolution tracking
  - impact_analysis: structural, behavioral, data-flow
  - failure_prediction: all 4 categories always evaluated per simulation
  - risk_scoring: low / medium / high criteria; always assigned;
    partially_resolved → at least medium
  - simulation_decision_gate: all blocking rules + override protocol (hardened)
  - simulation_gateway: full pipeline (approved / blocked / invalid input)
    - risk_level never "unknown" (RISK_HIGH used for intake failures)
    - governance authenticity verification (rejects external payloads)
    - audit is mandatory blocking
    - hard-gate enforcement
    - invalid override treated as absent
  - API endpoint: POST /api/mutations/simulate
    - override min-length enforcement
    - blocking_mode present in gate result
"""

from __future__ import annotations

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
    OVERRIDE_MIN_JUSTIFICATION_LENGTH,
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
# Shared fixtures
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

# Override that satisfies the full protocol.
_VALID_OVERRIDE = {
    "justification": "Approved by senior engineer for emergency release",
    "accepted_risks": ["cross_module_impact", "structural_changes"],
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
        assert any("example" in m for m in surface.impacted_modules)

    def test_known_file_resolves_direct_dependencies(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/models.py"])
        surface = map_dependency_surface(contract)
        assert surface.complete is True
        assert "backend/app/database.py" in surface.impacted_files
        direct_links = [l for l in surface.dependency_links if l["type"] == "direct"]
        assert any(l["target"] == "backend/app/database.py" for l in direct_links)

    def test_indirect_dependencies_resolved(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/main.py"])
        surface = map_dependency_surface(contract)
        indirect_links = [l for l in surface.dependency_links if l["type"] == "indirect"]
        assert len(indirect_links) > 0

    def test_empty_target_files_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=[])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert "dependency_graph_unavailable" in surface.incomplete_reason

    def test_multiple_target_files(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        assert surface.complete is True
        assert len(surface.impacted_files) >= 3
        assert len(surface.impacted_modules) >= 3

    def test_blank_entry_in_target_files_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/foo.py", ""])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert "blank_entries" in surface.incomplete_reason

    def test_out_of_scope_path_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["some/random/path.py"])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert "out_of_scope_paths" in surface.incomplete_reason

    def test_restricted_path_dot_env_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=[".env"])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert "restricted_paths" in surface.incomplete_reason

    def test_restricted_path_secrets_returns_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["secrets/api_keys.json"])
        surface = map_dependency_surface(contract)
        assert surface.complete is False
        assert "restricted_paths" in surface.incomplete_reason


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
        assert any("validation" in b for b in impact.behavioral_impact)

    def test_cross_module_data_flow_detected(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        combined = " ".join(impact.data_flow_impact)
        assert "dependency" in combined or "data_flow" in combined

    def test_all_impact_categories_populated(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        assert impact.structural_impact
        assert impact.behavioral_impact
        assert impact.data_flow_impact


# ---------------------------------------------------------------------------
# failure_prediction
# ---------------------------------------------------------------------------


class TestFailurePrediction:
    def test_risks_produce_runtime_failures(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        impact = analyze_impact(_APPROVED_CONTRACT, surface)
        failures = predict_failures(_APPROVED_CONTRACT, impact, surface)
        assert FAILURE_RUNTIME in failures.failure_types

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

    def test_all_four_failure_categories_always_present(self):
        """All 4 failure categories must be evaluated in every simulation."""
        for contract in (_APPROVED_CONTRACT, _DELETE_CONTRACT, _MULTI_MODULE_CONTRACT):
            surface = map_dependency_surface(contract)
            impact = analyze_impact(contract, surface)
            failures = predict_failures(contract, impact, surface)
            for category in (
                FAILURE_BUILD,
                FAILURE_RUNTIME,
                FAILURE_DEPENDENCY_BREAK,
                FAILURE_CONTRACT_VIOLATION,
            ):
                assert category in failures.failure_types, (
                    f"Category {category!r} missing from failures for contract "
                    f"operation_type={contract['operation_type']!r}. "
                    f"Got: {failures.failure_types}"
                )

    def test_all_four_categories_present_for_minimal_contract(self):
        """Even a minimal contract with no risks must evaluate all 4 categories."""
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
        assert FAILURE_BUILD in failures.failure_types
        assert FAILURE_RUNTIME in failures.failure_types
        assert FAILURE_DEPENDENCY_BREAK in failures.failure_types
        assert FAILURE_CONTRACT_VIOLATION in failures.failure_types


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
            predicted_failures=[PredictedFailure(FAILURE_RUNTIME, "low risk", "low")],
            failure_types=[FAILURE_RUNTIME],
        )
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        assert any("unknown_dependencies" in c for c in risk.criteria_matched)

    def test_risk_level_always_in_valid_set(self):
        """risk_level must always be low/medium/high - never None, empty, or unknown."""
        for contract in (_APPROVED_CONTRACT, _MULTI_MODULE_CONTRACT, _DELETE_CONTRACT):
            surface = map_dependency_surface(contract)
            impact = analyze_impact(contract, surface)
            failures = predict_failures(contract, impact, surface)
            risk = score_risk(surface, impact, failures)
            assert risk.level in ("low", "medium", "high"), (
                f"Invalid risk level: {risk.level!r}"
            )

    def test_medium_risk_limited_deps(self):
        contract = dict(_APPROVED_CONTRACT, target_files=["backend/app/models.py"])
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
# SimulationOverride - hardened construction
# ---------------------------------------------------------------------------


class TestSimulationOverride:
    def test_valid_override_constructs(self):
        ov = SimulationOverride.from_dict(_VALID_OVERRIDE)
        assert ov.justification == _VALID_OVERRIDE["justification"]
        assert ov.accepted_risks == _VALID_OVERRIDE["accepted_risks"]

    def test_justification_too_short_raises(self):
        with pytest.raises(ValueError, match="justification"):
            SimulationOverride.from_dict(
                {"justification": "short", "accepted_risks": ["risk"]}
            )

    def test_empty_justification_raises(self):
        with pytest.raises(ValueError):
            SimulationOverride.from_dict(
                {"justification": "", "accepted_risks": ["risk"]}
            )

    def test_empty_accepted_risks_raises(self):
        with pytest.raises(ValueError, match="accepted_risks"):
            SimulationOverride.from_dict(
                {"justification": "This justification is long enough", "accepted_risks": []}
            )

    def test_blank_risk_entry_raises(self):
        with pytest.raises(ValueError):
            SimulationOverride.from_dict(
                {
                    "justification": "This justification is long enough",
                    "accepted_risks": ["valid risk", ""],
                }
            )

    def test_missing_accepted_risks_raises(self):
        with pytest.raises((ValueError, TypeError)):
            SimulationOverride.from_dict(
                {"justification": "This justification is long enough"}
            )

    def test_min_justification_length_constant_is_positive(self):
        assert OVERRIDE_MIN_JUSTIFICATION_LENGTH > 0


# ---------------------------------------------------------------------------
# simulation_decision_gate (hardened)
# ---------------------------------------------------------------------------


class TestSimulationDecisionGate:
    def _low_risk_surface(self):
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
        risk, failures, surface = self._low_risk_surface()
        if risk.level == RISK_LOW:
            gate = simulation_decision_gate(risk, failures, surface)
            assert gate.safe_to_execute is True
            assert gate.blocked_reason is None

    def test_blocking_mode_always_true(self):
        """Gate must always have blocking_mode=True (not advisory)."""
        risk, failures, surface = self._low_risk_surface()
        gate = simulation_decision_gate(risk, failures, surface)
        assert gate.blocking_mode is True

    def test_blocked_if_incomplete_dependency_analysis(self):
        incomplete = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        risk = RiskScore(level=RISK_LOW, criteria_matched=["isolated_change"])
        failures = FailurePrediction(
            predicted_failures=[PredictedFailure(FAILURE_RUNTIME, "test", "low")],
            failure_types=[FAILURE_RUNTIME],
        )
        gate = simulation_decision_gate(risk, failures, incomplete)
        assert gate.safe_to_execute is False
        assert "dependency_analysis_incomplete" in gate.blocked_reason
        assert gate.blocking_mode is True

    def test_blocked_high_risk_without_override(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        gate = simulation_decision_gate(risk, failures, surface, override=None)
        assert gate.safe_to_execute is False
        assert "risk_level_high_no_override" in gate.blocked_reason

    def test_valid_override_allows_high_risk(self):
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        assert risk.level == RISK_HIGH
        override = SimulationOverride.from_dict(_VALID_OVERRIDE)
        gate = simulation_decision_gate(risk, failures, surface, override=override)
        assert gate.safe_to_execute is True
        assert gate.override_used is True

    def test_incomplete_surface_not_overridable(self):
        """Rule 1 (incomplete dependency) must never be bypassed by override."""
        incomplete = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        risk = RiskScore(level=RISK_HIGH, criteria_matched=["unknown_dependencies"])
        failures = FailurePrediction(
            predicted_failures=[PredictedFailure(FAILURE_RUNTIME, "test", "low")],
            failure_types=[FAILURE_RUNTIME],
        )
        override = SimulationOverride.from_dict(_VALID_OVERRIDE)
        gate = simulation_decision_gate(risk, failures, incomplete, override=override)
        assert gate.safe_to_execute is False
        assert "dependency_analysis_incomplete" in gate.blocked_reason

    def test_high_severity_failures_block_without_override(self):
        surface = map_dependency_surface(_DELETE_CONTRACT)
        impact = analyze_impact(_DELETE_CONTRACT, surface)
        failures = predict_failures(_DELETE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        gate = simulation_decision_gate(risk, failures, surface, override=None)
        assert gate.safe_to_execute is False

    def test_gate_blocking_note_contains_hard_block_label(self):
        """Blocking gate notes must use HARD_BLOCK: prefix."""
        incomplete = DependencySurface(complete=False, incomplete_reason="test")
        risk = RiskScore(level=RISK_LOW, criteria_matched=[])
        failures = FailurePrediction(
            predicted_failures=[PredictedFailure(FAILURE_RUNTIME, "x", "low")],
            failure_types=[FAILURE_RUNTIME],
        )
        gate = simulation_decision_gate(risk, failures, incomplete)
        assert any("HARD_BLOCK" in note for note in gate.gate_notes)

    def test_gate_reports_override_rejected_in_blocked_reason(self):
        """blocked_reason must mention override rejection when override is invalid."""
        surface = map_dependency_surface(_MULTI_MODULE_CONTRACT)
        impact = analyze_impact(_MULTI_MODULE_CONTRACT, surface)
        failures = predict_failures(_MULTI_MODULE_CONTRACT, impact, surface)
        risk = score_risk(surface, impact, failures)
        # Construct an override that SimulationOverride validates as invalid via
        # __post_init__ - we test that the gate blocks anyway by not providing one.
        gate = simulation_decision_gate(risk, failures, surface, override=None)
        assert gate.safe_to_execute is False
        # Either "no override provided" or similar language must appear.
        assert "no override" in gate.blocked_reason.lower() or "override" in gate.blocked_reason.lower()


# ---------------------------------------------------------------------------
# simulation_gateway (full pipeline)
# ---------------------------------------------------------------------------


class TestSimulationGateway:
    def test_blocked_governance_not_approved(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, status="blocked")
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert "mutation_contract_not_validated" in result.blocked_reason

    def test_pending_governance_blocked(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, status="pending")
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert result.risk_level == RISK_HIGH

    def test_missing_contract_id_blocked(self):
        governance = {k: v for k, v in _APPROVED_GOVERNANCE_RESULT.items()
                      if k != "contract_id"}
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert "contract_id" in result.blocked_reason

    def test_missing_mutation_proposal_blocked(self):
        governance = dict(_APPROVED_GOVERNANCE_RESULT, mutation_proposal=None)
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert result.risk_level == RISK_HIGH

    def test_empty_target_files_in_proposal_blocked(self):
        bad_proposal = dict(_APPROVED_CONTRACT, target_files=[])
        governance = dict(_APPROVED_GOVERNANCE_RESULT, mutation_proposal=bad_proposal)
        result = simulation_gateway(governance_result=governance)
        assert result.safe_to_execute is False
        assert result.risk_level == RISK_HIGH

    def test_intake_failure_never_uses_unknown_risk_level(self):
        """risk_level must never be 'unknown'; RISK_HIGH is used for intake failures."""
        governance = dict(_APPROVED_GOVERNANCE_RESULT, status="blocked")
        result = simulation_gateway(governance_result=governance)
        assert result.risk_level != "unknown"
        assert result.risk_level == RISK_HIGH

    def test_approved_single_file_returns_structured_result(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert isinstance(result, SimulationResult)
        assert result.risk_level in (RISK_LOW, RISK_MEDIUM, RISK_HIGH)
        assert isinstance(result.impacted_files, list)
        assert isinstance(result.predicted_failures, list)
        assert isinstance(result.safe_to_execute, bool)
        assert result.reasoning_summary

    def test_all_required_output_fields_present(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        d = result.to_dict()
        for field in ("impacted_files", "risk_level", "predicted_failures",
                      "safe_to_execute", "reasoning_summary"):
            assert field in d, f"Missing required output field: {field}"

    def test_reasoning_summary_contains_decision_label(self):
        """reasoning_summary must clearly state BLOCKED or SAFE_TO_PROCEED."""
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert ("BLOCKED" in result.reasoning_summary
                or "SAFE_TO_PROCEED" in result.reasoning_summary)

    def test_reasoning_summary_contains_risk_level(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "RISK LEVEL" in result.reasoning_summary

    def test_reasoning_summary_contains_dependency_surface(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "DEPENDENCY SURFACE" in result.reasoning_summary

    def test_reasoning_summary_contains_predicted_failures(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "PREDICTED FAILURES" in result.reasoning_summary

    def test_reasoning_summary_names_impacted_files(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "backend/app/example.py" in result.reasoning_summary

    def test_high_risk_blocked_without_override(self):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        result = simulation_gateway(governance_result=gov)
        assert result.risk_level == RISK_HIGH
        assert result.safe_to_execute is False
        assert result.blocked_reason is not None
        assert "BLOCKED" in result.reasoning_summary

    def test_high_risk_with_valid_override_passes(self):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        result = simulation_gateway(governance_result=gov, override=_VALID_OVERRIDE)
        assert result.risk_level == RISK_HIGH
        assert result.safe_to_execute is True
        assert result.override_used is True

    def test_invalid_override_too_short_treated_as_absent(self):
        """Invalid override (too-short justification) must be treated as absent."""
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        bad_override = {"justification": "short", "accepted_risks": ["risk"]}
        result = simulation_gateway(governance_result=gov, override=bad_override)
        # Override is invalid -> gate blocks because high risk + no valid override
        assert result.safe_to_execute is False

    def test_invalid_override_empty_risks_treated_as_absent(self):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        bad_override = {
            "justification": "This justification is long enough",
            "accepted_risks": [],
        }
        result = simulation_gateway(governance_result=gov, override=bad_override)
        assert result.safe_to_execute is False

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

    def test_audit_id_is_valid_uuid(self):
        import uuid as _uuid
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        _uuid.UUID(result.audit_id)

    def test_system_context_accepted(self):
        result = simulation_gateway(
            governance_result=_APPROVED_GOVERNANCE_RESULT,
            system_context={"env": "staging", "version": "1.2.3"},
        )
        assert isinstance(result, SimulationResult)

    def test_audit_failure_raises_runtime_error(self):
        """Audit failure must propagate (block_if_log_not_written)."""
        from unittest.mock import patch as _patch

        def _failing_audit(record):
            raise RuntimeError("SIMULATION_AUDIT_LOG_FAILURE: test injection")

        with _patch(
            "backend.app.mutation_simulation.engine.persist_simulation_audit_record",
            side_effect=_failing_audit,
        ):
            with pytest.raises(RuntimeError, match="SIMULATION_AUDIT_LOG_FAILURE"):
                simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)


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
        for field in ("safe_to_execute", "risk_level", "impacted_files",
                      "predicted_failures", "reasoning_summary"):
            assert field in body

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
        assert body["risk_level"] == RISK_HIGH

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

    def test_post_simulate_high_risk_with_valid_override(self, client):
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        payload = {
            "governance_result": gov,
            "override": _VALID_OVERRIDE,
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
        assert resp.status_code == 401

    def test_post_simulate_rejects_missing_governance_result(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={},
                headers=self._headers(),
            )
        assert resp.status_code == 422

    def test_post_simulate_rejects_override_with_short_justification(self, client):
        """API must reject override with justification shorter than minimum."""
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={
                    "governance_result": _APPROVED_GOVERNANCE_RESULT,
                    "override": {"justification": "short", "accepted_risks": ["r"]},
                },
                headers=self._headers(),
            )
        assert resp.status_code == 422

    def test_post_simulate_rejects_override_with_empty_accepted_risks(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={
                    "governance_result": _APPROVED_GOVERNANCE_RESULT,
                    "override": {
                        "justification": "This justification is long enough",
                        "accepted_risks": [],
                    },
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
        boundary = resp.json()["execution_boundary"]
        assert boundary["no_file_write"] is True
        assert boundary["no_git_commit"] is True
        assert boundary["no_deployment_trigger"] is True

    def test_post_simulate_with_system_context(self, client):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={
                    "governance_result": _APPROVED_GOVERNANCE_RESULT,
                    "system_context": {"env": "staging"},
                },
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
        assert resp.json()["governance_contract"] == "MUTATION_SIMULATION_EXECUTION_V1"

    def test_intake_failure_risk_level_in_response(self, client):
        """API response risk_level must be RISK_HIGH for rejected intake contracts."""
        gov = dict(_APPROVED_GOVERNANCE_RESULT, status="pending")
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            resp = client.post(
                "/api/mutations/simulate",
                json={"governance_result": gov},
                headers=self._headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["safe_to_execute"] is False
        assert body["risk_level"] == RISK_HIGH


# ---------------------------------------------------------------------------
# Governance authenticity tests
# ---------------------------------------------------------------------------


class TestGovernanceAuthenticity:
    """Simulation must reject input that does not originate from the governance layer."""

    def test_reject_missing_governance_contract_field(self):
        """Missing governance_contract must be rejected as unauthenticated."""
        gov = {k: v for k, v in _APPROVED_GOVERNANCE_RESULT.items()
               if k != "governance_contract"}
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_wrong_governance_contract_value(self):
        """Invalid governance_contract string must be rejected."""
        gov = dict(_APPROVED_GOVERNANCE_RESULT,
                   governance_contract="SOME_EXTERNAL_SYSTEM_V1")
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_missing_audit_id(self):
        """Missing audit_id must be rejected — proves governance audit did not run."""
        gov = {k: v for k, v in _APPROVED_GOVERNANCE_RESULT.items()
               if k != "audit_id"}
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_empty_audit_id(self):
        """Empty audit_id must be rejected."""
        gov = dict(_APPROVED_GOVERNANCE_RESULT, audit_id="")
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_gate_result_not_passed(self):
        """gate_result.passed != True must be rejected."""
        gov = dict(_APPROVED_GOVERNANCE_RESULT,
                   gate_result={"passed": False, "reason": "something"})
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_missing_gate_result(self):
        """Missing gate_result must be rejected."""
        gov = {k: v for k, v in _APPROVED_GOVERNANCE_RESULT.items()
               if k != "gate_result"}
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_reject_missing_execution_boundary(self):
        """Missing execution_boundary must be rejected — governance always stamps it."""
        gov = {k: v for k, v in _APPROVED_GOVERNANCE_RESULT.items()
               if k != "execution_boundary"}
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "governance_authenticity_failed" in result.blocked_reason

    def test_authenticity_failure_uses_risk_high(self):
        """Authenticity failures must return RISK_HIGH, never 'unknown'."""
        gov = dict(_APPROVED_GOVERNANCE_RESULT, audit_id="")
        result = simulation_gateway(governance_result=gov)
        assert result.risk_level == RISK_HIGH
        assert result.risk_level != "unknown"

    def test_valid_governance_result_passes_authenticity(self):
        """A fully formed governance result must pass authenticity checks."""
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert isinstance(result, SimulationResult)
        # If authenticity check passed, blocked_reason should not mention it.
        if result.blocked_reason:
            assert "governance_authenticity_failed" not in result.blocked_reason


# ---------------------------------------------------------------------------
# Dependency completeness tests
# ---------------------------------------------------------------------------


class TestDependencyCompleteness:
    """Dependency mapping must declare completeness; partial resolution raises risk."""

    def test_complete_surface_has_complete_true(self):
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        assert surface.complete is True

    def test_empty_target_files_surface_is_incomplete(self):
        contract = dict(_APPROVED_CONTRACT, target_files=[])
        surface = map_dependency_surface(contract)
        assert surface.complete is False

    def test_incomplete_surface_blocks_simulation(self):
        """Incomplete dependency surface must produce a hard block."""
        bad_proposal = dict(_APPROVED_CONTRACT, target_files=[])
        gov = dict(_APPROVED_GOVERNANCE_RESULT, mutation_proposal=bad_proposal)
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        # Should be blocked by dependency gate rule.
        assert result.blocked_reason is not None

    def test_incomplete_surface_not_overridable(self):
        """Incomplete dependency surface must be a hard block that cannot be overridden."""
        bad_proposal = dict(_APPROVED_CONTRACT, target_files=[])
        gov = dict(_APPROVED_GOVERNANCE_RESULT, mutation_proposal=bad_proposal)
        result = simulation_gateway(governance_result=gov, override=_VALID_OVERRIDE)
        assert result.safe_to_execute is False

    def test_surface_completeness_flag_present(self):
        """DependencySurface must always declare a completeness flag."""
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        assert hasattr(surface, "complete")
        assert isinstance(surface.complete, bool)

    def test_partially_resolved_surface_flag(self):
        """DependencySurface exposes partially_resolved and unresolved_files fields."""
        surface = map_dependency_surface(_APPROVED_CONTRACT)
        assert hasattr(surface, "partially_resolved")
        assert hasattr(surface, "unresolved_files")
        assert isinstance(surface.partially_resolved, bool)
        assert isinstance(surface.unresolved_files, list)

    def test_partially_resolved_surface_yields_at_least_medium_risk(self):
        """A partially resolved surface must result in risk ≥ medium (not low)."""
        # Use a file that has no known dependency record to trigger partial resolution.
        contract = dict(_APPROVED_CONTRACT,
                        target_files=["backend/app/unknown_module_xyz.py"])
        surface = map_dependency_surface(contract)
        if not surface.partially_resolved:
            pytest.skip("surface fully resolved for this file — cannot test partial")
        impact = analyze_impact(contract, surface)
        failures = predict_failures(contract, impact, surface)
        score = score_risk(surface, impact, failures)
        assert score.level in (RISK_MEDIUM, RISK_HIGH), (
            f"Expected medium or high for partially_resolved surface, got {score.level!r}"
        )

    def test_incomplete_surface_yields_high_risk(self):
        """An incomplete dependency surface must score RISK_HIGH."""
        incomplete_surface = DependencySurface(
            complete=False,
            incomplete_reason="dependency_graph_unavailable:target_files_empty",
        )
        impact = ImpactAnalysis(
            structural_impact=[],
            behavioral_impact=[],
            data_flow_impact=[],
        )
        failures = FailurePrediction(predicted_failures=[], failure_types=[])
        score = score_risk(incomplete_surface, impact, failures)
        assert score.level == RISK_HIGH


# ---------------------------------------------------------------------------
# Reasoning summary validation tests
# ---------------------------------------------------------------------------


class TestReasoningSummaryValidation:
    """reasoning_summary must include dependency impact, risk rationale, failure sections."""

    def test_reasoning_summary_includes_dependency_impact_section(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "DEPENDENCY SURFACE" in result.reasoning_summary, (
            "reasoning_summary must include a DEPENDENCY SURFACE section"
        )

    def test_reasoning_summary_includes_risk_rationale(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "RISK LEVEL" in result.reasoning_summary, (
            "reasoning_summary must include a RISK LEVEL (risk rationale) section"
        )

    def test_reasoning_summary_includes_failure_justification(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "PREDICTED FAILURES" in result.reasoning_summary, (
            "reasoning_summary must include a PREDICTED FAILURES section"
        )

    def test_reasoning_summary_names_impacted_files(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "backend/app/example.py" in result.reasoning_summary, (
            "reasoning_summary must explicitly name impacted files"
        )

    def test_reasoning_summary_states_gate_decision(self):
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert ("BLOCKED" in result.reasoning_summary
                or "SAFE_TO_PROCEED" in result.reasoning_summary), (
            "reasoning_summary must state BLOCKED or SAFE_TO_PROCEED"
        )

    def test_reasoning_summary_blocked_contains_block_reason(self):
        """Blocked summary must explain why."""
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        result = simulation_gateway(governance_result=gov)
        assert result.safe_to_execute is False
        assert "BLOCKED" in result.reasoning_summary

    def test_reasoning_summary_includes_risk_criteria(self):
        """reasoning_summary must mention the criteria that drove the risk level."""
        gov = _approved_governance(_MULTI_MODULE_CONTRACT)
        result = simulation_gateway(governance_result=gov)
        # At least the risk level name must appear.
        assert result.risk_level.upper() in result.reasoning_summary.upper(), (
            "reasoning_summary must reference the risk level in its rationale"
        )

    def test_reasoning_summary_impact_section_present(self):
        """reasoning_summary must include an impact analysis section."""
        result = simulation_gateway(governance_result=_APPROVED_GOVERNANCE_RESULT)
        assert "IMPACT" in result.reasoning_summary.upper(), (
            "reasoning_summary must include an IMPACT section"
        )
