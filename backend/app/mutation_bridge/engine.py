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
    """Produce a structured human-readable execution summary."""
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
