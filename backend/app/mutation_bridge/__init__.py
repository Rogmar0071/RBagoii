"""
backend.app.mutation_bridge
=============================
MUTATION_BRIDGE_EXECUTION_V1

The controlled execution gateway between simulation and real-world mutation.
Enforces re-validation, safe execution boundaries, reversible operations,
and audit-complete mutation commits.

Contract ID   : MUTATION_BRIDGE_EXECUTION_V1
Class         : OPERATIONAL
Status        : LOCKED
Reversibility : REVERSIBLE
Depends on    : MODE_ENGINE_EXECUTION_V2,
                MUTATION_GOVERNANCE_EXECUTION_V1,
                MUTATION_SIMULATION_EXECUTION_V1
"""

from .audit import persist_bridge_audit_record
from .contract import (
    BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH,
    BRIDGE_STATUS_BLOCKED,
    BRIDGE_STATUS_EXECUTED,
    BUILD_STATUS_FAILED,
    BUILD_STATUS_PASSED,
    BUILD_STATUS_SKIPPED,
    BridgeAuditRecord,
    BridgeExecutionOverride,
    BridgeResult,
)
from .engine import bridge_gateway
from .gate import BridgeGateResult, bridge_execution_gate
from .revalidation import (
    CHECK_DEPENDENCY_GRAPH,
    CHECK_FILE_HASH_INTEGRITY,
    CHECK_GOVERNANCE_AUDIT_LINKAGE,
    CHECK_NO_CONFLICTS,
    CHECK_TARGET_FILES,
    RuntimeRevalidationResult,
    revalidate_runtime_state,
)

__all__ = [
    # Constants
    "BRIDGE_OVERRIDE_MIN_JUSTIFICATION_LENGTH",
    "BRIDGE_STATUS_BLOCKED",
    "BRIDGE_STATUS_EXECUTED",
    "BUILD_STATUS_FAILED",
    "BUILD_STATUS_PASSED",
    "BUILD_STATUS_SKIPPED",
    "CHECK_DEPENDENCY_GRAPH",
    "CHECK_FILE_HASH_INTEGRITY",
    "CHECK_GOVERNANCE_AUDIT_LINKAGE",
    "CHECK_NO_CONFLICTS",
    "CHECK_TARGET_FILES",
    # Data types
    "BridgeAuditRecord",
    "BridgeExecutionOverride",
    "BridgeGateResult",
    "BridgeResult",
    "RuntimeRevalidationResult",
    # Functions
    "bridge_execution_gate",
    "bridge_gateway",
    "persist_bridge_audit_record",
    "revalidate_runtime_state",
]
