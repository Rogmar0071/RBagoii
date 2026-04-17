"""
backend.app.contract_construction
==================================
PHASE 2 — Contract Construction for DUAL_MODE_GOVERNANCE_AND_INTENT_BINDING_V1
BOUNDARY — CONTRACT_EXECUTION_BOUNDARY_LOCK_V1

Constructs validation contracts from Intent Objects.
Contract fully defines enforcement surface for validation.

Includes CONTRACT VALIDATION GATE to ensure only valid contracts
are used by the mode engine.

OUTPUT:
Contract Object with:
- required_sections: list[str]
- required_elements: list[str]
- validation_rules: list[str]
- output_format: str
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.app.intent_extraction import IntentObject

if TYPE_CHECKING:
    from backend.app.mode_engine import ValidationResult


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


# ---------------------------------------------------------------------------
# CONTRACT_EXECUTION_BOUNDARY_LOCK_V1 — Contract Validation Gate
# ---------------------------------------------------------------------------


def validate_contract(contract: ContractObject | None) -> "ValidationResult":
    """Validate contract structure, consistency, and safety.

    CONTRACT VALIDATION GATE (MANDATORY BOUNDARY):
    This function MUST be called before using any contract in strict_mode.
    No contract may reach validation stages without passing this gate.

    VALIDATION CHECKS:

    1. STRUCTURE
       - required_sections exists and is non-empty
       - validation_rules exists (can be empty list)
       - output_format defined and non-empty

    2. CONSISTENCY
       - no duplicate sections
       - no empty rule definitions

    3. SAFETY
       - contract is not None
       - no undefined fields
       - no malformed structure

    Parameters
    ----------
    contract:
        ContractObject to validate, or None

    Returns
    -------
    ValidationResult
        passed=True if contract is valid
        passed=False with detailed failure reasons if invalid
    """
    # Import here to avoid circular dependency
    from backend.app.mode_engine import ValidationResult

    failed_rules: list[str] = []
    missing_fields: list[str] = []
    corrections: list[str] = []

    # SAFETY CHECK: Contract must exist
    if contract is None:
        return ValidationResult(
            stage="contract_boundary",
            passed=False,
            failed_rules=["contract_is_none"],
            missing_fields=["contract"],
            correction_instructions=[
                "Contract must exist before validation stages can run",
                "strict_mode requires a valid contract to be constructed",
            ],
        )

    # STRUCTURE CHECK 1: required_sections must exist and be non-empty
    if not hasattr(contract, "required_sections"):
        failed_rules.append("missing_required_sections_field")
        missing_fields.append("required_sections")
        corrections.append("Contract must define 'required_sections' field")
    elif not isinstance(contract.required_sections, list):
        failed_rules.append("required_sections_not_list")
        corrections.append("required_sections must be a list")
    elif len(contract.required_sections) == 0:
        failed_rules.append("required_sections_empty")
        missing_fields.append("required_sections")
        corrections.append("required_sections must contain at least one section")

    # STRUCTURE CHECK 2: validation_rules must exist
    if not hasattr(contract, "validation_rules"):
        failed_rules.append("missing_validation_rules_field")
        missing_fields.append("validation_rules")
        corrections.append("Contract must define 'validation_rules' field")
    elif not isinstance(contract.validation_rules, list):
        failed_rules.append("validation_rules_not_list")
        corrections.append("validation_rules must be a list")

    # STRUCTURE CHECK 3: output_format must be defined and non-empty
    if not hasattr(contract, "output_format"):
        failed_rules.append("missing_output_format_field")
        missing_fields.append("output_format")
        corrections.append("Contract must define 'output_format' field")
    elif not isinstance(contract.output_format, str):
        failed_rules.append("output_format_not_string")
        corrections.append("output_format must be a string")
    elif not contract.output_format or not contract.output_format.strip():
        failed_rules.append("output_format_empty")
        missing_fields.append("output_format")
        corrections.append("output_format must be non-empty")

    # CONSISTENCY CHECK 1: No duplicate sections
    if hasattr(contract, "required_sections") and isinstance(contract.required_sections, list):
        seen_sections = set()
        for section in contract.required_sections:
            if section in seen_sections:
                failed_rules.append(f"duplicate_section:{section}")
                corrections.append(f"Duplicate section '{section}' found in required_sections")
            seen_sections.add(section)

    # CONSISTENCY CHECK 2: No empty rule definitions
    if hasattr(contract, "validation_rules") and isinstance(contract.validation_rules, list):
        for rule in contract.validation_rules:
            if not isinstance(rule, str) or not rule.strip():
                failed_rules.append("empty_validation_rule")
                corrections.append("All validation rules must be non-empty strings")
                break

    # SAFETY CHECK: Verify all required fields exist
    required_fields = [
        "required_sections",
        "required_elements",
        "validation_rules",
        "output_format",
    ]
    for field_name in required_fields:
        if not hasattr(contract, field_name):
            failed_rules.append(f"missing_field:{field_name}")
            missing_fields.append(field_name)
            corrections.append(f"Contract must have '{field_name}' field")

    # Return result
    if len(failed_rules) == 0:
        return ValidationResult(
            stage="contract_boundary",
            passed=True,
        )
    else:
        return ValidationResult(
            stage="contract_boundary",
            passed=False,
            failed_rules=failed_rules,
            missing_fields=missing_fields,
            correction_instructions=corrections,
        )
