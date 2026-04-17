"""
backend.app.intent_extraction
==============================
PHASE 1 — Intent Extraction for DUAL_MODE_GOVERNANCE_AND_INTENT_BINDING_V1

Extracts structured intent from raw user messages deterministically.
No validation rules, no meaning mutation.

OUTPUT:
Intent Object with:
- domain: str
- objective: str
- constraints: list[str] (optional)
- expected_output_type: str
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IntentObject:
    """Structured intent extracted from user message.

    This is a deterministic extraction only - no validation, no mutation.
    """

    domain: str = ""
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    expected_output_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "objective": self.objective,
            "constraints": self.constraints,
            "expected_output_type": self.expected_output_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntentObject":
        return cls(
            domain=data.get("domain", ""),
            objective=data.get("objective", ""),
            constraints=data.get("constraints", []),
            expected_output_type=data.get("expected_output_type", ""),
        )


def extract_intent(user_message: str) -> IntentObject:
    """Extract structured intent from raw user message.

    This is a deterministic extraction only - no validation rules,
    no meaning mutation. The extracted intent is used to drive
    contract construction in strict_mode.

    Parameters
    ----------
    user_message:
        Raw user message/query

    Returns
    -------
    IntentObject
        Structured intent with domain, objective, constraints, output type
    """
    # For now, this is a simple deterministic extraction
    # In a real implementation, this could use pattern matching or NLP
    # but must remain deterministic and not mutate meaning

    intent = IntentObject()

    # Basic extraction: treat the entire message as the objective
    # More sophisticated extraction can be added later
    intent.objective = user_message.strip()

    # Attempt to infer domain from keywords
    lower_msg = user_message.lower()
    if any(kw in lower_msg for kw in ["file", "code", "function", "class", "module"]):
        intent.domain = "code_modification"
    elif any(kw in lower_msg for kw in ["test", "spec", "verify"]):
        intent.domain = "testing"
    elif any(kw in lower_msg for kw in ["analyze", "explain", "describe"]):
        intent.domain = "analysis"
    else:
        intent.domain = "general"

    # Attempt to infer expected output type
    if any(kw in lower_msg for kw in ["list", "show", "find"]):
        intent.expected_output_type = "list"
    elif any(kw in lower_msg for kw in ["explain", "describe", "what"]):
        intent.expected_output_type = "explanation"
    elif any(kw in lower_msg for kw in ["create", "add", "implement"]):
        intent.expected_output_type = "structured_proposal"
    else:
        intent.expected_output_type = "text"

    return intent
