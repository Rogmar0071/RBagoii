"""
backend.app.shared.hash_contract
==================================
Single source of truth for the proposal-level hash input template.

Import HASH_INPUT_TEMPLATE wherever a deterministic fingerprint of a
mutation proposal file entry is required so that the same hash is produced
at every pipeline stage, preventing drift between independently maintained
copies of the same logic.
"""
from __future__ import annotations

# Template for computing a SHA-256 fingerprint that encodes the governance
# contract ID, the target file path, and the proposed change description.
# INVARIANT: never modify this string without updating every call site.
HASH_INPUT_TEMPLATE = "proposal:{contract_id}:{fpath}:{proposed_changes}"
