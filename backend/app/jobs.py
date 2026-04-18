"""
backend.app.jobs
================
Public, module-level job functions for RQ (Redis Queue) workers.

All functions in this module:
- Are module-level (no closures, no lambdas, no nesting)
- Have a public name (no leading underscore)
- Are registered at a stable import path
- Can be safely serialised and deserialised by an RQ worker process

CONTRACT: MQP-CONTRACT:RQ_RUNTIME_STABILITY_AND_STATE_TRUTH_V3 §2
"""

from __future__ import annotations


def run_extraction_job(session_id: str) -> None:
    """Run extraction + preview for *session_id*, updating status.json.

    This is the public, importable entry point that RQ workers call.  It
    delegates to the private implementation in ``backend.app.main`` so that
    all existing session-directory helpers, status writers, and environment
    configuration remain in one place.

    CONTRACT: MQP-CONTRACT:RQ_RUNTIME_STABILITY_AND_STATE_TRUTH_V3 §2
    - Module-level function (importable by RQ worker)
    - Public name — no underscore prefix
    - Stable import path: backend.app.jobs.run_extraction_job
    """
    from backend.app.main import _run_extraction

    _run_extraction(session_id)
