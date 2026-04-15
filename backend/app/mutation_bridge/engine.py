"""
backend.app.mutation_bridge.engine
=====================================
MUTATION_BRIDGE_EXECUTION_V1 — main bridge execution gateway.

System pipeline:
  receive_simulation_result
    → verify_governance_authenticity   (structural signature of governance pipeline)
    → verify_simulation_integrity      (structural signature of simulation pipeline)
    → revalidate_runtime_state         (3-check re-validation; HARD BLOCK on any failure)
    → execution_gate                   (all 3 conditions + high-risk override)
    → staged_execution                 (simulated: branch, diff, build, snapshot)
    → artifact_generation              (enforce all 4 required artifacts)
    → audit_log                        (mandatory; RuntimeError propagates unhandled)
    → return_bridge_result             (structured BridgeResult ONLY — no free text)

Governance invariants enforced:
  - execution_requires_verified_simulation    safe_to_execute must be True.
  - execution_requires_verified_governance    gate_result.passed must be True.
  - execution_is_reversible                   isolated branch only; no main commit.
  - execution_is_audited                      block_if:audit_write_failure.
  - execution_is_isolated                     no_direct_commit_to_main enforced.

Execution scope (STRICTLY SIMULATED — no real git ops):
  - Branches are named, never created in a real repo.
  - Diffs are synthesised from the mutation proposal.
  - Build validation is deterministic (no subprocess calls).
  - No file writes, no git commits, no pushes, no deployments.

Execution boundary (enforced constants — never relaxed):
  - no_direct_commit_to_main
  - no_auto_merge
  - no_deployment_trigger

Prohibited actions enforced:
  - direct_main_branch_commit           blocked at gate + execution step
  - execution_without_simulation        blocked at gate (sim contract check)
  - execution_without_governance        blocked at gate (governance_passed check)
  - bypass_runtime_validation           revalidation is mandatory and blocking
  - hidden_mutation                     all mutations logged in audit

Depends on:
  - MUTATION_GOVERNANCE_EXECUTION_V1  (verified governance result required)
  - MUTATION_SIMULATION_EXECUTION_V1  (verified simulation result required)
  - MODE_ENGINE_EXECUTION_V2          (mode alignment at governance layer)
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .audit import persist_bridge_audit_record
from .contract import (
    BRIDGE_STATUS_BLOCKED,
    BRIDGE_STATUS_EXECUTED,
    BUILD_STATUS_FAILED,
    BUILD_STATUS_PASSED,
    BridgeAuditRecord,
    BridgeExecutionOverride,
    BridgeResult,
)
from .gate import BridgeGateResult, bridge_execution_gate
from .revalidation import RuntimeRevalidationResult, revalidate_runtime_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structural signature constants
# ---------------------------------------------------------------------------

_EXPECTED_GOVERNANCE_CONTRACT = "MUTATION_GOVERNANCE_EXECUTION_V1"
_EXPECTED_SIMULATION_CONTRACT = "MUTATION_SIMULATION_EXECUTION_V1"

# Fields that only a genuine governance result carries.
_REQUIRED_GOVERNANCE_FIELDS: tuple[str, ...] = (
    "contract_id",
    "governance_contract",
    "status",
    "mutation_proposal",
    "gate_result",
    "audit_id",
    "execution_boundary",
)

# Fields that only a genuine simulation result carries.
_REQUIRED_SIMULATION_FIELDS: tuple[str, ...] = (
    "simulation_id",
    "governance_contract",
    "source_contract_id",
    "source_governance_audit_id",
    "safe_to_execute",
    "risk_level",
    "audit_id",
)

# Valid risk levels from the simulation contract.
_VALID_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})

# Branch name prefix — mutations always go to an isolated branch, never main.
_BRANCH_PREFIX = "mutation/bridge-"

# ---------------------------------------------------------------------------
# Execution boundary constants (enforced — never relaxed)
# ---------------------------------------------------------------------------

_EXECUTION_BOUNDARY: dict[str, bool] = {
    "no_direct_commit_to_main": True,
    "no_auto_merge": True,
    "no_deployment_trigger": True,
}


# ---------------------------------------------------------------------------
# Authentication / integrity helpers
# ---------------------------------------------------------------------------


def _verify_governance_authenticity(
    governance_result: dict[str, Any],
) -> str | None:
    """Verify the result structurally originates from the governance pipeline.

    Checks:
      - governance_contract == "MUTATION_GOVERNANCE_EXECUTION_V1"
      - All required fields present
      - audit_id is a non-empty string
      - gate_result.passed == True
      - execution_boundary is a dict

    Returns None if authentic, or a rejection reason string.
    """
    gc = governance_result.get("governance_contract", "")
    if gc != _EXPECTED_GOVERNANCE_CONTRACT:
        return (
            "block_if:governance_not_verified — "
            f"governance_contract={gc!r}; expected {_EXPECTED_GOVERNANCE_CONTRACT!r}"
        )

    for fname in _REQUIRED_GOVERNANCE_FIELDS:
        if fname not in governance_result:
            return (
                "block_if:governance_not_verified — "
                f"missing required field {fname!r} in governance_result"
            )

    audit_id = governance_result.get("audit_id", "")
    if not isinstance(audit_id, str) or not audit_id.strip():
        return (
            "block_if:governance_not_verified — "
            "audit_id is absent or empty in governance_result"
        )

    gate_result = governance_result.get("gate_result")
    if not isinstance(gate_result, dict) or gate_result.get("passed") is not True:
        return (
            "block_if:governance_not_verified — "
            f"gate_result={gate_result!r}; must be a dict with passed=True"
        )

    eb = governance_result.get("execution_boundary")
    if not isinstance(eb, dict):
        return (
            "block_if:governance_not_verified — "
            "execution_boundary dict is absent in governance_result"
        )

    return None


def _verify_simulation_integrity(
    simulation_result: dict[str, Any],
) -> str | None:
    """Verify the result structurally originates from the simulation pipeline.

    Checks per input_contract.simulation_requirements:
      - governance_contract == "MUTATION_SIMULATION_EXECUTION_V1"
      - All required fields present
      - safe_to_execute == True
      - risk_level in [low, medium, high]
      - audit_id is a non-empty string

    Returns None if valid, or a rejection reason string.
    """
    sim_contract = simulation_result.get("governance_contract", "")
    if sim_contract != _EXPECTED_SIMULATION_CONTRACT:
        return (
            "block_if:simulation_not_verified — "
            f"governance_contract={sim_contract!r}; "
            f"expected {_EXPECTED_SIMULATION_CONTRACT!r}"
        )

    for fname in _REQUIRED_SIMULATION_FIELDS:
        if fname not in simulation_result:
            return (
                "block_if:simulation_not_verified — "
                f"missing required field {fname!r} in simulation_result"
            )

    # Key presence alone is insufficient — an empty value breaks the audit chain.
    sgai = simulation_result.get("source_governance_audit_id", "")
    if not isinstance(sgai, str) or not sgai.strip():
        return (
            "block_if:simulation_not_verified — "
            "source_governance_audit_id is absent or empty in simulation_result; "
            "audit chain integrity cannot be verified"
        )

    safe_to_execute = simulation_result.get("safe_to_execute")
    if safe_to_execute is not True:
        return (
            "block_if:simulation_not_verified — "
            f"simulation_result.safe_to_execute={safe_to_execute!r}; must be True"
        )

    risk_level = simulation_result.get("risk_level", "")
    if risk_level not in _VALID_RISK_LEVELS:
        return (
            "block_if:simulation_not_verified — "
            f"simulation_result.risk_level={risk_level!r}; "
            f"must be one of {sorted(_VALID_RISK_LEVELS)}"
        )

    audit_id = simulation_result.get("audit_id", "")
    if not isinstance(audit_id, str) or not audit_id.strip():
        return (
            "block_if:simulation_not_verified — "
            "audit_id is absent or empty in simulation_result"
        )

    return None


# ---------------------------------------------------------------------------
# Staged execution (SIMULATED — no real git ops, no file writes)
# ---------------------------------------------------------------------------


def _generate_branch_name(bridge_id: str) -> str:
    """Generate a deterministic isolated branch name.

    Constraint: never 'main', 'master', or any protected branch name.
    """
    short_id = bridge_id.replace("-", "")[:12]
    return f"{_BRANCH_PREFIX}{short_id}"


def _generate_diff_patch(
    mutation_proposal: dict[str, Any],
    bridge_id: str,
) -> str:
    """Synthesise a diff patch from the mutation proposal.

    No file writes or git operations are performed.  The patch is a
    deterministic representation of what would be applied in a real
    execution, suitable for human review.
    """
    target_files: list[str] = list(mutation_proposal.get("target_files") or [])
    operation_type: str = str(mutation_proposal.get("operation_type", "unknown"))
    proposed_changes: str = str(mutation_proposal.get("proposed_changes", ""))

    lines: list[str] = [
        "# MUTATION_BRIDGE_EXECUTION_V1 — Simulated Diff",
        f"# bridge_id: {bridge_id}",
        f"# operation: {operation_type}",
        "",
    ]
    for fpath in target_files:
        content_hash = hashlib.sha256(
            f"{bridge_id}:{fpath}:{proposed_changes}".encode()
        ).hexdigest()[:16]
        lines += [
            f"diff --git a/{fpath} b/{fpath}",
            f"--- a/{fpath}",
            f"+++ b/{fpath}",
            "@@ -1,0 +1,1 @@",
            f"+# [bridge:{content_hash}] {proposed_changes[:120]}",
            "",
        ]
    return "\n".join(lines)


def _run_build_validation(
    mutation_proposal: dict[str, Any],
    target_files: list[str],
) -> tuple[str, str | None]:
    """Simulate local build validation deterministically.

    Returns (build_status, failure_reason).

    Rules:
      - If any target file has a restricted path segment → BUILD_STATUS_FAILED.
      - If operation_type is "delete_file" for a file with known downstream
        dependents → BUILD_STATUS_FAILED (conservative).
      - Otherwise → BUILD_STATUS_PASSED.

    No subprocesses are invoked; no external I/O occurs.
    """
    _RESTRICTED = frozenset({".env", "secrets", "infra/credentials"})
    operation_type: str = str(mutation_proposal.get("operation_type", ""))

    for fpath in target_files:
        norm = fpath.replace("\\", "/").lstrip("/")
        for segment in norm.split("/"):
            if segment in _RESTRICTED:
                return (
                    BUILD_STATUS_FAILED,
                    f"build_validation_failed:restricted_path_in_target={fpath!r}",
                )
        for restricted in _RESTRICTED:
            if restricted in norm:
                return (
                    BUILD_STATUS_FAILED,
                    f"build_validation_failed:restricted_path_in_target={fpath!r}",
                )

    if operation_type == "delete_file":
        return (
            BUILD_STATUS_FAILED,
            "build_validation_failed:delete_file_requires_manual_review",
        )

    return BUILD_STATUS_PASSED, None


def _perform_staged_execution(
    mutation_proposal: dict[str, Any],
    bridge_id: str,
) -> tuple[str, str, list[str], str, str | None, list[str]]:
    """Execute the four staged steps (all simulated — no real git ops).

    Steps:
      1. create_isolated_branch  → branch_name
      2. apply_mutation_changes  → diff_patch
      3. run_local_build_validation → build_status
      4. generate_diff_snapshot  → (diff_patch is already the snapshot)

    Returns
    -------
    (branch_name, diff_patch, modified_files_list,
     build_status, build_failure_reason, execution_actions)
    """
    execution_actions: list[str] = []

    # Step 1: create_isolated_branch
    branch_name = _generate_branch_name(bridge_id)
    execution_actions.append(f"create_isolated_branch: {branch_name!r}")

    # Step 2: apply_mutation_changes (simulated)
    target_files: list[str] = list(mutation_proposal.get("target_files") or [])
    diff_patch = _generate_diff_patch(mutation_proposal, bridge_id)
    execution_actions.append(
        f"apply_mutation_changes: {len(target_files)} file(s) — simulated, no file write"
    )

    # Step 3: run_local_build_validation
    build_status, build_failure_reason = _run_build_validation(
        mutation_proposal, target_files
    )
    execution_actions.append(f"run_local_build_validation: {build_status}")

    # Step 4: generate_diff_snapshot
    execution_actions.append("generate_diff_snapshot: diff_patch produced")

    return (
        branch_name,
        diff_patch,
        target_files,
        build_status,
        build_failure_reason,
        execution_actions,
    )


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


def _validate_artifacts(
    branch_name: str,
    diff_patch: str,
    modified_files_list: list[str],
    execution_summary: str,
    target_files: list[str],
) -> str | None:
    """Verify all required artifacts are non-empty and consistent with the contract.

    Checks:
      1. Each artifact field is non-empty.
      2. modified_files_list must match mutation_contract.target_files exactly
         (order-independent set equality).
      3. diff_patch must contain a reference to every target file path.

    Returns None if all checks pass, or a blocked_reason string.
    """
    if not branch_name or not branch_name.strip():
        return "block_if:artifact_missing — branch_name is empty"
    if not diff_patch or not diff_patch.strip():
        return "block_if:artifact_missing — diff_patch is empty"
    if not modified_files_list:
        return "block_if:artifact_missing — modified_files_list is empty"
    if not execution_summary or not execution_summary.strip():
        return "block_if:artifact_missing — execution_summary is empty"

    # modified_files_list must exactly match mutation_contract.target_files.
    if sorted(modified_files_list) != sorted(target_files):
        return (
            "block_if:artifact_inconsistency — "
            f"modified_files_list {sorted(modified_files_list)} does not match "
            f"mutation_contract.target_files {sorted(target_files)}"
        )

    # diff_patch must contain a reference to every target file path.
    for fpath in target_files:
        if fpath not in diff_patch:
            return (
                f"block_if:artifact_inconsistency — "
                f"diff_patch does not reference target file {fpath!r}"
            )

    return None


# ---------------------------------------------------------------------------
# Execution summary builder
# ---------------------------------------------------------------------------


def _build_execution_summary(
    mutation_proposal: dict[str, Any],
    branch_name: str,
    build_status: str,
    gate: BridgeGateResult,
    revalidation: RuntimeRevalidationResult,
    override_used: bool,
    risk_level: str = "",
    override_details: dict[str, Any] | None = None,
) -> str:
    """Produce a structured human-readable execution summary.

    Mandatory fields per contract:
      - mutation scope (operation + target files)
      - risk level
      - decision (EXECUTED / BLOCKED)
      - override details (justification + accepted_risks when applied)
    """
    op = mutation_proposal.get("operation_type", "unknown")
    targets = mutation_proposal.get("target_files") or []
    proposed = str(mutation_proposal.get("proposed_changes", ""))[:200]

    decision_label = "EXECUTED" if gate.passed else "BLOCKED"

    lines: list[str] = [
        f"=== MUTATION BRIDGE RESULT: {decision_label} ===",
        "",
        "MUTATION SCOPE:",
        f"  operation: {op}",
        f"  target_files ({len(targets)}): {', '.join(targets[:5])}",
        f"  proposed_changes: {proposed}",
        "",
        f"RISK LEVEL: {risk_level.upper() if risk_level else 'UNKNOWN'}",
        "",
        f"ISOLATED BRANCH: {branch_name}",
        f"BUILD STATUS: {build_status.upper()}",
        "",
        "RUNTIME REVALIDATION:",
    ]
    for check, detail in (revalidation.check_details or {}).items():
        lines.append(f"  {check}: {detail}")

    lines += [
        "",
        f"GATE DECISION: {decision_label}",
    ]
    if gate.blocked_reason:
        lines.append(f"  Blocked because: {gate.blocked_reason}")
    if gate.gate_notes:
        lines.append(f"  Gate notes: {'; '.join(gate.gate_notes[:6])}")

    lines += [
        "",
        "OVERRIDE:",
        f"  applied: {override_used}",
    ]
    if override_used and override_details:
        justification = override_details.get("justification", "")
        accepted_risks = override_details.get("accepted_risks", [])
        lines.append(f"  justification: {justification}")
        lines.append(
            f"  accepted_risks: {', '.join(str(r) for r in accepted_risks)}"
        )

    lines += [
        "",
        "EXECUTION CONSTRAINTS:",
        "  no_direct_commit_to_main: enforced",
        "  no_auto_merge: enforced",
        "  no_deployment_trigger: enforced",
        "  execution_scope: simulated (no real git ops, no file writes)",
        "",
        "EXECUTION BOUNDARY DECLARATION:",
        "  SIMULATED_EXECUTION_ONLY",
        "  NO_REAL_MUTATION",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bridge gateway
# ---------------------------------------------------------------------------


def bridge_gateway(
    *,
    governance_result: dict[str, Any],
    simulation_result: dict[str, Any],
    override: dict[str, Any] | None = None,
    system_context: dict[str, Any] | None = None,  # noqa: ARG001
) -> BridgeResult:
    """Single mandatory entry/exit point for the bridge execution pipeline.

    Pipeline (MUTATION_BRIDGE_EXECUTION_V1):
      1. receive_simulation_result     — validate non-empty dict inputs.
      2. verify_governance_authenticity — structural signature check.
      3. verify_simulation_integrity   — structural signature + safe_to_execute.
      4. revalidate_runtime_state      — 3-check re-validation; HARD BLOCK on any.
      5. execution_gate                — 5 conditions; HARD BLOCK on any failure.
      6. staged_execution              — simulated; produces 4 artifacts.
      7. artifact_generation           — enforce all artifacts present or BLOCK.
      8. audit_log                     — mandatory write; RuntimeError propagates.
      9. return_bridge_result          — structured BridgeResult ONLY.

    Parameters
    ----------
    governance_result:
        The dict output of MutationGovernanceResult.to_dict().
        Must have status="approved" and gate_result.passed=True.
    simulation_result:
        The dict output of SimulationResult.to_dict().
        Must have safe_to_execute=True.
    override:
        Optional dict with explicit_approval, justification, accepted_risks.
        Required when simulation_result.risk_level == "high".
    system_context:
        Optional ambient context (accepted for future extension; not used).

    Returns
    -------
    BridgeResult
        Always structured.  status="executed" or status="blocked".
        NEVER raises on validation or gate failure — only on audit failure.

    Raises
    ------
    RuntimeError
        If the database is configured and the audit write fails
        (block_if:audit_write_failure — propagates unhandled).
    """
    result = BridgeResult()

    # Pull traceability IDs early so audit always has them.
    result.source_governance_contract_id = str(
        governance_result.get("contract_id", "")
    )
    result.source_simulation_id = str(
        simulation_result.get("simulation_id", "")
    )

    audit = BridgeAuditRecord(
        audit_id=result.audit_id,
        bridge_id=result.bridge_id,
        governance_result=governance_result,
        simulation_result=simulation_result,
    )

    # ------------------------------------------------------------------
    # Step 2: verify_governance_authenticity
    # ------------------------------------------------------------------
    gov_error = _verify_governance_authenticity(governance_result)
    if gov_error:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=gov_error,
            execution_actions=[f"verify_governance_authenticity: FAILED — {gov_error}"],
        )

    # ------------------------------------------------------------------
    # Step 3: verify_simulation_integrity
    # ------------------------------------------------------------------
    sim_error = _verify_simulation_integrity(simulation_result)
    if sim_error:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=sim_error,
            execution_actions=[f"verify_simulation_integrity: FAILED — {sim_error}"],
        )

    # ------------------------------------------------------------------
    # Step 4: revalidate_runtime_state (MANDATORY — HARD BLOCK on any failure)
    # ------------------------------------------------------------------
    revalidation = revalidate_runtime_state(governance_result, simulation_result)
    audit.runtime_validation_result = revalidation.to_dict()

    if not revalidation.passed:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=revalidation.blocked_reason or "runtime_revalidation_failed",
            execution_actions=[
                f"revalidate_runtime_state: FAILED — {revalidation.blocked_reason}"
            ],
        )

    # ------------------------------------------------------------------
    # Step 5: build override object if provided
    # ------------------------------------------------------------------
    bridge_override: BridgeExecutionOverride | None = None
    if override:
        try:
            bridge_override = BridgeExecutionOverride.from_dict(override)
        except ValueError as exc:
            logger.warning(
                "mutation_bridge: override rejected — %s; treating as no-override",
                exc,
            )
            bridge_override = None

    # ------------------------------------------------------------------
    # Step 5 (cont.): execution_gate (HARD BLOCK)
    # ------------------------------------------------------------------
    gate = bridge_execution_gate(
        governance_result=governance_result,
        simulation_result=simulation_result,
        revalidation_result=revalidation,
        override=bridge_override,
    )

    assert gate.blocking_mode is True, (
        "bridge_gate invariant violated: blocking_mode must be True"
    )

    if not gate.passed:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=gate.blocked_reason or "execution_gate_failed",
            execution_actions=[f"execution_gate: FAILED — {gate.blocked_reason}"],
        )

    # ------------------------------------------------------------------
    # Step 6: staged_execution (SIMULATED — no real git ops)
    # ------------------------------------------------------------------
    mutation_proposal: dict[str, Any] = governance_result.get("mutation_proposal") or {}

    (
        branch_name,
        diff_patch,
        modified_files_list,
        build_status,
        build_failure_reason,
        execution_actions,
    ) = _perform_staged_execution(mutation_proposal, result.bridge_id)

    # If build validation failed, block execution.
    if build_failure_reason:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=build_failure_reason,
            execution_actions=execution_actions + [
                f"staged_execution: FAILED — {build_failure_reason}"
            ],
        )

    # ------------------------------------------------------------------
    # Step 7: artifact_generation
    # ------------------------------------------------------------------
    override_details_dict: dict[str, Any] | None = None
    if gate.override_used and bridge_override:
        override_details_dict = bridge_override.to_dict()

    execution_summary = _build_execution_summary(
        mutation_proposal=mutation_proposal,
        branch_name=branch_name,
        build_status=build_status,
        gate=gate,
        revalidation=revalidation,
        override_used=gate.override_used,
        risk_level=str(simulation_result.get("risk_level", "")),
        override_details=override_details_dict,
    )

    target_files: list[str] = list(mutation_proposal.get("target_files") or [])
    artifact_error = _validate_artifacts(
        branch_name=branch_name,
        diff_patch=diff_patch,
        modified_files_list=modified_files_list,
        execution_summary=execution_summary,
        target_files=target_files,
    )
    if artifact_error:
        return _build_blocked_result(
            result=result,
            audit=audit,
            blocked_reason=artifact_error,
            execution_actions=execution_actions + [
                f"artifact_generation: FAILED — {artifact_error}"
            ],
        )

    # All artifacts present — populate result.
    result.status = BRIDGE_STATUS_EXECUTED
    result.branch_name = branch_name
    result.diff_patch = diff_patch
    result.modified_files_list = modified_files_list
    result.build_status = build_status
    result.execution_summary = execution_summary
    result.override_used = gate.override_used

    execution_actions.append("artifact_generation: all required artifacts produced")

    # ------------------------------------------------------------------
    # Step 8: audit_log (MANDATORY — exceptions propagate unhandled)
    # ------------------------------------------------------------------
    audit.execution_actions = execution_actions
    audit.artifacts = {
        "branch_name": result.branch_name,
        "diff_patch": result.diff_patch[:2000],  # truncate for storage
        "modified_files_list": result.modified_files_list,
        "build_status": result.build_status,
        "execution_summary": result.execution_summary[:2000],
    }
    audit.status = result.status
    audit.blocked_reason = result.blocked_reason
    audit.override_used = result.override_used
    audit.override_details = override_details_dict

    # DO NOT wrap in try/except — audit failure must propagate
    # (block_if:audit_write_failure invariant).
    persist_bridge_audit_record(audit)

    # ------------------------------------------------------------------
    # Step 9: return structured result (no free text, no execution)
    # ------------------------------------------------------------------
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_blocked_result(
    *,
    result: BridgeResult,
    audit: BridgeAuditRecord,
    blocked_reason: str,
    execution_actions: list[str],
) -> BridgeResult:
    """Populate *result* as a blocked bridge result and persist the audit record."""
    result.status = BRIDGE_STATUS_BLOCKED
    result.blocked_reason = blocked_reason

    audit.execution_actions = execution_actions
    audit.artifacts = {}
    audit.status = BRIDGE_STATUS_BLOCKED
    audit.blocked_reason = blocked_reason

    # DO NOT wrap in try/except — audit failure must propagate
    # (block_if:audit_write_failure invariant).
    persist_bridge_audit_record(audit)
    return result
