from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXPECTED_GOVERNANCE_CONTRACT = "MUTATION_GOVERNANCE_EXECUTION_V1"
_EXPECTED_SIMULATION_CONTRACT = "MUTATION_SIMULATION_EXECUTION_V1"
_VALID_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
_RESTRICTED_PATH_PREFIXES: tuple[str, ...] = ("secrets/", "infra/credentials/", ".env")


# ---------------------------------------------------------------------------
# Step 1 helper: governance authenticity
# ---------------------------------------------------------------------------


def _verify_governance_authenticity(governance_result: dict[str, Any]) -> str | None:
    """Verify the governance result structurally originates from the governance pipeline.

    Returns None if authentic, or a rejection reason string containing
    'governance_not_verified' on any failure.
    """
    gc = governance_result.get("governance_contract", "")
    if gc != _EXPECTED_GOVERNANCE_CONTRACT:
        return (
            "block_if:governance_not_verified — "
            f"governance_contract={gc!r}; expected {_EXPECTED_GOVERNANCE_CONTRACT!r}"
        )

    audit_id = governance_result.get("audit_id", "")
    if not isinstance(audit_id, str) or not audit_id.strip():
        return (
            "block_if:governance_not_verified — "
            "audit_id is absent or empty"
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
            "execution_boundary dict is absent"
        )

    return None


# ---------------------------------------------------------------------------
# Step 2 helper: simulation integrity
# ---------------------------------------------------------------------------


def _verify_simulation_integrity(simulation_result: dict[str, Any]) -> str | None:
    """Verify the simulation result structurally originates from the simulation pipeline.

    Returns None if valid, or a rejection reason string containing
    'simulation_not_verified' on any failure.
    """
    gc = simulation_result.get("governance_contract", "")
    if gc != _EXPECTED_SIMULATION_CONTRACT:
        return (
            "block_if:simulation_not_verified — "
            f"governance_contract={gc!r}; expected {_EXPECTED_SIMULATION_CONTRACT!r}"
        )

    if not bool(simulation_result.get("safe_to_execute", False)):
        return (
            "block_if:simulation_not_verified — "
            "safe_to_execute is False"
        )

    risk_level = str(simulation_result.get("risk_level", "")).strip()
    if risk_level not in _VALID_RISK_LEVELS:
        return (
            "block_if:simulation_not_verified — "
            f"risk_level={risk_level!r}; must be one of {sorted(_VALID_RISK_LEVELS)}"
        )

    audit_id = simulation_result.get("audit_id", "")
    if not isinstance(audit_id, str) or not audit_id.strip():
        return (
            "block_if:simulation_not_verified — "
            "audit_id is absent or empty"
        )

    if "source_governance_audit_id" not in simulation_result:
        return (
            "block_if:simulation_not_verified — "
            "source_governance_audit_id is absent"
        )
    sgai = simulation_result.get("source_governance_audit_id", "")
    if not isinstance(sgai, str) or not sgai.strip():
        return (
            "block_if:simulation_not_verified — "
            "source_governance_audit_id is empty"
        )

    return None


# ---------------------------------------------------------------------------
# Step 6 helper: staged execution (simulated — no real git ops, no file writes)
# ---------------------------------------------------------------------------


def _perform_staged_execution(
    mutation_proposal: dict[str, Any],
    bridge_id: str,
) -> tuple[str, str, list[str], str, str | None, list[str]]:
    """Perform a staged (fully simulated) execution of the mutation proposal.

    Returns
    -------
    (branch_name, diff_patch, modified_files_list, build_status, fail_reason, actions)
    """
    target_files: list[str] = list(mutation_proposal.get("target_files") or [])
    operation_type: str = str(mutation_proposal.get("operation_type", ""))
    proposed_changes: str = str(mutation_proposal.get("proposed_changes", ""))

    branch_name = f"mutation/bridge-{bridge_id}"
    actions: list[str] = []

    # Build validation: delete_file is prohibited
    if operation_type == "delete_file":
        return (
            branch_name,
            "",
            [],
            BUILD_STATUS_FAILED,
            (
                "block_if:build_validation_failed — "
                "operation_type='delete_file' is not permitted in bridge execution; "
                "file removal breaks the import graph and cannot be safely staged"
            ),
            ["build_validation:FAILED:delete_file_operation"],
        )

    # Build validation: restricted paths
    restricted = [
        f for f in target_files
        if any(f.startswith(r) for r in _RESTRICTED_PATH_PREFIXES)
    ]
    if restricted:
        return (
            branch_name,
            "",
            [],
            BUILD_STATUS_FAILED,
            (
                f"block_if:build_validation_failed — "
                f"target file(s) in restricted paths: {restricted}"
            ),
            ["build_validation:FAILED:restricted_path"],
        )

    # Simulate diff patch (no real file I/O, no subprocess)
    diff_lines: list[str] = []
    for fpath in target_files:
        diff_lines += [
            f"diff --git a/{fpath} b/{fpath}",
            f"--- a/{fpath}",
            f"+++ b/{fpath}",
            "@@ -0,0 +1,1 @@",
            f"+# [BRIDGE SIMULATION] {proposed_changes[:100]}",
        ]
        actions.append(f"staged:update:{fpath}")

    diff_patch = "\n".join(diff_lines)
    modified_files_list = list(target_files)
    actions.append("build_simulation:PASSED")

    return (
        branch_name,
        diff_patch,
        modified_files_list,
        BUILD_STATUS_PASSED,
        None,
        actions,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def bridge_gateway(
    *,
    governance_result: dict[str, Any],
    simulation_result: dict[str, Any],
    override: dict[str, Any] | None = None,
    system_context: dict[str, Any] | None = None,
) -> BridgeResult:
    """Single mandatory entry/exit point for MUTATION_BRIDGE_EXECUTION_V1.

    Pipeline:
      1. _verify_governance_authenticity  — structural signature check
      2. _verify_simulation_integrity     — structural signature + safe_to_execute
      3. revalidate_runtime_state         — runtime re-validation (HARD BLOCK)
      4. parse override                   — invalid override silently → None
      5. bridge_execution_gate            — 5-condition gate (HARD BLOCK)
      6. _perform_staged_execution        — simulated; no real git ops
      7. artifact consistency enforcement — modified_files + diff_patch checks
      8. persist_bridge_audit_record      — mandatory; exceptions propagate
    """
    source_contract_id = str(governance_result.get("contract_id", ""))

    result = BridgeResult(
        source_governance_contract_id=source_contract_id,
        source_simulation_id=str(simulation_result.get("simulation_id", "")),
    )

    risk_level: str = str(simulation_result.get("risk_level", "")).strip()
    mutation_proposal: dict[str, Any] = governance_result.get("mutation_proposal") or {}

    revalidation: RuntimeRevalidationResult = RuntimeRevalidationResult(passed=True)
    gate: BridgeGateResult | None = None
    override_obj: BridgeExecutionOverride | None = None
    override_details: dict[str, Any] | None = None
    actions: list[str] = []

    # ------------------------------------------------------------------
    # Step 1: governance authenticity
    # ------------------------------------------------------------------
    gov_error = _verify_governance_authenticity(governance_result)
    if gov_error:
        result.status = BRIDGE_STATUS_BLOCKED
        result.blocked_reason = gov_error
    else:
        # ------------------------------------------------------------------
        # Step 2: simulation integrity
        # ------------------------------------------------------------------
        sim_error = _verify_simulation_integrity(simulation_result)
        if sim_error:
            result.status = BRIDGE_STATUS_BLOCKED
            result.blocked_reason = sim_error
        else:
            # ------------------------------------------------------------------
            # Step 3: runtime re-validation
            # ------------------------------------------------------------------
            revalidation = revalidate_runtime_state(governance_result, simulation_result)
            if not revalidation.passed:
                result.status = BRIDGE_STATUS_BLOCKED
                result.blocked_reason = revalidation.blocked_reason
            else:
                # ------------------------------------------------------------------
                # Step 4: parse override — invalid override is silently treated as None
                # ------------------------------------------------------------------
                if override is not None:
                    try:
                        override_obj = BridgeExecutionOverride.from_dict(override)
                    except ValueError:
                        override_obj = None

                # ------------------------------------------------------------------
                # Step 5: execution gate
                # ------------------------------------------------------------------
                gate = bridge_execution_gate(
                    governance_result, simulation_result, revalidation, override_obj
                )
                if not gate.passed:
                    result.status = BRIDGE_STATUS_BLOCKED
                    result.blocked_reason = gate.blocked_reason
                    result.override_used = gate.override_used
                else:
                    # ------------------------------------------------------------------
                    # Step 6: staged execution
                    # ------------------------------------------------------------------
                    (
                        branch_name,
                        diff_patch,
                        modified_files_list,
                        build_status,
                        fail_reason,
                        actions,
                    ) = _perform_staged_execution(mutation_proposal, result.bridge_id)

                    result.branch_name = branch_name
                    result.diff_patch = diff_patch
                    result.modified_files_list = modified_files_list
                    result.build_status = build_status

                    if fail_reason or build_status == BUILD_STATUS_FAILED:
                        result.status = BRIDGE_STATUS_BLOCKED
                        result.blocked_reason = (
                            fail_reason or "block_if:build_validation_failed"
                        )
                    else:
                        # ------------------------------------------------------------------
                        # Step 7: artifact consistency
                        # ------------------------------------------------------------------
                        target_files = list(mutation_proposal.get("target_files") or [])

                        # Check A: modified_files_list must match target_files exactly
                        if sorted(modified_files_list) != sorted(target_files):
                            result.status = BRIDGE_STATUS_BLOCKED
                            result.blocked_reason = (
                                "block_if:artifact_inconsistency — "
                                f"modified_files_list={modified_files_list!r} does not "
                                f"match target_files={target_files!r}"
                            )
                        # Check B: diff_patch must reference at least one target file
                        elif target_files and not any(
                            f in diff_patch for f in target_files
                        ):
                            result.status = BRIDGE_STATUS_BLOCKED
                            result.blocked_reason = (
                                "block_if:artifact_inconsistency — "
                                "diff_patch does not reference any target files: "
                                f"{target_files}"
                            )
                        else:
                            result.status = BRIDGE_STATUS_EXECUTED
                            result.override_used = gate.override_used
                            if gate.override_used and override_obj is not None:
                                override_details = override_obj.to_dict()

    # ------------------------------------------------------------------
    # Build execution summary (always — both EXECUTED and BLOCKED)
    # ------------------------------------------------------------------
    gate_for_summary = BridgeGateResult(
        passed=(result.status == BRIDGE_STATUS_EXECUTED),
        blocked_reason=result.blocked_reason,
        gate_notes=gate.gate_notes if gate is not None else [],
        override_used=result.override_used,
    )
    result.execution_summary = _build_execution_summary(
        mutation_proposal=mutation_proposal,
        branch_name=result.branch_name,
        build_status=result.build_status,
        gate=gate_for_summary,
        revalidation=revalidation,
        override_used=result.override_used,
        risk_level=risk_level,
        override_details=override_details,
    )

    # ------------------------------------------------------------------
    # Step 8: mandatory audit — NO try/except; errors must propagate
    # ------------------------------------------------------------------
    audit_record = BridgeAuditRecord(
        audit_id=result.audit_id,
        bridge_id=result.bridge_id,
        governance_result=governance_result,
        simulation_result=simulation_result,
        runtime_validation_result=revalidation.to_dict(),
        execution_actions=actions,
        artifacts={
            "branch_name": result.branch_name,
            "diff_patch": result.diff_patch,
            "modified_files_list": result.modified_files_list,
            "build_status": result.build_status,
        },
        status=result.status,
        blocked_reason=result.blocked_reason,
        override_used=result.override_used,
        override_details=override_details,
    )
    persist_bridge_audit_record(audit_record)

    return result


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
