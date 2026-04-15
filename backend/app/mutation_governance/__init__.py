"""
backend.app.mutation_governance
================================
MUTATION_GOVERNANCE_EXECUTION_V1

Governs all AI-generated mutation proposals by enforcing structured mutation
contracts, validation, and execution gating.  No mutation may be executed
without passing the full governance pipeline.

Contract ID   : MUTATION_GOVERNANCE_EXECUTION_V1
Class         : GOVERNANCE
Status        : LOCKED
Reversibility : FORWARD_ONLY
Depends on    : MODE_ENGINE_EXECUTION_V2
"""

from .audit import persist_mutation_audit_record
from .contract import (
    MutationContract,
    MutationGovernanceAuditRecord,
    MutationValidationResult,
    OperationType,
)
from .engine import MutationGovernanceResult, mutation_governance_gateway
from .gate import GateResult, mutation_enforcement_gate
from .validation import (
    ALLOWED_PATH_PREFIXES,
    RESTRICTED_PATHS,
    stage_1_structural_validation,
    stage_2_logical_validation,
    stage_3_scope_validation,
)

__all__ = [
    "ALLOWED_PATH_PREFIXES",
    "GateResult",
    "MutationContract",
    "MutationGovernanceAuditRecord",
    "MutationGovernanceResult",
    "MutationValidationResult",
    "OperationType",
    "RESTRICTED_PATHS",
    "mutation_enforcement_gate",
    "mutation_governance_gateway",
    "persist_mutation_audit_record",
    "stage_1_structural_validation",
    "stage_2_logical_validation",
    "stage_3_scope_validation",
]
