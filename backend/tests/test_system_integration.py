"""
SYSTEM_INTEGRATION_TEST_V1
===========================
End-to-end pipeline validation: intent → governance → simulation → bridge.

Tests the full request/response flow via the FastAPI TestClient, covering
every pipeline stage and verifying all invariants across system boundaries.

Invariants validated
--------------------
- audit_chain_complete:
    governance.audit_id → simulation.source_governance_audit_id
    simulation.simulation_id → bridge.source_simulation_id
- no_execution_without_validation:
    simulation must reject non-approved governance results
- no_bridge_without_simulation:
    bridge must reject invalid simulation results
- artifact_consistency:
    bridge.modified_files_list ⊆ mutation_proposal.target_files
- simulation_only:
    no file writes, no git commits, no external side effects
    (execution_boundary enforced on every pipeline stage)

External dependencies (OpenAI, DB) are replaced with deterministic stubs
so that tests are hermetic and always pass in CI.
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_system_integration")

from backend.app.main import app

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

TOKEN = "test-integration-key"
_AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# A realistic intent message that exercises the full pipeline.
_INTENT_MESSAGE = (
    "Create a mutation to update backend/app/example.py to improve logging"
)

# The governance AI mock returns a valid contract for a single known file.
# backend/app/example.py is within the allowed backend/ scope, has no known
# backend dependency entries, so the simulation produces a complete surface
# with partially_resolved=True → medium risk → safe_to_execute=True.
_INTEGRATION_PROPOSAL: dict[str, Any] = {
    "target_files": ["backend/app/example.py"],
    "operation_type": "update_file",
    "proposed_changes": (
        "Add structured logging to improve pipeline observability."
    ),
    "assumptions": ["The file exists and has an importable logger variable"],
    "alternatives": ["Use a logging middleware instead"],
    "confidence": 0.85,
    "risks": ["May increase log verbosity"],
    "missing_data": ["none"],
}

_MOCK_AI_RESPONSE = (
    "SECTION_INTENT_ANALYSIS:\n"
    "ASSUMPTIONS: The file exists and has an importable logger variable\n"
    "ALTERNATIVES: Use a logging middleware instead\n"
    "CONFIDENCE: 0.85\n"
    "MISSING_DATA: none\n"
    "\n"
    "SECTION_MUTATION_CONTRACT:\n"
    + json.dumps(_INTEGRATION_PROPOSAL)
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """TestClient with all audit writes and the governance AI call patched out.

    The three audit patches prevent DB writes while keeping the rest of the
    pipeline (validation, gate, result construction) fully intact.
    The governance AI patch returns a deterministic approved contract.
    """
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "backend.app.mutation_governance.engine"
                ".persist_mutation_audit_record"
            )
        )
        stack.enter_context(
            patch(
                "backend.app.mutation_simulation.engine"
                ".persist_simulation_audit_record"
            )
        )
        stack.enter_context(
            patch("backend.app.mutation_bridge.engine.persist_bridge_audit_record")
        )
        stack.enter_context(
            patch(
                "backend.app.mutation_routes._build_ai_call",
                return_value=lambda _prompt: _MOCK_AI_RESPONSE,
            )
        )
        stack.enter_context(patch.dict(os.environ, {"API_KEY": TOKEN}))
        yield TestClient(app)


# ===========================================================================
# Step 1 — POST /api/chat/intent
# ===========================================================================


class TestStep1Intent:
    """Intent parsing returns a structured object and never executes."""

    def test_returns_200(self, client):
        resp = client.post(
            "/api/chat/intent",
            json={"message": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_response_is_structured_intent(self, client):
        resp = client.post(
            "/api/chat/intent",
            json={"message": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        # Required top-level schema fields (schemaVersion 2).
        assert "intentId" in data
        assert data["intentId"]  # non-empty UUID
        assert "intent" in data
        assert "mode" in data

    def test_intent_never_executes(self, client):
        """The intent endpoint must not return an execution instruction."""
        resp = client.post(
            "/api/chat/intent",
            json={"message": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        # Mode A (no repo_context) always returns canExecuteDeterministically=false.
        change_plan = data.get("changePlan", {})
        assert change_plan.get("canExecuteDeterministically") is False

    def test_missing_message_returns_400(self, client):
        resp = client.post(
            "/api/chat/intent",
            json={},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 400


# ===========================================================================
# Step 2 — POST /api/mutations/propose
# ===========================================================================


class TestStep2Governance:
    """Governance produces an approved proposal with a mandatory audit_id."""

    def test_returns_200(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_status_approved(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert data["status"] == "approved"

    def test_mutation_proposal_present(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "mutation_proposal" in data
        assert data["mutation_proposal"]  # non-empty dict
        assert "target_files" in data["mutation_proposal"]

    def test_audit_id_present(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "audit_id" in data
        assert data["audit_id"]  # non-empty string

    def test_contract_id_present(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "contract_id" in data
        assert data["contract_id"]

    def test_execution_boundary_enforced(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        boundary = resp.json().get("execution_boundary", {})
        assert boundary.get("no_git_commit") is True
        assert boundary.get("no_file_write") is True
        assert boundary.get("no_deployment_trigger") is True


# ===========================================================================
# Step 3 — POST /api/mutations/simulate
# ===========================================================================


class TestStep3Simulation:
    """Simulation assigns risk, maps dependencies, and links back to governance."""

    @pytest.fixture()
    def governance_result(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        return resp.json()

    def test_returns_200(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_risk_level_assigned(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert data["risk_level"] in ("low", "medium", "high")

    def test_dependency_surface_present(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "impacted_files" in data
        assert isinstance(data["impacted_files"], list)
        assert "backend/app/example.py" in data["impacted_files"]

    def test_failure_prediction_present(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "predicted_failures" in data
        assert isinstance(data["predicted_failures"], list)

    def test_audit_id_present(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "audit_id" in data
        assert data["audit_id"]

    def test_simulation_id_present(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "simulation_id" in data
        assert data["simulation_id"]

    def test_source_governance_audit_id_linked(self, client, governance_result):
        """Audit chain: governance.audit_id → simulation.source_governance_audit_id."""
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "source_governance_audit_id" in data
        assert data["source_governance_audit_id"] == governance_result["audit_id"]

    def test_execution_boundary_enforced(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        boundary = resp.json().get("execution_boundary", {})
        assert boundary.get("no_file_write") is True
        assert boundary.get("no_git_commit") is True
        assert boundary.get("no_deployment_trigger") is True

    def test_rejects_non_approved_governance(self, client):
        """no_execution_without_validation invariant."""
        blocked_gov = {
            "contract_id": "test-blocked-id",
            "governance_contract": "MUTATION_GOVERNANCE_EXECUTION_V1",
            "status": "blocked",
            "mutation_proposal": dict(_INTEGRATION_PROPOSAL),
            "validation_results": [],
            "gate_result": {"passed": False},
            "blocked_reason": "stage_1_structural_validation failed",
            "audit_id": "test-blocked-audit",
            "created_at": "2026-01-01T00:00:00+00:00",
            "execution_boundary": {
                "no_git_commit": True,
                "no_file_write": True,
                "no_deployment_trigger": True,
            },
        }
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": blocked_gov},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["safe_to_execute"] is False
        assert data["blocked_reason"]

    def test_rejects_governance_without_audit_id(self, client, governance_result):
        """governance_result without audit_id must be rejected."""
        bad_gov = dict(governance_result)
        bad_gov["audit_id"] = ""
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": bad_gov},
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert data["safe_to_execute"] is False


# ===========================================================================
# Step 4 — POST /api/mutations/execute
# ===========================================================================


class TestStep4Bridge:
    """Bridge executes (simulated) and links back to the simulation audit chain."""

    @pytest.fixture()
    def governance_result(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        return resp.json()

    @pytest.fixture()
    def simulation_result(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        return resp.json()

    def test_returns_200(self, client, governance_result, simulation_result):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_status_executed_or_blocked(
        self, client, governance_result, simulation_result
    ):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert data["status"] in ("executed", "blocked")

    def test_execution_summary_present(
        self, client, governance_result, simulation_result
    ):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "execution_summary" in data
        assert isinstance(data["execution_summary"], str)

    def test_audit_id_present(self, client, governance_result, simulation_result):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "audit_id" in data
        assert data["audit_id"]

    def test_bridge_id_present(self, client, governance_result, simulation_result):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "bridge_id" in data
        assert data["bridge_id"]

    def test_source_simulation_id_linked(
        self, client, governance_result, simulation_result
    ):
        """Audit chain: simulation.simulation_id → bridge.source_simulation_id."""
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert "source_simulation_id" in data
        assert data["source_simulation_id"] == simulation_result["simulation_id"]

    def test_execution_boundary_enforced(
        self, client, governance_result, simulation_result
    ):
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": simulation_result,
            },
            headers=_AUTH_HEADERS,
        )
        boundary = resp.json().get("execution_boundary", {})
        assert boundary.get("no_direct_commit_to_main") is True
        assert boundary.get("no_auto_merge") is True
        assert boundary.get("no_deployment_trigger") is True

    def test_rejects_invalid_simulation_result(
        self, client, governance_result, simulation_result
    ):
        """no_bridge_without_simulation invariant."""
        bad_sim = dict(simulation_result)
        bad_sim["safe_to_execute"] = False
        bad_sim["blocked_reason"] = "test block"
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": bad_sim,
            },
            headers=_AUTH_HEADERS,
        )
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["blocked_reason"]

    def test_rejects_simulation_without_required_fields(
        self, client, governance_result, simulation_result
    ):
        """Bridge must reject simulation dicts missing required fields."""
        bad_sim = {
            "safe_to_execute": True,
            "risk_level": "low",
        }
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": bad_sim,
            },
            headers=_AUTH_HEADERS,
        )
        # FastAPI request-body validator rejects missing governance_contract field.
        assert resp.status_code in (200, 422)
        if resp.status_code == 200:
            assert resp.json()["status"] == "blocked"


# ===========================================================================
# Full pipeline trace — complete end-to-end invariant validation
# ===========================================================================


class TestFullPipelineTrace:
    """Validate all invariants across the complete 4-step pipeline."""

    @pytest.fixture()
    def pipeline_trace(self, client):
        """Run the full pipeline and return all four stage responses."""
        # Step 1: intent
        intent_resp = client.post(
            "/api/chat/intent",
            json={"message": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert intent_resp.status_code == 200

        # Step 2: governance
        gov_resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert gov_resp.status_code == 200
        gov = gov_resp.json()

        # Step 3: simulation
        sim_resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": gov},
            headers=_AUTH_HEADERS,
        )
        assert sim_resp.status_code == 200
        sim = sim_resp.json()

        # Step 4: bridge
        bridge_resp = client.post(
            "/api/mutations/execute",
            json={"governance_result": gov, "simulation_result": sim},
            headers=_AUTH_HEADERS,
        )
        assert bridge_resp.status_code == 200

        return {
            "intent": intent_resp.json(),
            "governance": gov,
            "simulation": sim,
            "bridge": bridge_resp.json(),
        }

    def test_audit_chain_complete(self, pipeline_trace):
        """governance.audit_id → simulation.source_governance_audit_id
        simulation.simulation_id → bridge.source_simulation_id
        """
        gov = pipeline_trace["governance"]
        sim = pipeline_trace["simulation"]
        bridge = pipeline_trace["bridge"]

        # Link 1: governance → simulation
        assert sim["source_governance_audit_id"] == gov["audit_id"]

        # Link 2: simulation → bridge
        assert bridge["source_simulation_id"] == sim["simulation_id"]

    def test_simulation_only_no_execution(self, pipeline_trace):
        """Execution boundary must be present on every stage response."""
        gov_boundary = pipeline_trace["governance"].get("execution_boundary", {})
        sim_boundary = pipeline_trace["simulation"].get("execution_boundary", {})
        bridge_boundary = pipeline_trace["bridge"].get("execution_boundary", {})

        # Governance boundary
        assert gov_boundary.get("no_git_commit") is True
        assert gov_boundary.get("no_file_write") is True

        # Simulation boundary
        assert sim_boundary.get("no_file_write") is True
        assert sim_boundary.get("no_git_commit") is True

        # Bridge boundary (different keys per contract)
        assert bridge_boundary.get("no_direct_commit_to_main") is True
        assert bridge_boundary.get("no_auto_merge") is True
        assert bridge_boundary.get("no_deployment_trigger") is True

    def test_artifact_consistency(self, pipeline_trace):
        """bridge.modified_files_list must be a subset of target_files."""
        target_files = pipeline_trace["governance"]["mutation_proposal"]["target_files"]
        modified_files = pipeline_trace["bridge"].get("modified_files_list", [])
        # All modified files must originate from the governance proposal.
        for fpath in modified_files:
            assert fpath in target_files, (
                f"{fpath!r} in modified_files_list but not in target_files"
            )

    def test_final_status_present(self, pipeline_trace):
        """Every stage must carry a deterministic status."""
        assert pipeline_trace["governance"]["status"] in ("approved", "blocked")
        assert isinstance(pipeline_trace["simulation"]["safe_to_execute"], bool)
        assert pipeline_trace["bridge"]["status"] in ("executed", "blocked")

    def test_all_stage_audit_ids_are_unique(self, pipeline_trace):
        """Each stage produces its own unique audit_id."""
        gov_audit = pipeline_trace["governance"]["audit_id"]
        sim_audit = pipeline_trace["simulation"]["audit_id"]
        bridge_audit = pipeline_trace["bridge"]["audit_id"]

        assert gov_audit
        assert sim_audit
        assert bridge_audit
        # All three IDs must be distinct.
        ids = {gov_audit, sim_audit, bridge_audit}
        assert len(ids) == 3, f"audit_ids are not unique: {ids}"

    def test_execution_summary_contains_boundary_markers(self, pipeline_trace):
        """Execution boundary must be declared as invariant, not just convention."""
        summary = pipeline_trace["bridge"].get("execution_summary", "")
        assert "SIMULATED_EXECUTION_ONLY" in summary, (
            "execution_summary must contain SIMULATED_EXECUTION_ONLY marker"
        )
        assert "NO_REAL_MUTATION" in summary, (
            "execution_summary must contain NO_REAL_MUTATION marker"
        )


# ===========================================================================
# Negative path tests — failure behavior must be deterministic
# ===========================================================================


class TestNegativePaths:
    """Prove the system fails correctly — failure behavior is where governance lives."""

    @pytest.fixture()
    def governance_result(self, client):
        resp = client.post(
            "/api/mutations/propose",
            json={"intent": _INTENT_MESSAGE},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        return resp.json()

    @pytest.fixture()
    def simulation_result(self, client, governance_result):
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": governance_result},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        return resp.json()

    # -----------------------------------------------------------------------
    # Simulation — rejects governance without audit_id
    # -----------------------------------------------------------------------

    def test_simulation_rejects_missing_governance_audit_id(
        self, client, governance_result
    ):
        """Simulation must hard-block when governance audit_id is absent."""
        bad_gov = dict(governance_result)
        bad_gov.pop("audit_id", None)
        resp = client.post(
            "/api/mutations/simulate",
            json={"governance_result": bad_gov},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["safe_to_execute"] is False
        assert data["blocked_reason"]

    # -----------------------------------------------------------------------
    # Bridge — rejects simulation with broken audit chain
    # -----------------------------------------------------------------------

    def test_bridge_rejects_missing_source_governance_audit_id(
        self, client, governance_result, simulation_result
    ):
        """Bridge must block when source_governance_audit_id is absent."""
        bad_sim = dict(simulation_result)
        del bad_sim["source_governance_audit_id"]
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": bad_sim,
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["blocked_reason"]

    def test_bridge_rejects_tampered_source_governance_audit_id(
        self, client, governance_result, simulation_result
    ):
        """Bridge must block when source_governance_audit_id does not match governance.

        A forged or replayed audit ID must be caught at the revalidation step,
        proving the bridge cannot be bypassed by constructing a plausible-looking
        simulation result.
        """
        bad_sim = dict(simulation_result)
        bad_sim["source_governance_audit_id"] = "fake-audit-id-tampered"
        resp = client.post(
            "/api/mutations/execute",
            json={
                "governance_result": governance_result,
                "simulation_result": bad_sim,
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["blocked_reason"]

