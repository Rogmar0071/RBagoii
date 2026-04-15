"""
backend.app.mutation_simulation.engine
========================================
MUTATION_SIMULATION_EXECUTION_V1 — main simulation gateway.

System pipeline:
  receive_validated_mutation_contract
    → dependency_surface_mapping
    → impact_analysis
    → failure_prediction
    → risk_scoring
    → simulation_decision_gate
    → audit_log                (mandatory, blocking on DB failure)
    → return_simulation_result (structured object ONLY — no execution)

Governance invariants enforced:
  - simulation_precedes_execution       Simulation layer does NOT execute mutations.
  - all_mutations_must_be_scored        Every simulation run produces a risk_level.
  - high_risk_requires_override         High-risk simulations blocked without override.
  - no_unanalyzed_mutation_passes       All mutations go through the full pipeline.
  - audit_is_mandatory                  block_if_log_not_written.

Execution boundary (enforced constants):
  - no_file_write
  - no_git_commit
  - no_deployment_trigger

Depends on:
  - MUTATION_GOVERNANCE_EXECUTION_V1  (validated contract required)
  - MODE_ENGINE_EXECUTION_V2          (mode alignment enforced at governance layer)
"""

from __future__ import annotations

import logging
from typing import Any

from .audit import persist_simulation_audit_record
from .contract import (
    SimulationAuditRecord,
    SimulationOverride,
    SimulationResult,
)
from .dependency_surface import map_dependency_surface
from .failure_prediction import predict_failures
from .gate import simulation_decision_gate
from .impact_analysis import analyze_impact
from .risk_scoring import score_risk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution boundary constants (simulation layer — no execution allowed)
# ---------------------------------------------------------------------------

_EXECUTION_BOUNDARY: dict[str, bool] = {
    "no_file_write": True,
    "no_git_commit": True,
    "no_deployment_trigger": True,
}

# ---------------------------------------------------------------------------
# Enforced modes (alignment with MODE_ENGINE_EXECUTION_V2)
# ---------------------------------------------------------------------------

_ENFORCED_MODES: list[str] = ["strict_mode", "prediction_mode", "audit_mode"]


def simulation_gateway(
    *,
    governance_result: dict[str, Any],
    override: dict[str, Any] | None = None,
    system_context: dict[str, Any] | None = None,
) -> SimulationResult:
    """Single mandatory entry/exit point for the simulation pipeline.

    Pipeline (MUTATION_SIMULATION_EXECUTION_V1):
      1. receive_validated_mutation_contract — verify governance_result is approved.
      2. dependency_surface_mapping          — identify all impacted components.
      3. impact_analysis                     — structural/behavioral/data-flow effects.
      4. failure_prediction                  — possible failure modes.
      5. risk_scoring                        — deterministic low/medium/high level.
      6. simulation_decision_gate            — block if any blocking rule fires.
      7. audit_log                           — mandatory write (raises on DB failure).
      8. return_simulation_result            — structured SimulationResult only.

    Parameters
    ----------
    governance_result:
        The dict output of ``MutationGovernanceResult.to_dict()``.
        ``status`` MUST be ``"approved"`` — blocked/pending contracts are
        rejected at step 1 (``block_if:mutation_contract_not_validated``).
    override:
        Optional dict ``{"justification": str, "accepted_risks": [str, ...]}``.
        Required (and validated) when risk_level turns out to be high.
    system_context:
        Optional ambient context (ignored in this version but accepted for
        future extension without breaking the call-site contract).

    Returns
    -------
    SimulationResult
        Always structured.  ``safe_to_execute=True`` means no blockers found.
        ``safe_to_execute=False`` means execution MUST NOT proceed.

    Raises
    ------
    RuntimeError
        If the database is configured and the audit write fails
        (``block_if_log_not_written``).
    """
    # ------------------------------------------------------------------
    # Step 1: validate the incoming governance result
    # ------------------------------------------------------------------
    result = SimulationResult()
    result.source_contract_id = str(governance_result.get("contract_id", ""))

    status = governance_result.get("status", "")
    mutation_proposal: dict[str, Any] = governance_result.get("mutation_proposal") or {}

    if status != "approved" or not mutation_proposal:
        return _build_blocked_result(
            result=result,
            mutation_proposal=mutation_proposal,
            blocked_reason=(
                "block_if:mutation_contract_not_validated — "
                f"governance status={status!r}; mutation_proposal present="
                f"{bool(mutation_proposal)}"
            ),
            override=None,
        )

    # ------------------------------------------------------------------
    # Step 2: dependency surface mapping
    # ------------------------------------------------------------------
    surface = map_dependency_surface(mutation_proposal)

    # ------------------------------------------------------------------
    # Step 3: impact analysis
    # ------------------------------------------------------------------
    impact = analyze_impact(mutation_proposal, surface)

    # ------------------------------------------------------------------
    # Step 4: failure prediction
    # ------------------------------------------------------------------
    failures = predict_failures(mutation_proposal, impact, surface)

    # ------------------------------------------------------------------
    # Step 5: risk scoring
    # ------------------------------------------------------------------
    risk = score_risk(surface, impact, failures)

    # ------------------------------------------------------------------
    # Step 6: build override object if provided
    # ------------------------------------------------------------------
    sim_override: SimulationOverride | None = None
    if override:
        sim_override = SimulationOverride.from_dict(override)

    # ------------------------------------------------------------------
    # Step 7: simulation decision gate
    # ------------------------------------------------------------------
    gate = simulation_decision_gate(risk, failures, surface, sim_override)

    # ------------------------------------------------------------------
    # Populate result fields
    # ------------------------------------------------------------------
    result.impacted_files = surface.impacted_files
    result.impacted_modules = surface.impacted_modules
    result.dependency_links = surface.dependency_links
    result.structural_impact = impact.structural_impact
    result.behavioral_impact = impact.behavioral_impact
    result.data_flow_impact = impact.data_flow_impact
    result.failure_types = failures.failure_types
    result.predicted_failures = [f.to_dict() for f in failures.predicted_failures]
    result.risk_level = risk.level
    result.risk_criteria_matched = risk.criteria_matched
    result.safe_to_execute = gate.safe_to_execute
    result.blocked_reason = gate.blocked_reason
    result.override_used = gate.override_used

    result.reasoning_summary = _build_reasoning_summary(
        mutation_proposal=mutation_proposal,
        surface=surface,
        impact=impact,
        failures=failures,
        risk=risk,
        gate=gate,
    )

    # ------------------------------------------------------------------
    # Step 8: audit log (mandatory — raises if DB write fails)
    # ------------------------------------------------------------------
    audit = SimulationAuditRecord(
        audit_id=result.audit_id,
        simulation_id=result.simulation_id,
        mutation_contract=mutation_proposal,
        simulation_outputs={
            "impacted_files": result.impacted_files,
            "impacted_modules": result.impacted_modules,
            "structural_impact": result.structural_impact,
            "behavioral_impact": result.behavioral_impact,
            "data_flow_impact": result.data_flow_impact,
            "failure_types": result.failure_types,
            "predicted_failures": result.predicted_failures,
            "risk_criteria_matched": result.risk_criteria_matched,
        },
        risk_level=result.risk_level,
        decision=result.safe_to_execute,
        override_used=result.override_used,
        blocked_reason=result.blocked_reason,
    )
    persist_simulation_audit_record(audit)

    # ------------------------------------------------------------------
    # Step 9: return structured result (no execution)
    # ------------------------------------------------------------------
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_blocked_result(
    *,
    result: SimulationResult,
    mutation_proposal: dict[str, Any],
    blocked_reason: str,
    override: SimulationOverride | None,
) -> SimulationResult:
    """Populate *result* as a blocked simulation and persist audit record."""
    result.safe_to_execute = False
    result.blocked_reason = blocked_reason
    result.risk_level = "unknown"
    result.reasoning_summary = f"Simulation blocked at intake gate: {blocked_reason}"

    audit = SimulationAuditRecord(
        audit_id=result.audit_id,
        simulation_id=result.simulation_id,
        mutation_contract=mutation_proposal,
        simulation_outputs={},
        risk_level=result.risk_level,
        decision=False,
        override_used=False,
        blocked_reason=blocked_reason,
    )
    persist_simulation_audit_record(audit)
    return result


def _build_reasoning_summary(
    *,
    mutation_proposal: dict[str, Any],
    surface: object,
    impact: object,
    failures: object,
    risk: object,
    gate: object,
) -> str:
    """Produce a concise human-readable reasoning summary."""
    from .contract import DependencySurface, FailurePrediction, ImpactAnalysis, RiskScore
    from .gate import SimulationGateResult

    assert isinstance(surface, DependencySurface)
    assert isinstance(impact, ImpactAnalysis)
    assert isinstance(failures, FailurePrediction)
    assert isinstance(risk, RiskScore)
    assert isinstance(gate, SimulationGateResult)

    op = mutation_proposal.get("operation_type", "unknown")
    targets = mutation_proposal.get("target_files", [])
    n_files = len(surface.impacted_files)
    n_modules = len(surface.impacted_modules)
    n_failures = len(failures.predicted_failures)
    high_count = sum(1 for f in failures.predicted_failures if f.severity == "high")
    decision_str = "SAFE_TO_PROCEED" if gate.safe_to_execute else "BLOCKED"

    parts = [
        f"Operation: {op} on {len(targets)} target file(s).",
        f"Dependency surface: {n_files} impacted file(s) across {n_modules} module(s).",
        f"Risk level: {risk.level.upper()} (criteria: {', '.join(risk.criteria_matched)}).",
        f"Predicted failures: {n_failures} total, {high_count} high-severity.",
        f"Decision: {decision_str}.",
    ]
    if gate.blocked_reason:
        parts.append(f"Blocked because: {gate.blocked_reason}")
    if gate.override_used:
        parts.append("Override was accepted for this simulation.")

    return " ".join(parts)
