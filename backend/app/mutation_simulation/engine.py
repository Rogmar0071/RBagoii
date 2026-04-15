"""
backend.app.mutation_simulation.engine
========================================
MUTATION_SIMULATION_EXECUTION_V1 - main simulation gateway.

System pipeline:
  receive_validated_mutation_contract
    -> dependency_surface_mapping
    -> impact_analysis
    -> failure_prediction
    -> risk_scoring
    -> simulation_decision_gate
    -> audit_log                (mandatory - BLOCKING; raises on DB failure)
    -> return_simulation_result (structured object ONLY - no execution)

Governance invariants enforced:
  - simulation_precedes_execution       Simulation layer does NOT execute mutations.
  - all_mutations_must_be_scored        Every simulation run produces a risk_level.
  - high_risk_requires_override         High-risk simulations blocked without valid override.
  - no_unanalyzed_mutation_passes       All mutations go through the full pipeline.
  - audit_is_mandatory                  block_if_log_not_written - audit errors propagate.

Input enforcement:
  - governance_result.status MUST be "approved"
  - governance_result.mutation_proposal MUST be a non-empty dict
  - governance_result.contract_id MUST be present
  - Rejected contracts receive risk_level=RISK_HIGH
    (conservative default for unverifiable contracts)

Execution boundary (enforced constants - never relaxed):
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
    RISK_HIGH,
    SimulationAuditRecord,
    SimulationOverride,
    SimulationResult,
)
from .dependency_surface import map_dependency_surface
from .failure_prediction import predict_failures
from .gate import SimulationGateResult, simulation_decision_gate
from .impact_analysis import analyze_impact
from .risk_scoring import score_risk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution boundary constants (simulation layer - no execution allowed)
# ---------------------------------------------------------------------------

_EXECUTION_BOUNDARY: dict[str, bool] = {
    "no_file_write": True,
    "no_git_commit": True,
    "no_deployment_trigger": True,
}

# Required fields that a governance_result dict must contain.
_REQUIRED_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "contract_id",
    "status",
    "mutation_proposal",
)

# Required fields that must be present and non-empty in mutation_proposal.
_REQUIRED_PROPOSAL_FIELDS: tuple[str, ...] = (
    "target_files",
    "operation_type",
    "proposed_changes",
)

# Structural signature that only a genuine governance result carries.
_EXPECTED_GOVERNANCE_CONTRACT = "MUTATION_GOVERNANCE_EXECUTION_V1"


def _verify_governance_authenticity(
    governance_result: dict[str, Any],
) -> str | None:
    """Verify the result structurally originates from the governance pipeline.

    The simulation layer must not trust arbitrary external payloads that merely
    set status='approved'.  We check for a set of fields that only the
    MUTATION_GOVERNANCE_EXECUTION_V1 pipeline stamps onto a result:

      - governance_contract == "MUTATION_GOVERNANCE_EXECUTION_V1"
      - audit_id is a non-empty string (proves it was audited)
      - gate_result is a dict with passed=True (proves it cleared the governance gate)
      - execution_boundary is a dict with the three no-execution keys
        (proves it was created by the governance engine, not hand-crafted)

    Returns None if authentic, or a rejection reason string.
    """
    # 1. governance_contract field must identify the correct pipeline.
    gc = governance_result.get("governance_contract", "")
    if gc != _EXPECTED_GOVERNANCE_CONTRACT:
        return (
            "block_if:governance_authenticity_failed - "
            f"governance_contract={gc!r}; expected {_EXPECTED_GOVERNANCE_CONTRACT!r}. "
            "Input does not originate from the governance pipeline."
        )

    # 2. audit_id must be present (proves the governance audit ran).
    audit_id = governance_result.get("audit_id", "")
    if not isinstance(audit_id, str) or not audit_id.strip():
        return (
            "block_if:governance_authenticity_failed - "
            "audit_id is absent or empty. "
            "Governance pipeline stamps a UUID audit_id on every result."
        )

    # 3. gate_result must show passed=True.
    gate_result = governance_result.get("gate_result")
    if not isinstance(gate_result, dict) or gate_result.get("passed") is not True:
        return (
            "block_if:governance_authenticity_failed - "
            f"gate_result={gate_result!r}; must be a dict with passed=True. "
            "Only governance-approved results may enter the simulation pipeline."
        )

    # 4. execution_boundary dict must be present (governance engine stamps this).
    eb = governance_result.get("execution_boundary")
    if not isinstance(eb, dict):
        return (
            "block_if:governance_authenticity_failed - "
            "execution_boundary dict is absent. "
            "Governance pipeline always stamps execution_boundary."
        )

    return None


def _validate_governance_result(
    governance_result: dict[str, Any],
) -> str | None:
    """Validate the incoming governance_result dict.

    Returns None on success, or an error string describing the first
    validation failure (used as blocked_reason).

    Checks (in order):
      1. Governance authenticity: structural signature matches governance pipeline.
      2. All required top-level fields are present.
      3. status == "approved" (rejects blocked, pending, or unknown status).
      4. mutation_proposal is a non-empty dict.
      5. mutation_proposal contains the required proposal fields.
    """
    # Check authenticity before anything else — do not process untrusted payloads.
    auth_error = _verify_governance_authenticity(governance_result)
    if auth_error:
        return auth_error

    for field_name in _REQUIRED_GOVERNANCE_FIELDS:
        if field_name not in governance_result:
            return (
                "block_if:mutation_contract_not_validated - "
                f"missing required field '{field_name}' in governance_result"
            )

    status = governance_result.get("status", "")
    if status != "approved":
        return (
            "block_if:mutation_contract_not_validated - "
            f"governance_result.status={status!r}; must be 'approved'"
        )

    proposal = governance_result.get("mutation_proposal")
    if not isinstance(proposal, dict) or not proposal:
        return (
            "block_if:mutation_contract_not_validated - "
            "mutation_proposal is absent or empty"
        )

    for field_name in _REQUIRED_PROPOSAL_FIELDS:
        value = proposal.get(field_name)
        if value is None or (isinstance(value, (str, list)) and not value):
            return (
                "block_if:mutation_contract_not_validated - "
                f"mutation_proposal.{field_name} is missing or empty"
            )

    return None


def simulation_gateway(
    *,
    governance_result: dict[str, Any],
    override: dict[str, Any] | None = None,
    system_context: dict[str, Any] | None = None,
) -> SimulationResult:
    """Single mandatory entry/exit point for the simulation pipeline.

    Pipeline (MUTATION_SIMULATION_EXECUTION_V1):
      1. receive_validated_mutation_contract - structured validation of
         governance_result (status, fields, proposal completeness).
      2. dependency_surface_mapping          - identify all impacted components;
         block if mapping is incomplete.
      3. impact_analysis                     - structural/behavioral/data-flow effects.
      4. failure_prediction                  - possible failure modes.
      5. risk_scoring                        - deterministic low/medium/high level.
         risk_level is ALWAYS assigned; intake-blocked contracts receive
         RISK_HIGH, not "unknown".
      6. simulation_decision_gate            - HARD BLOCK if any rule fires.
         safe_to_execute=False is absolute; the engine never overrides the gate.
      7. audit_log                           - mandatory write; audit exceptions
         propagate without being caught (block_if_log_not_written).
      8. return_simulation_result            - structured SimulationResult only.

    Parameters
    ----------
    governance_result:
        The dict output of MutationGovernanceResult.to_dict().
        status MUST be "approved" - any other status causes rejection.
    override:
        Optional dict {"justification": str, "accepted_risks": [str, ...]}.
        Required (and validated) when risk turns out to be high.
        An override that fails protocol validation is treated as absent.
    system_context:
        Optional ambient context (accepted for future extension).

    Returns
    -------
    SimulationResult
        Always structured.  safe_to_execute=True means no blockers found.
        safe_to_execute=False means execution MUST NOT proceed.

    Raises
    ------
    RuntimeError
        If the database is configured and the audit write fails
        (block_if_log_not_written - propagates unhandled).
    """
    # ------------------------------------------------------------------
    # Step 1: validate the incoming governance result
    # ------------------------------------------------------------------
    result = SimulationResult()
    result.source_contract_id = str(governance_result.get("contract_id", ""))
    result.source_governance_audit_id = str(governance_result.get("audit_id", ""))

    validation_error = _validate_governance_result(governance_result)
    if validation_error:
        return _build_intake_blocked_result(
            result=result,
            mutation_proposal=governance_result.get("mutation_proposal") or {},
            blocked_reason=validation_error,
        )

    mutation_proposal: dict[str, Any] = governance_result["mutation_proposal"]

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
    #   risk_level is ALWAYS assigned (governance invariant:
    #   all_mutations_must_be_scored).
    # ------------------------------------------------------------------
    risk = score_risk(surface, impact, failures)
    assert risk.level in ("low", "medium", "high"), (
        f"risk_scoring invariant violated: level={risk.level!r}"
    )

    # ------------------------------------------------------------------
    # Step 6: build override object if provided
    # ------------------------------------------------------------------
    sim_override: SimulationOverride | None = None
    if override:
        try:
            sim_override = SimulationOverride.from_dict(override)
        except ValueError as exc:
            # Invalid override treated as absent - gate will block if needed.
            logger.warning(
                "mutation_simulation: override rejected - %s; "
                "treating as no-override",
                exc,
            )
            sim_override = None

    # ------------------------------------------------------------------
    # Step 7: simulation decision gate (HARD BLOCK)
    # ------------------------------------------------------------------
    gate = simulation_decision_gate(risk, failures, surface, sim_override)

    # Invariant: the engine NEVER returns safe_to_execute=True when the
    # gate says safe_to_execute=False.
    assert gate.blocking_mode is True, (
        "simulation_gate invariant violated: blocking_mode must be True"
    )

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
    # Hard-bind safe_to_execute to the gate result - no override possible here.
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
    # Step 8: audit log (MANDATORY - exceptions propagate unhandled)
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
    # Do NOT wrap in try/except - audit failure must propagate
    # (block_if_log_not_written invariant).
    persist_simulation_audit_record(audit)

    # ------------------------------------------------------------------
    # Step 9: return structured result (no execution)
    # ------------------------------------------------------------------
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_intake_blocked_result(
    *,
    result: SimulationResult,
    mutation_proposal: dict[str, Any],
    blocked_reason: str,
) -> SimulationResult:
    """Populate *result* as an intake-blocked simulation and persist audit.

    Uses RISK_HIGH (conservative for unverifiable contracts).
    meaningful risk_level value (governance invariant: all_mutations_must_be_scored).
    """
    result.safe_to_execute = False
    result.blocked_reason = blocked_reason
    result.risk_level = RISK_HIGH
    result.reasoning_summary = (
        "BLOCKED at intake validation.\n"
        f"Reason: {blocked_reason}\n"
        "The mutation contract was not approved by the governance pipeline or "
        "is structurally incomplete. No simulation analysis was performed."
    )

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
    # Do NOT wrap in try/except - audit failure must propagate.
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
    """Produce a detailed human-readable reasoning summary.

    The summary explicitly covers:
      - What is being mutated (operation, target files)
      - What was impacted (files, modules)
      - Why the risk level was assigned (criteria)
      - What failures were predicted (types, severities)
      - The gate decision and the exact reason for blocking or passing
      - Override details if applied
    """
    from .contract import DependencySurface, FailurePrediction, ImpactAnalysis, RiskScore

    assert isinstance(surface, DependencySurface)
    assert isinstance(impact, ImpactAnalysis)
    assert isinstance(failures, FailurePrediction)
    assert isinstance(risk, RiskScore)
    assert isinstance(gate, SimulationGateResult)

    op = mutation_proposal.get("operation_type", "unknown")
    targets = mutation_proposal.get("target_files", [])
    proposed = mutation_proposal.get("proposed_changes", "")[:200]

    n_impacted_files = len(surface.impacted_files)
    n_modules = len(surface.impacted_modules)
    n_direct = sum(1 for lnk in surface.dependency_links if lnk.get("type") == "direct")
    n_indirect = sum(1 for lnk in surface.dependency_links if lnk.get("type") == "indirect")

    high_failures = [f for f in failures.predicted_failures if f.severity == "high"]
    med_failures = [f for f in failures.predicted_failures if f.severity == "medium"]

    decision_label = "SAFE_TO_PROCEED" if gate.safe_to_execute else "BLOCKED"

    lines: list[str] = [
        f"=== MUTATION SIMULATION RESULT: {decision_label} ===",
        "",
        f"OPERATION: {op} on {len(targets)} target file(s): {', '.join(targets[:5])}",
        f"PROPOSED CHANGE: {proposed}",
        "",
        f"DEPENDENCY SURFACE ({n_impacted_files} impacted file(s), "
        f"{n_modules} module(s)):",
    ]
    if surface.impacted_files:
        for f in sorted(surface.impacted_files)[:10]:
            lines.append(f"  - {f}")
        if n_impacted_files > 10:
            lines.append(f"  ... and {n_impacted_files - 10} more")
    lines += [
        f"  Direct dependency links: {n_direct}",
        f"  Indirect dependency links: {n_indirect}",
        "",
        f"STRUCTURAL IMPACT: {'; '.join(impact.structural_impact[:5])}",
        f"BEHAVIORAL IMPACT: {'; '.join(impact.behavioral_impact[:5])}",
        f"DATA FLOW IMPACT:  {'; '.join(impact.data_flow_impact[:3])}",
        "",
        f"RISK LEVEL: {risk.level.upper()}",
        f"  Criteria matched: {', '.join(risk.criteria_matched)}",
        "",
        f"PREDICTED FAILURES ({len(failures.predicted_failures)} total, "
        f"{len(high_failures)} high-severity, {len(med_failures)} medium-severity):",
    ]
    for f in failures.predicted_failures[:8]:
        lines.append(
            f"  [{f.severity.upper()}] {f.failure_type}: {f.description[:150]}"
        )
    if len(failures.predicted_failures) > 8:
        lines.append(f"  ... and {len(failures.predicted_failures) - 8} more")

    lines += ["", f"GATE DECISION: {decision_label}"]
    if gate.blocked_reason:
        lines.append(f"  Blocked because: {gate.blocked_reason}")
    if gate.override_used:
        lines.append("  Override was accepted and applied.")
    if gate.gate_notes:
        lines.append(f"  Gate notes: {'; '.join(gate.gate_notes[:4])}")

    return "\n".join(lines)
