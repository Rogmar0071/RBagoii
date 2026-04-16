"""
backend.app.contract_construction
==================================
PHASE 2 — Contract Construction for DUAL_MODE_GOVERNANCE_AND_INTENT_BINDING_V1

Constructs validation contracts from Intent Objects.
Contract fully defines enforcement surface for validation.

OUTPUT:
Contract Object with:
- required_sections: list[str]
- required_elements: list[str]
- validation_rules: list[str]
- output_format: str
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.app.intent_extraction import IntentObject


@dataclass
class ContractObject:
    """Validation contract constructed from Intent.
    
    Fully defines the enforcement surface for validation.
    No generic validation allowed outside contract.
    """
    required_sections: list[str] = field(default_factory=list)
    required_elements: list[str] = field(default_factory=list)
    validation_rules: list[str] = field(default_factory=list)
    output_format: str = ""
    intent_domain: str = ""
    intent_objective: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "required_sections": self.required_sections,
            "required_elements": self.required_elements,
            "validation_rules": self.validation_rules,
            "output_format": self.output_format,
            "intent_domain": self.intent_domain,
            "intent_objective": self.intent_objective,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContractObject":
        return cls(
            required_sections=data.get("required_sections", []),
            required_elements=data.get("required_elements", []),
            validation_rules=data.get("validation_rules", []),
            output_format=data.get("output_format", ""),
            intent_domain=data.get("intent_domain", ""),
            intent_objective=data.get("intent_objective", ""),
        )


def construct_contract(intent: IntentObject) -> ContractObject:
    """Construct validation contract from Intent Object.
    
    The contract must fully define the enforcement surface.
    No generic validation is allowed outside the contract.
    
    Parameters
    ----------
    intent:
        Intent object extracted from user message
        
    Returns
    -------
    ContractObject
        Contract with required_sections, required_elements, validation_rules, output_format
    """
    contract = ContractObject(
        intent_domain=intent.domain,
        intent_objective=intent.objective,
    )
    
    # Define contract based on intent domain and expected output type
    if intent.domain == "code_modification":
        contract.required_sections = [
            "SECTION_INTENT_ANALYSIS",
            "SECTION_MUTATION_CONTRACT",
        ]
        contract.required_elements = [
            "target_files",
            "operation_type",
            "proposed_changes",
            "assumptions",
            "alternatives",
            "confidence",
            "risks",
            "missing_data",
        ]
        contract.validation_rules = [
            "all_required_sections_present",
            "all_required_elements_present",
            "target_files_non_empty",
            "operation_type_valid",
            "assumptions_explicit",
            "alternatives_present",
            "confidence_valid",
            "risks_present",
        ]
        contract.output_format = "structured_json"
        
    elif intent.domain == "analysis":
        contract.required_sections = [
            "ASSUMPTIONS",
            "CONFIDENCE",
            "MISSING_DATA",
        ]
        contract.required_elements = []
        contract.validation_rules = [
            "assumptions_present",
            "confidence_present",
            "missing_data_declared",
        ]
        contract.output_format = "labeled_sections"
        
    elif intent.expected_output_type == "structured_proposal":
        # Generic structured proposal
        contract.required_sections = [
            "ASSUMPTIONS",
            "ALTERNATIVES",
            "CONFIDENCE",
            "MISSING_DATA",
        ]
        contract.required_elements = []
        contract.validation_rules = [
            "assumptions_explicit",
            "alternatives_present",
            "confidence_valid",
            "missing_data_declared",
        ]
        contract.output_format = "labeled_sections"
        
    else:
        # Minimal contract for general queries
        contract.required_sections = [
            "ASSUMPTIONS",
            "CONFIDENCE",
        ]
        contract.required_elements = []
        contract.validation_rules = [
            "assumptions_present",
            "confidence_present",
        ]
        contract.output_format = "labeled_sections"
    
    return contract
