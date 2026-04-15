"""
backend.app.mutation_simulation
================================
MUTATION_SIMULATION_EXECUTION_V1

Predicts the impact of mutation proposals before execution by analyzing
dependency surfaces, failure modes, and structural risks.
Blocks unsafe mutations before any execution layer is reached.

Contract ID   : MUTATION_SIMULATION_EXECUTION_V1
Class         : GOVERNANCE
Status        : LOCKED
Reversibility : FORWARD_ONLY
Depends on    : MODE_ENGINE_EXECUTION_V2, MUTATION_GOVERNANCE_EXECUTION_V1
"""

from .audit import persist_simulation_audit_record
from .contract import (
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
    SimulationAuditRecord,
    SimulationOverride,
    SimulationResult,
)
from .dependency_surface import map_dependency_surface
from .engine import simulation_gateway
from .failure_prediction import predict_failures
from .gate import SimulationGateResult, simulation_decision_gate
from .impact_analysis import analyze_impact
from .risk_scoring import score_risk

__all__ = [
    # Constants
    "FAILURE_BUILD",
    "FAILURE_CONTRACT_VIOLATION",
    "FAILURE_DEPENDENCY_BREAK",
    "FAILURE_RUNTIME",
    "OVERRIDE_MIN_JUSTIFICATION_LENGTH",
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    # Data types
    "DependencySurface",
    "FailurePrediction",
    "ImpactAnalysis",
    "PredictedFailure",
    "RiskScore",
    "SimulationAuditRecord",
    "SimulationGateResult",
    "SimulationOverride",
    "SimulationResult",
    # Functions
    "analyze_impact",
    "map_dependency_surface",
    "persist_simulation_audit_record",
    "predict_failures",
    "score_risk",
    "simulation_decision_gate",
    "simulation_gateway",
]
