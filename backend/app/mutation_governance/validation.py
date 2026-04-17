"""
backend.app.mutation_governance.validation
===========================================
Three-stage mutation contract validation pipeline.

Stage 1 — Structural : all_required_fields_present, operation_type_valid
Stage 2 — Logical    : assumptions_explicit, alternatives_present,
                       confidence_present, risks_present
Stage 3 — Scope      : target_files_within_allowed_scope,
                       no_protected_paths_modified

Constraints enforced across all stages:
  - no_empty_fields
  - no_undeclared_assumptions
  - no_single_path_bias
"""

from __future__ import annotations

from .contract import MutationContract, MutationValidationResult

# ---------------------------------------------------------------------------
# File scope configuration (file_scope_control)
# ---------------------------------------------------------------------------

ALLOWED_PATH_PREFIXES: tuple[str, ...] = (
    "backend/",
    "android/",
    "scripts/",
)

RESTRICTED_PATHS: tuple[str, ...] = (
    ".env",
    "secrets/",
    "infra/credentials/",
)

# Recognised categorical confidence values (case-insensitive substring match).
_VALID_CATEGORICAL_CONFIDENCE: frozenset[str] = frozenset(
    {"low", "medium", "high", "very low", "very high"}
)


# ---------------------------------------------------------------------------
# Stage 1 — Structural validation
# ---------------------------------------------------------------------------


def stage_1_structural_validation(
    contract: MutationContract,
) -> MutationValidationResult:
    """Validate all required fields are present and non-empty, and that
    ``operation_type`` is one of the declared valid values.

    Checks:
      - all_required_fields_present
      - operation_type_valid
      - no_empty_fields (list and string fields must be non-empty)
    """
    failed: list[str] = []
    corrections: list[str] = []

    # Scalar string fields must be non-empty.
    string_fields: list[tuple[str, str]] = [
        ("operation_type", contract.operation_type),
        ("proposed_changes", contract.proposed_changes),
    ]
    for name, value in string_fields:
        if not (isinstance(value, str) and value.strip()):
            failed.append(f"missing_or_empty_field:{name}")
            corrections.append(f"Field '{name}' must be present and non-empty")

    # List fields must be non-empty lists.
    list_fields: list[tuple[str, list]] = [
        ("target_files", contract.target_files),
        ("assumptions", contract.assumptions),
        ("alternatives", contract.alternatives),
        ("risks", contract.risks),
        ("missing_data", contract.missing_data),
    ]
    for name, value in list_fields:
        if not (isinstance(value, list) and value):
            failed.append(f"missing_or_empty_field:{name}")
            corrections.append(f"Field '{name}' must be a non-empty list")

    # Confidence must not be None / empty string.
    conf = contract.confidence
    if conf is None or (isinstance(conf, str) and not conf.strip()):
        failed.append("missing_or_empty_field:confidence")
        corrections.append("Field 'confidence' must be present and non-empty")

    # target_files must be a list of non-empty strings.
    if isinstance(contract.target_files, list):
        for path in contract.target_files:
            if not (isinstance(path, str) and path.strip()):
                failed.append("target_files:blank_entry")
                corrections.append("All entries in 'target_files' must be non-empty path strings")
                break

    # operation_type must be one of the declared valid values.
    if (
        isinstance(contract.operation_type, str)
        and contract.operation_type
        and contract.operation_type not in MutationContract.VALID_OPERATION_TYPES
    ):
        failed.append(f"invalid_operation_type:{contract.operation_type!r}")
        corrections.append(
            "operation_type must be one of: "
            + ", ".join(sorted(MutationContract.VALID_OPERATION_TYPES))
        )

    return MutationValidationResult(
        stage="structural",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
    )


# ---------------------------------------------------------------------------
# Stage 2 — Logical validation
# ---------------------------------------------------------------------------


def stage_2_logical_validation(
    contract: MutationContract,
) -> MutationValidationResult:
    """Validate logical consistency of the mutation contract.

    Checks:
      - assumptions_explicit          (non-empty list, all entries non-empty strings)
      - alternatives_present          (at least one distinct alternative — no_single_path_bias)
      - confidence_present_and_valid  (numeric [0,1] or categorical low/medium/high)
      - risks_present                 (non-empty list, all entries non-empty strings)
    """
    failed: list[str] = []
    corrections: list[str] = []

    # assumptions_explicit — no_undeclared_assumptions constraint.
    if isinstance(contract.assumptions, list) and contract.assumptions:
        blank = [a for a in contract.assumptions if not (isinstance(a, str) and a.strip())]
        if blank:
            failed.append("undeclared_assumptions:blank_entries")
            corrections.append(
                "All assumption entries must be non-empty, explicitly stated strings"
            )
    else:
        failed.append("assumptions_not_explicit")
        corrections.append(
            "assumptions must be a non-empty list of explicitly stated assumption strings"
        )

    # alternatives_present — no_single_path_bias constraint.
    if isinstance(contract.alternatives, list) and contract.alternatives:
        blank_alt = [a for a in contract.alternatives if not (isinstance(a, str) and a.strip())]
        if blank_alt:
            failed.append("alternatives_blank_entries")
            corrections.append("All alternative entries must be non-empty strings")
    else:
        failed.append("alternatives_absent:single_path_bias")
        corrections.append(
            "alternatives must list at least one distinct alternative approach "
            "(no_single_path_bias)"
        )

    # confidence_present_and_valid.
    conf = contract.confidence
    is_valid = False
    if isinstance(conf, (int, float)):
        is_valid = 0.0 <= float(conf) <= 1.0
    elif isinstance(conf, str):
        conf_lower = conf.strip().lower()
        try:
            val = float(conf_lower)
            is_valid = 0.0 <= val <= 1.0
        except ValueError:
            is_valid = any(cat in conf_lower for cat in _VALID_CATEGORICAL_CONFIDENCE)
    if not is_valid:
        failed.append("invalid_confidence")
        corrections.append(
            "confidence must be a number between 0 and 1, or one of: low / medium / high"
        )

    # risks_present.
    if isinstance(contract.risks, list) and contract.risks:
        blank_risks = [r for r in contract.risks if not (isinstance(r, str) and r.strip())]
        if blank_risks:
            failed.append("risks_blank_entries")
            corrections.append("All risk entries must be non-empty strings")
    else:
        failed.append("risks_absent")
        corrections.append("risks must be a non-empty list of identified risk strings")

    return MutationValidationResult(
        stage="logical",
        passed=len(failed) == 0,
        failed_rules=failed,
        correction_instructions=corrections,
    )


# ---------------------------------------------------------------------------
# Stage 3 — Scope validation
# ---------------------------------------------------------------------------


def _is_restricted(path: str) -> bool:
    """Return True if *path* matches any restricted path pattern."""
    norm = path.replace("\\", "/")
    for restricted in RESTRICTED_PATHS:
        # Exact match or prefix match or presence anywhere in the path.
        if norm == restricted or norm.startswith(restricted):
            return True
    # Check each path segment for ".env" (catches nested .env files).
    for segment in norm.split("/"):
        if segment == ".env":
            return True
    return False


def _is_allowed(path: str) -> bool:
    """Return True if *path* falls under an allowed path prefix."""
    norm = path.lstrip("/").replace("\\", "/")
    return any(norm.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def stage_3_scope_validation(
    contract: MutationContract,
) -> MutationValidationResult:
    """Validate that all target files are within the allowed scope and that
    no restricted paths are referenced.

    Checks:
      - target_files_within_allowed_scope   (backend/, android/, scripts/)
      - no_protected_paths_modified         (.env, secrets/, infra/credentials/)

    Enforcement:
      - block_outside_scope
      - block_sensitive_access
    """
    failed: list[str] = []
    blocked: list[str] = []
    corrections: list[str] = []

    files = contract.target_files if isinstance(contract.target_files, list) else []
    for path in files:
        if not isinstance(path, str):
            continue

        # Restricted paths have priority over allowed paths.
        if _is_restricted(path):
            failed.append(f"restricted_path:{path}")
            blocked.append(path)
            corrections.append(f"'{path}' is on the restricted list and may not be modified")
        elif not _is_allowed(path):
            failed.append(f"out_of_scope_path:{path}")
            blocked.append(path)
            corrections.append(
                f"'{path}' is outside the allowed scope ({', '.join(ALLOWED_PATH_PREFIXES)})"
            )

    return MutationValidationResult(
        stage="scope",
        passed=len(failed) == 0,
        failed_rules=failed,
        blocked_paths=blocked,
        correction_instructions=corrections,
    )
