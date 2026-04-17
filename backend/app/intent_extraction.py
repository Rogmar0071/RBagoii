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
    intent_type: str = "explain"
    truth_requirement: str = "flexible"
    domain_risk: str = "low"
    verification_required: str = "no"
    output_class: str = "synthesis"

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "objective": self.objective,
            "constraints": self.constraints,
            "expected_output_type": self.expected_output_type,
            "intent_type": self.intent_type,
            "truth_requirement": self.truth_requirement,
            "domain_risk": self.domain_risk,
            "verification_required": self.verification_required,
            "output_class": self.output_class,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntentObject":
        return cls(
            domain=data.get("domain", ""),
            objective=data.get("objective", ""),
            constraints=data.get("constraints", []),
            expected_output_type=data.get("expected_output_type", ""),
            intent_type=data.get("intent_type", "explain"),
            truth_requirement=data.get("truth_requirement", "flexible"),
            domain_risk=data.get("domain_risk", "low"),
            verification_required=data.get("verification_required", "no"),
            output_class=data.get("output_class", "synthesis"),
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

    # Layer 1 — intent contract classification for strict governance.
    if any(kw in lower_msg for kw in ["decide", "choose", "select", "recommend"]):
        intent.intent_type = "decide"
    elif any(kw in lower_msg for kw in ["analyze", "compare", "evaluate"]):
        intent.intent_type = "analyze"
    elif any(kw in lower_msg for kw in ["generate", "create", "draft"]):
        intent.intent_type = "generate"
    else:
        intent.intent_type = "explain"

    if any(kw in lower_msg for kw in ["fact", "true", "accurate", "exact", "verify", "source"]):
        intent.truth_requirement = "strict"
    elif any(kw in lower_msg for kw in ["guess", "brainstorm", "idea", "creative"]):
        intent.truth_requirement = "speculative"
    else:
        intent.truth_requirement = "flexible"

    if any(kw in lower_msg for kw in ["medical", "legal", "finance", "security", "compliance"]):
        intent.domain_risk = "high"
    elif any(kw in lower_msg for kw in ["architecture", "migration", "policy", "decision"]):
        intent.domain_risk = "medium"
    else:
        intent.domain_risk = "low"

    intent.verification_required = "yes" if intent.truth_requirement == "strict" else "no"

    # output_class intentionally depends on truth_requirement above.
    if any(kw in lower_msg for kw in ["opinion", "preference", "subjective"]):
        intent.output_class = "opinion"
    elif intent.truth_requirement == "strict":
        intent.output_class = "fact"
    else:
        intent.output_class = "synthesis"

    return intent
