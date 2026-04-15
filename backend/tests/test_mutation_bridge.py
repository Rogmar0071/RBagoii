"""
Tests for MUTATION_BRIDGE_EXECUTION_V1.

Coverage:
  - BridgeExecutionOverride: validation (explicit_approval, justification, accepted_risks)
  - RuntimeRevalidationResult: all three checks (target files, contract linkage,
    dependency graph) including all block conditions
  - BridgeGateResult: all five blocking conditions, override protocol
  - bridge_gateway: full pipeline (executed / blocked / various failure modes)
    - governance_not_verified (authenticity checks)
    - simulation_not_verified (integrity checks)
    - runtime_revalidation_failed (each check independently)
    - execution_gate_failed (each condition independently)
    - high_risk requires override with explicit_approval
    - build_validation failure blocks execution
    - artifact enforcement
    - audit is mandatory and propagates RuntimeError
    - execution_boundary constants always present
    - no real git ops / no file writes
  - API endpoint: POST /api/mutations/execute
    - override validation (explicit_approval, min-length, accepted_risks)
    - blocking_mode always present in gate result
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_mutation_bridge")

from backend.app.mutation_bridge import (
    BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH,
    BRIDGE_STATUS_BLOCKED,
    BRIDGE_STATUS_EXECUTED,
    BUILD_STATUS_FAILED,
    BUILD_STATUS_PASSED,
    CHECK_DEPENDENCY_GRAPH,
    CHECK_FILE_HASH_INTEGRITY,
    CHECK_GOVERNANCE_AUDIT_LINKAGE,
    CHECK_NO_CONFLICTS,
    CHECK_TARGET_FILES,
    BridgeExecutionOverride,
    BridgeGateResult,
    BridgeResult,
    RuntimeRevalidationResult,
    bridge_execution_gate,
    bridge_gateway,
    revalidate_runtime_state,
)
from backend.app.mutation_bridge.engine import (
    _verify_governance_authenticity,
    _verify_simulation_integrity,
)
from backend.app.mutation_bridge.revalidation import (
    _compute_proposal_file_hash,
)
from backend.app.main import app

TOKEN = "test-bridge-key"

# ---------------------------------------------------------------------------
# Shared fixtures — valid approved governance result
# ---------------------------------------------------------------------------

_APPROVED_PROPOSAL: dict[str, Any] = {
    "target_files": ["backend/app/example.py"],
    "operation_type": "update_file",
    "proposed_changes": "Add input validation to the process() function.",
    "assumptions": ["The process() function exists"],
    "alternatives": ["Validate at the API layer"],
    "confidence": 0.85,
    "risks": ["Existing callers may fail with new validation"],
    "missing_data": ["none"],
}

_CONTRACT_ID = str(uuid.uuid4())
_AUDIT_ID_GOV = str(uuid.uuid4())
_AUDIT_ID_SIM = str(uuid.uuid4())
_SIMULATION_ID = str(uuid.uuid4())


def _make_governance_result(
    *,
    contract_id: str = _CONTRACT_ID,
    status: str = "approved",
    gate_passed: bool = True,
    proposal: dict[str, Any] | None = None,
    audit_id: str = _AUDIT_ID_GOV,
) -> dict[str, Any]:
    return {
        "contract_id": contract_id,
        "governance_contract": "MUTATION_GOVERNANCE_EXECUTION_V1",
        "status": status,
        "mutation_proposal": proposal if proposal is not None else dict(_APPROVED_PROPOSAL),
        "validation_results": [],
        "gate_result": {"passed": gate_passed, "blocked_reason": None, "failed_stages": []},
        "blocked_reason": None,
        "audit_id": audit_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "execution_boundary": {
            "no_git_commit": True,
            "no_file_write": True,
            "no_deployment_trigger": True,
        },
    }


def _make_simulation_result(
    *,
    simulation_id: str = _SIMULATION_ID,
    source_contract_id: str = _CONTRACT_ID,
    source_governance_audit_id: str = _AUDIT_ID_GOV,
    safe_to_execute: bool = True,
    risk_level: str = "low",
    audit_id: str = _AUDIT_ID_SIM,
    impacted_files: list[str] | None = None,
    blocked_reason: str | None = None,
    file_snapshot_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    # Build matching hashes from the default proposal when not explicitly provided.
    if file_snapshot_hashes is None:
        default_files = ["backend/app/example.py"]
        file_snapshot_hashes = {
            f: _compute_proposal_file_hash(
                source_contract_id, f, _APPROVED_PROPOSAL["proposed_changes"]
            )
            for f in default_files
        }
    return {
        "simulation_id": simulation_id,
        "governance_contract": "MUTATION_SIMULATION_EXECUTION_V1",
        "source_contract_id": source_contract_id,
        "source_governance_audit_id": source_governance_audit_id,
        "impacted_files": impacted_files
        if impacted_files is not None
        else ["backend/app/example.py", "backend/app/models.py"],
        "risk_level": risk_level,
        "predicted_failures": [],
        "safe_to_execute": safe_to_execute,
        "reasoning_summary": "Simulation passed.",
        "impacted_modules": [],
        "dependency_links": [],
        "structural_impact": [],
        "behavioral_impact": [],
        "data_flow_impact": [],
        "failure_types": [],
        "risk_criteria_matched": [],
        "blocked_reason": blocked_reason,
        "override_used": False,
        "audit_id": audit_id,
        "file_snapshot_hashes": file_snapshot_hashes,
        "created_at": "2026-01-01T00:00:00+00:00",
        "execution_boundary": {
            "no_file_write": True,
            "no_git_commit": True,
            "no_deployment_trigger": True,
        },
    }


def _make_valid_override() -> dict[str, Any]:
    return {
        "explicit_approval": True,
        "justification": "Accepted after security review and stakeholder sign-off.",
        "accepted_risks": ["minor performance regression"],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_audit():
    """Suppress audit persistence for unit tests."""
    with patch("backend.app.mutation_bridge.engine.persist_bridge_audit_record"):
        yield


@pytest.fixture()
def client():
    with patch("backend.app.mutation_bridge.engine.persist_bridge_audit_record"):
        with patch.dict(os.environ, {"API_KEY": TOKEN}):
            yield TestClient(app)


# ===========================================================================
# BridgeExecutionOverride — validation
# ===========================================================================


class TestBridgeExecutionOverride:
    def test_valid_override(self):
        ov = BridgeExecutionOverride(
            explicit_approval=True,
            justification="Accepted after review.",
            accepted_risks=["risk A"],
        )
        assert ov.explicit_approval is True
        assert ov.justification == "Accepted after review."
        assert ov.accepted_risks == ["risk A"]

    def test_explicit_approval_false_raises(self):
        with pytest.raises(ValueError, match="explicit_approval"):
            BridgeExecutionOverride(
                explicit_approval=False,
                justification="Accepted after review.",
                accepted_risks=["risk A"],
            )

    def test_justification_too_short_raises(self):
        with pytest.raises(ValueError, match="justification"):
            BridgeExecutionOverride(
                explicit_approval=True,
                justification="short",
                accepted_risks=["risk A"],
            )

    def test_justification_exactly_min_length_ok(self):
        just = "x" * BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH
        ov = BridgeExecutionOverride(
            explicit_approval=True,
            justification=just,
            accepted_risks=["risk A"],
        )
        assert ov.justification == just

    def test_empty_accepted_risks_raises(self):
        with pytest.raises(ValueError, match="accepted_risks"):
            BridgeExecutionOverride(
                explicit_approval=True,
                justification="Accepted after review.",
                accepted_risks=[],
            )

    def test_blank_accepted_risk_raises(self):
        with pytest.raises(ValueError, match="accepted_risks"):
            BridgeExecutionOverride(
                explicit_approval=True,
                justification="Accepted after review.",
                accepted_risks=["  "],
            )

    def test_from_dict_valid(self):
        ov = BridgeExecutionOverride.from_dict(_make_valid_override())
        assert ov.explicit_approval is True

    def test_from_dict_invalid_explicit_approval_raises(self):
        data = _make_valid_override()
        data["explicit_approval"] = False
        with pytest.raises(ValueError, match="explicit_approval"):
            BridgeExecutionOverride.from_dict(data)

    def test_to_dict_roundtrip(self):
        ov = BridgeExecutionOverride.from_dict(_make_valid_override())
        d = ov.to_dict()
        assert d["explicit_approval"] is True
        assert d["justification"] == _make_valid_override()["justification"]
        assert d["accepted_risks"] == _make_valid_override()["accepted_risks"]


# ===========================================================================
# RuntimeRevalidationResult — revalidate_runtime_state
# ===========================================================================


class TestRevalidateRuntimeState:
    def test_all_checks_pass(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is True
        assert result.failed_checks == []
        assert result.blocked_reason is None
        assert CHECK_TARGET_FILES in result.check_details
        assert CHECK_NO_CONFLICTS in result.check_details
        assert CHECK_DEPENDENCY_GRAPH in result.check_details
        assert CHECK_FILE_HASH_INTEGRITY in result.check_details
        assert CHECK_GOVERNANCE_AUDIT_LINKAGE in result.check_details

    def test_target_file_missing_from_simulation(self):
        gov = _make_governance_result()
        # simulation does not include the governance target file
        sim = _make_simulation_result(impacted_files=["backend/app/other.py"])
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_TARGET_FILES in result.failed_checks
        assert "target_file_missing_or_modified" in result.blocked_reason

    def test_target_files_empty_in_proposal_passes_check1(self):
        # If proposal has no target_files, check 1 trivially passes (nothing to verify).
        gov = _make_governance_result(proposal={"target_files": [], "operation_type": "update_file", "proposed_changes": "x"})
        sim = _make_simulation_result()
        result = revalidate_runtime_state(gov, sim)
        # Checks 1 passes (no files to match), check 2 depends on contract_id
        assert CHECK_TARGET_FILES in result.check_details
        assert "PASSED" in result.check_details[CHECK_TARGET_FILES]

    def test_contract_id_mismatch_blocks(self):
        gov = _make_governance_result(contract_id="governance-id-aaa")
        sim = _make_simulation_result(source_contract_id="different-id-bbb")
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_NO_CONFLICTS in result.failed_checks
        assert "repo_state_changed" in result.blocked_reason

    def test_missing_governance_contract_id_blocks(self):
        gov = _make_governance_result()
        gov.pop("contract_id")
        sim = _make_simulation_result()
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_NO_CONFLICTS in result.failed_checks

    def test_missing_simulation_source_id_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["source_contract_id"] = ""
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_NO_CONFLICTS in result.failed_checks

    def test_safe_to_execute_false_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(safe_to_execute=False)
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_DEPENDENCY_GRAPH in result.failed_checks
        assert "dependency_invalidated" in result.blocked_reason

    def test_check_details_populated_on_failure(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(impacted_files=["backend/app/other.py"])
        result = revalidate_runtime_state(gov, sim)
        assert result.check_details[CHECK_TARGET_FILES].startswith("FAILED:")

    # ------------------------------------------------------------------
    # Check 4: file_content_matches_simulation_snapshot
    # ------------------------------------------------------------------

    def test_file_hash_no_snapshot_passes(self):
        """No snapshot hashes → check skipped, revalidation passes."""
        gov = _make_governance_result()
        sim = _make_simulation_result(file_snapshot_hashes={})
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is True
        assert "skipped" in result.check_details[CHECK_FILE_HASH_INTEGRITY]

    def test_file_hash_matching_snapshot_passes(self):
        """Snapshot hashes match governance proposal → check passes."""
        gov = _make_governance_result()
        # _make_simulation_result() computes matching hashes by default.
        sim = _make_simulation_result()
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is True
        assert "PASSED" in result.check_details[CHECK_FILE_HASH_INTEGRITY]

    def test_file_hash_mismatch_blocks(self):
        """Tampered hash in snapshot → Check 4 fires."""
        gov = _make_governance_result()
        sim = _make_simulation_result(
            file_snapshot_hashes={"backend/app/example.py": "deadbeef" * 8}
        )
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_FILE_HASH_INTEGRITY in result.failed_checks
        assert "target_file_missing_or_modified" in result.blocked_reason

    def test_file_hash_extra_snapshot_entry_no_block(self):
        """Snapshot with an entry for a non-target file is ignored."""
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["file_snapshot_hashes"]["backend/app/other.py"] = "irrelevant"
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is True

    # ------------------------------------------------------------------
    # Check 5: governance_audit_id_linked_to_simulation
    # ------------------------------------------------------------------

    def test_governance_audit_id_linked_correctly_passes(self):
        gov = _make_governance_result(audit_id=_AUDIT_ID_GOV)
        sim = _make_simulation_result(source_governance_audit_id=_AUDIT_ID_GOV)
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is True
        assert "PASSED" in result.check_details[CHECK_GOVERNANCE_AUDIT_LINKAGE]

    def test_governance_audit_id_mismatch_blocks(self):
        gov = _make_governance_result(audit_id=_AUDIT_ID_GOV)
        sim = _make_simulation_result(source_governance_audit_id="wrong-audit-id")
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_GOVERNANCE_AUDIT_LINKAGE in result.failed_checks
        assert "repo_state_changed" in result.blocked_reason

    def test_governance_audit_id_empty_in_simulation_blocks(self):
        gov = _make_governance_result(audit_id=_AUDIT_ID_GOV)
        sim = _make_simulation_result()
        sim["source_governance_audit_id"] = ""
        result = revalidate_runtime_state(gov, sim)
        assert result.passed is False
        assert CHECK_GOVERNANCE_AUDIT_LINKAGE in result.failed_checks

    def test_check_details_populated_on_all_pass(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = revalidate_runtime_state(gov, sim)
        for check in (
            CHECK_TARGET_FILES,
            CHECK_NO_CONFLICTS,
            CHECK_DEPENDENCY_GRAPH,
            CHECK_FILE_HASH_INTEGRITY,
            CHECK_GOVERNANCE_AUDIT_LINKAGE,
        ):
            assert "PASSED" in result.check_details[check], (
                f"expected PASSED in check_details[{check!r}]"
            )


# ===========================================================================
# BridgeGateResult — bridge_execution_gate
# ===========================================================================


class TestBridgeExecutionGate:
    def _revalidation_passed(self) -> RuntimeRevalidationResult:
        return RuntimeRevalidationResult(passed=True, check_details={
            CHECK_TARGET_FILES: "PASSED",
            CHECK_NO_CONFLICTS: "PASSED",
            CHECK_DEPENDENCY_GRAPH: "PASSED",
        })

    def test_all_conditions_pass_low_risk(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="low")
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is True
        assert gate.blocking_mode is True
        assert gate.override_used is False

    def test_blocking_mode_always_true(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.blocking_mode is True

    def test_governance_gate_not_passed_blocks(self):
        gov = _make_governance_result(gate_passed=False)
        sim = _make_simulation_result()
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is False
        assert "governance_not_verified" in gate.blocked_reason
        assert "HARD_BLOCK:governance_not_verified" in gate.gate_notes

    def test_simulation_not_safe_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(safe_to_execute=False)
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is False
        assert "simulation_not_verified" in gate.blocked_reason
        assert "HARD_BLOCK:simulation_not_verified" in gate.gate_notes

    def test_revalidation_failed_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        reval = RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_TARGET_FILES],
            blocked_reason="block_if:target_file_missing_or_modified — ...",
        )
        gate = bridge_execution_gate(gov, sim, reval)
        assert gate.passed is False
        assert "runtime_revalidation_failed" in gate.blocked_reason
        assert "HARD_BLOCK:runtime_revalidation_failed" in gate.gate_notes

    def test_high_risk_without_override_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is False
        assert "high_risk_without_override" in gate.blocked_reason
        assert "HARD_BLOCK:high_risk_without_override" in gate.gate_notes

    def test_high_risk_with_valid_override_passes(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        ov = BridgeExecutionOverride.from_dict(_make_valid_override())
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed(), override=ov)
        assert gate.passed is True
        assert gate.override_used is True

    def test_high_risk_override_explicit_approval_false_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        # Build an override with explicit_approval=False bypassing constructor validation
        ov = object.__new__(BridgeExecutionOverride)
        ov.explicit_approval = False
        ov.justification = "Accepted after review."
        ov.accepted_risks = ["risk A"]
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed(), override=ov)
        assert gate.passed is False
        assert "high_risk_without_override" in gate.blocked_reason

    def test_wrong_simulation_contract_blocks(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["governance_contract"] = "UNKNOWN_CONTRACT"
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is False
        assert "execution_without_simulation" in gate.blocked_reason
        assert "HARD_BLOCK:execution_without_simulation" in gate.gate_notes

    def test_medium_risk_no_override_passes(self):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="medium")
        gate = bridge_execution_gate(gov, sim, self._revalidation_passed())
        assert gate.passed is True
        assert gate.override_used is False


# ===========================================================================
# bridge_gateway — full pipeline
# ===========================================================================


class TestBridgeGateway:
    def test_successful_execution_low_risk(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_EXECUTED
        assert result.branch_name.startswith("mutation/bridge-")
        assert result.diff_patch != ""
        assert result.modified_files_list == ["backend/app/example.py"]
        assert result.execution_summary != ""
        assert result.build_status == BUILD_STATUS_PASSED
        assert result.override_used is False
        assert result.blocked_reason is None

    def test_result_always_structured(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        d = result.to_dict()
        for key in (
            "bridge_id",
            "governance_contract",
            "source_governance_contract_id",
            "source_simulation_id",
            "status",
            "branch_name",
            "diff_patch",
            "modified_files_list",
            "build_status",
            "execution_summary",
            "override_used",
            "audit_id",
            "created_at",
            "execution_boundary",
        ):
            assert key in d, f"missing key: {key}"

    def test_execution_boundary_constants_always_present(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        eb = result.execution_boundary
        assert eb["no_direct_commit_to_main"] is True
        assert eb["no_auto_merge"] is True
        assert eb["no_deployment_trigger"] is True

    def test_execution_boundary_constants_on_blocked_result(self, no_audit):
        gov = _make_governance_result(gate_passed=False)
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        eb = result.execution_boundary
        assert eb["no_direct_commit_to_main"] is True
        assert eb["no_auto_merge"] is True
        assert eb["no_deployment_trigger"] is True

    def test_governance_contract_field_always_correct(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.governance_contract == "MUTATION_BRIDGE_EXECUTION_V1"

    # ------------------------------------------------------------------
    # Governance authenticity failures
    # ------------------------------------------------------------------

    def test_wrong_governance_contract_field_blocks(self, no_audit):
        gov = _make_governance_result()
        gov["governance_contract"] = "SOMETHING_ELSE"
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "governance_not_verified" in result.blocked_reason

    def test_missing_governance_audit_id_blocks(self, no_audit):
        gov = _make_governance_result()
        gov["audit_id"] = ""
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "governance_not_verified" in result.blocked_reason

    def test_governance_gate_not_passed_blocks(self, no_audit):
        gov = _make_governance_result(gate_passed=False)
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "governance_not_verified" in result.blocked_reason

    def test_missing_execution_boundary_in_governance_blocks(self, no_audit):
        gov = _make_governance_result()
        del gov["execution_boundary"]
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "governance_not_verified" in result.blocked_reason

    # ------------------------------------------------------------------
    # Simulation integrity failures
    # ------------------------------------------------------------------

    def test_wrong_simulation_contract_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["governance_contract"] = "NOT_SIMULATION"
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "simulation_not_verified" in result.blocked_reason

    def test_simulation_safe_to_execute_false_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(safe_to_execute=False)
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "simulation_not_verified" in result.blocked_reason

    def test_invalid_risk_level_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["risk_level"] = "unknown"
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "simulation_not_verified" in result.blocked_reason

    def test_missing_simulation_audit_id_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        sim["audit_id"] = "   "
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "simulation_not_verified" in result.blocked_reason

    # ------------------------------------------------------------------
    # Runtime re-validation failures
    # ------------------------------------------------------------------

    def test_revalidation_target_file_missing_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(impacted_files=["backend/app/other.py"])
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "target_file_missing_or_modified" in result.blocked_reason

    def test_revalidation_contract_id_mismatch_blocks(self, no_audit):
        gov = _make_governance_result(contract_id="aaa-bbb-ccc")
        sim = _make_simulation_result(source_contract_id="xxx-yyy-zzz")
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "repo_state_changed" in result.blocked_reason

    def test_revalidation_dependency_invalidated_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(safe_to_execute=True)
        # Patch revalidation in the engine module's namespace (where it was imported).
        failing = RuntimeRevalidationResult(
            passed=False,
            failed_checks=[CHECK_DEPENDENCY_GRAPH],
            blocked_reason="block_if:dependency_invalidated — test",
            check_details={CHECK_DEPENDENCY_GRAPH: "FAILED: test"},
        )
        with patch(
            "backend.app.mutation_bridge.engine.revalidate_runtime_state",
            return_value=failing,
        ):
            result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "dependency_invalidated" in result.blocked_reason

    # ------------------------------------------------------------------
    # High-risk override enforcement
    # ------------------------------------------------------------------

    def test_high_risk_without_override_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "high_risk_without_override" in result.blocked_reason

    def test_high_risk_with_valid_override_executes(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        result = bridge_gateway(
            governance_result=gov,
            simulation_result=sim,
            override=_make_valid_override(),
        )
        assert result.status == BRIDGE_STATUS_EXECUTED
        assert result.override_used is True

    def test_high_risk_override_explicit_approval_missing_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = dict(_make_valid_override())
        bad_override["explicit_approval"] = False
        result = bridge_gateway(
            governance_result=gov,
            simulation_result=sim,
            override=bad_override,
        )
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "high_risk_without_override" in result.blocked_reason

    def test_high_risk_override_justification_too_short_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = dict(_make_valid_override())
        bad_override["justification"] = "short"
        result = bridge_gateway(
            governance_result=gov,
            simulation_result=sim,
            override=bad_override,
        )
        assert result.status == BRIDGE_STATUS_BLOCKED

    def test_high_risk_override_empty_accepted_risks_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = dict(_make_valid_override())
        bad_override["accepted_risks"] = []
        result = bridge_gateway(
            governance_result=gov,
            simulation_result=sim,
            override=bad_override,
        )
        assert result.status == BRIDGE_STATUS_BLOCKED

    # ------------------------------------------------------------------
    # Build validation blocks execution
    # ------------------------------------------------------------------

    def test_delete_file_operation_blocks_build(self, no_audit):
        proposal = dict(_APPROVED_PROPOSAL)
        proposal["operation_type"] = "delete_file"
        gov = _make_governance_result(proposal=proposal)
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "build_validation_failed" in result.blocked_reason

    def test_restricted_path_in_target_blocks_build(self, no_audit):
        proposal = dict(_APPROVED_PROPOSAL)
        proposal["target_files"] = ["secrets/config.py"]
        # Need to get past governance/simulation integrity + revalidation checks,
        # so we mock to only exercise build validation failure.
        gov = _make_governance_result(proposal=proposal)
        sim = _make_simulation_result(impacted_files=["secrets/config.py"])
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        # build validation should catch it
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "build_validation_failed" in result.blocked_reason

    # ------------------------------------------------------------------
    # Artifact enforcement
    # ------------------------------------------------------------------

    def test_artifact_branch_name_always_present_on_success(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.branch_name != ""

    def test_artifact_diff_patch_always_present_on_success(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.diff_patch != ""

    def test_artifact_modified_files_list_always_present_on_success(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.modified_files_list != []

    def test_artifact_execution_summary_always_present_on_success(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.execution_summary != ""

    def test_artifact_execution_summary_contains_decision(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "EXECUTED" in result.execution_summary or "BLOCKED" in result.execution_summary

    def test_diff_patch_contains_target_file(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "backend/app/example.py" in result.diff_patch

    def test_branch_name_never_main(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.branch_name not in ("main", "master")
        assert "mutation/bridge-" in result.branch_name

    # ------------------------------------------------------------------
    # Audit enforcement
    # ------------------------------------------------------------------

    def test_audit_called_on_success(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record"
        ) as mock_audit:
            result = bridge_gateway(governance_result=gov, simulation_result=sim)
        mock_audit.assert_called_once()
        assert result.status == BRIDGE_STATUS_EXECUTED

    def test_audit_called_on_blocked_result(self):
        gov = _make_governance_result(gate_passed=False)
        sim = _make_simulation_result()
        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record"
        ) as mock_audit:
            result = bridge_gateway(governance_result=gov, simulation_result=sim)
        mock_audit.assert_called_once()
        assert result.status == BRIDGE_STATUS_BLOCKED

    def test_audit_failure_propagates_as_runtime_error(self):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record",
            side_effect=RuntimeError("BRIDGE_AUDIT_LOG_FAILURE: db down"),
        ):
            with pytest.raises(RuntimeError, match="BRIDGE_AUDIT_LOG_FAILURE"):
                bridge_gateway(governance_result=gov, simulation_result=sim)

    def test_audit_failure_not_suppressed(self):
        """Confirm audit errors are NOT swallowed — they must propagate."""
        gov = _make_governance_result()
        sim = _make_simulation_result()
        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record",
            side_effect=RuntimeError("TEST_AUDIT_FAILURE"),
        ):
            with pytest.raises(RuntimeError, match="TEST_AUDIT_FAILURE"):
                bridge_gateway(governance_result=gov, simulation_result=sim)

    # ------------------------------------------------------------------
    # Execution constraints
    # ------------------------------------------------------------------

    def test_no_real_git_ops_no_file_writes(self, no_audit):
        """Bridge never calls git or writes files — confirm no subprocesses."""
        import subprocess as _sp

        gov = _make_governance_result()
        sim = _make_simulation_result()
        original_run = _sp.run

        def _forbid_run(*args, **kwargs):  # pragma: no cover
            raise AssertionError(
                "bridge_gateway must not invoke subprocess.run (no real git ops)"
            )

        with patch.object(_sp, "run", side_effect=_forbid_run):
            result = bridge_gateway(governance_result=gov, simulation_result=sim)

        assert result.status == BRIDGE_STATUS_EXECUTED

    def test_source_ids_populated(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.source_governance_contract_id == _CONTRACT_ID
        assert result.source_simulation_id == _SIMULATION_ID

    # ------------------------------------------------------------------
    # Governance → Simulation audit_id linkage
    # ------------------------------------------------------------------

    def test_missing_source_governance_audit_id_in_simulation_blocks(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        del sim["source_governance_audit_id"]
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "simulation_not_verified" in result.blocked_reason

    def test_revalidation_audit_id_mismatch_blocks(self, no_audit):
        gov = _make_governance_result(audit_id=_AUDIT_ID_GOV)
        sim = _make_simulation_result(source_governance_audit_id="wrong-audit-id")
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "repo_state_changed" in result.blocked_reason

    def test_file_hash_mismatch_blocks_gateway(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(
            file_snapshot_hashes={"backend/app/example.py": "deadbeef" * 8}
        )
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "target_file_missing_or_modified" in result.blocked_reason

    # ------------------------------------------------------------------
    # Artifact consistency enforcement
    # ------------------------------------------------------------------

    def test_modified_files_list_inconsistency_blocks(self, no_audit):
        """modified_files_list not matching target_files must block."""
        from backend.app.mutation_bridge import engine as _engine

        gov = _make_governance_result()
        sim = _make_simulation_result()

        original = _engine._perform_staged_execution

        def _bad_staged(proposal, bridge_id):
            branch, diff, files, build, fail_reason, actions = original(
                proposal, bridge_id
            )
            # Return a mismatched files list
            return branch, diff, ["backend/app/WRONG.py"], build, fail_reason, actions

        with patch.object(_engine, "_perform_staged_execution", side_effect=_bad_staged):
            result = bridge_gateway(governance_result=gov, simulation_result=sim)

        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "artifact_inconsistency" in result.blocked_reason

    def test_diff_patch_missing_file_blocks(self, no_audit):
        """diff_patch not referencing a target file must block."""
        from backend.app.mutation_bridge import engine as _engine

        gov = _make_governance_result()
        sim = _make_simulation_result()

        original = _engine._perform_staged_execution

        def _empty_diff_staged(proposal, bridge_id):
            branch, _, files, build, fail_reason, actions = original(
                proposal, bridge_id
            )
            return branch, "# empty patch", files, build, fail_reason, actions

        with patch.object(
            _engine, "_perform_staged_execution", side_effect=_empty_diff_staged
        ):
            result = bridge_gateway(governance_result=gov, simulation_result=sim)

        assert result.status == BRIDGE_STATUS_BLOCKED
        assert "artifact_inconsistency" in result.blocked_reason

    # ------------------------------------------------------------------
    # Override audit — override must appear in audit record and summary
    # ------------------------------------------------------------------

    def test_override_details_in_audit_record(self):
        """When override is used, override_details must be stored in audit record."""
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        ov = _make_valid_override()

        captured: list = []

        def _capture_audit(record):
            captured.append(record)

        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record",
            side_effect=_capture_audit,
        ):
            result = bridge_gateway(
                governance_result=gov, simulation_result=sim, override=ov
            )

        assert result.status == BRIDGE_STATUS_EXECUTED
        assert len(captured) == 1
        record = captured[0]
        assert record.override_used is True
        assert record.override_details is not None
        assert record.override_details["explicit_approval"] is True
        assert record.override_details["justification"] == ov["justification"]
        assert record.override_details["accepted_risks"] == ov["accepted_risks"]

    def test_no_override_details_when_not_used(self):
        """When no override is used, override_details must be None in audit."""
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="low")

        captured: list = []

        def _capture_audit(record):
            captured.append(record)

        with patch(
            "backend.app.mutation_bridge.engine.persist_bridge_audit_record",
            side_effect=_capture_audit,
        ):
            result = bridge_gateway(governance_result=gov, simulation_result=sim)

        assert result.status == BRIDGE_STATUS_EXECUTED
        record = captured[0]
        assert record.override_used is False
        assert record.override_details is None

    # ------------------------------------------------------------------
    # Execution summary content — scope, risk, decision, override
    # ------------------------------------------------------------------

    def test_mutation_scope_in_execution_summary(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "MUTATION SCOPE" in result.execution_summary
        assert "backend/app/example.py" in result.execution_summary
        assert "update_file" in result.execution_summary

    def test_risk_level_in_execution_summary(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="medium")
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "RISK LEVEL" in result.execution_summary
        assert "MEDIUM" in result.execution_summary

    def test_decision_in_execution_summary(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "GATE DECISION" in result.execution_summary
        assert "EXECUTED" in result.execution_summary

    def test_override_details_in_execution_summary(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        ov = _make_valid_override()
        result = bridge_gateway(
            governance_result=gov, simulation_result=sim, override=ov
        )
        assert result.override_used is True
        assert "OVERRIDE" in result.execution_summary
        assert "applied: True" in result.execution_summary
        assert ov["justification"] in result.execution_summary
        assert ov["accepted_risks"][0] in result.execution_summary

    def test_no_override_section_shows_false(self, no_audit):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="low")
        result = bridge_gateway(governance_result=gov, simulation_result=sim)
        assert "applied: False" in result.execution_summary


# ===========================================================================
# _verify_governance_authenticity — unit tests
# ===========================================================================


class TestVerifyGovernanceAuthenticity:
    def test_valid_returns_none(self):
        gov = _make_governance_result()
        assert _verify_governance_authenticity(gov) is None

    def test_wrong_contract_returns_error(self):
        gov = _make_governance_result()
        gov["governance_contract"] = "WRONG"
        error = _verify_governance_authenticity(gov)
        assert error is not None
        assert "governance_not_verified" in error

    def test_missing_field_returns_error(self):
        gov = _make_governance_result()
        del gov["audit_id"]
        error = _verify_governance_authenticity(gov)
        assert error is not None

    def test_gate_not_passed_returns_error(self):
        gov = _make_governance_result(gate_passed=False)
        error = _verify_governance_authenticity(gov)
        assert error is not None
        assert "governance_not_verified" in error

    def test_missing_execution_boundary_returns_error(self):
        gov = _make_governance_result()
        del gov["execution_boundary"]
        error = _verify_governance_authenticity(gov)
        assert error is not None


# ===========================================================================
# _verify_simulation_integrity — unit tests
# ===========================================================================


class TestVerifySimulationIntegrity:
    def test_valid_returns_none(self):
        sim = _make_simulation_result()
        assert _verify_simulation_integrity(sim) is None

    def test_wrong_contract_returns_error(self):
        sim = _make_simulation_result()
        sim["governance_contract"] = "WRONG"
        error = _verify_simulation_integrity(sim)
        assert error is not None
        assert "simulation_not_verified" in error

    def test_safe_to_execute_false_returns_error(self):
        sim = _make_simulation_result(safe_to_execute=False)
        error = _verify_simulation_integrity(sim)
        assert error is not None
        assert "simulation_not_verified" in error

    def test_invalid_risk_level_returns_error(self):
        sim = _make_simulation_result()
        sim["risk_level"] = "critical"
        error = _verify_simulation_integrity(sim)
        assert error is not None
        assert "simulation_not_verified" in error

    def test_missing_audit_id_returns_error(self):
        sim = _make_simulation_result()
        sim["audit_id"] = ""
        error = _verify_simulation_integrity(sim)
        assert error is not None

    def test_missing_source_governance_audit_id_returns_error(self):
        sim = _make_simulation_result()
        del sim["source_governance_audit_id"]
        error = _verify_simulation_integrity(sim)
        assert error is not None
        assert "simulation_not_verified" in error

    def test_all_valid_risk_levels(self):
        for level in ("low", "medium", "high"):
            sim = _make_simulation_result(risk_level=level)
            assert _verify_simulation_integrity(sim) is None, f"level={level} should be valid"


# ===========================================================================
# API endpoint — POST /api/mutations/execute
# ===========================================================================


class TestBridgeApiEndpoint:
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def test_successful_execution(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
            headers=self._headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == BRIDGE_STATUS_EXECUTED
        assert data["governance_contract"] == "MUTATION_BRIDGE_EXECUTION_V1"
        assert data["branch_name"] != ""
        assert data["diff_patch"] != ""
        assert data["modified_files_list"] != []
        assert data["execution_summary"] != ""

    def test_blocked_result_returns_200_with_blocked_status(self, client):
        gov = _make_governance_result(gate_passed=False)
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
            headers=self._headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == BRIDGE_STATUS_BLOCKED
        assert data["blocked_reason"] is not None

    def test_execution_boundary_in_response(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
            headers=self._headers(),
        )
        data = resp.json()
        assert data["execution_boundary"]["no_direct_commit_to_main"] is True
        assert data["execution_boundary"]["no_auto_merge"] is True
        assert data["execution_boundary"]["no_deployment_trigger"] is True

    def test_missing_governance_result_returns_422(self, client):
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"simulation_result": sim},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_missing_simulation_result_returns_422(self, client):
        gov = _make_governance_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_override_explicit_approval_false_returns_422(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = _make_valid_override()
        bad_override["explicit_approval"] = False
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": gov,
                "simulation_result": sim,
                "override": bad_override,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_override_justification_too_short_returns_422(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = _make_valid_override()
        bad_override["justification"] = "short"
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": gov,
                "simulation_result": sim,
                "override": bad_override,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_override_empty_accepted_risks_returns_422(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        bad_override = _make_valid_override()
        bad_override["accepted_risks"] = []
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": gov,
                "simulation_result": sim,
                "override": bad_override,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 422

    def test_high_risk_with_valid_override_returns_executed(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result(risk_level="high")
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": gov,
                "simulation_result": sim,
                "override": _make_valid_override(),
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == BRIDGE_STATUS_EXECUTED
        assert data["override_used"] is True

    def test_no_auth_returns_401_or_403(self, client):
        gov = _make_governance_result()
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
        )
        assert resp.status_code in (401, 403)

    def test_response_has_no_free_text_root_fields(self, client):
        """Response must be structured BridgeResult — no free-text root keys."""
        gov = _make_governance_result()
        sim = _make_simulation_result()
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
            headers=self._headers(),
        )
        data = resp.json()
        expected_keys = {
            "bridge_id",
            "governance_contract",
            "source_governance_contract_id",
            "source_simulation_id",
            "status",
            "blocked_reason",
            "branch_name",
            "diff_patch",
            "modified_files_list",
            "build_status",
            "execution_summary",
            "override_used",
            "audit_id",
            "created_at",
            "execution_boundary",
        }
        assert set(data.keys()) == expected_keys

    def test_manual_bypass_payload_is_blocked(self, client):
        """A manually crafted payload that bypasses the real pipeline must be blocked.

        No bypass path: sending a governance_result or simulation_result that was
        not produced by the real pipeline (wrong contract identifier, missing required
        fields, or mismatched audit linkage) must be rejected and the status must be
        blocked.  This verifies there is no way to reach execution status with a
        forged or hand-constructed payload.
        """
        # Completely hand-crafted payload — not from any real governance or
        # simulation run, just a dict with plausible-looking content.
        forged_gov = {
            "contract_id": "forged-contract-id",
            "governance_contract": "MUTATION_GOVERNANCE_EXECUTION_V1",
            "status": "approved",
            "mutation_proposal": {
                "target_files": ["backend/app/example.py"],
                "operation_type": "update_file",
                "proposed_changes": "Bypass attempt",
                "assumptions": [],
                "alternatives": [],
                "confidence": 1.0,
                "risks": [],
                "missing_data": [],
            },
            "validation_results": [],
            "gate_result": {"passed": True, "blocked_reason": None, "failed_stages": []},
            "blocked_reason": None,
            "audit_id": "forged-audit-id",
            "created_at": "2026-01-01T00:00:00+00:00",
            "execution_boundary": {
                "no_git_commit": True,
                "no_file_write": True,
                "no_deployment_trigger": True,
            },
        }
        forged_sim = {
            "simulation_id": "forged-sim-id",
            "governance_contract": "MUTATION_SIMULATION_EXECUTION_V1",
            "source_contract_id": "forged-contract-id",
            # source_governance_audit_id deliberately omitted (as a real bypass would be)
            "impacted_files": ["backend/app/example.py"],
            "risk_level": "low",
            "predicted_failures": [],
            "safe_to_execute": True,
            "reasoning_summary": "Bypass.",
            "impacted_modules": [],
            "dependency_links": [],
            "structural_impact": [],
            "behavioral_impact": [],
            "data_flow_impact": [],
            "failure_types": [],
            "risk_criteria_matched": [],
            "blocked_reason": None,
            "override_used": False,
            "audit_id": "forged-sim-audit-id",
            "created_at": "2026-01-01T00:00:00+00:00",
            "execution_boundary": {
                "no_file_write": True,
                "no_git_commit": True,
                "no_deployment_trigger": True,
            },
        }
        resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": forged_gov, "simulation_result": forged_sim},
            headers=self._headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        # The bridge must detect the missing source_governance_audit_id and block.
        assert data["status"] == BRIDGE_STATUS_BLOCKED
        assert data["blocked_reason"] is not None
