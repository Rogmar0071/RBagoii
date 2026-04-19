"""
backend.app.job_registry
========================
Single source of truth for all RQ job functions.

Every entry MUST satisfy the import enforcement invariants checked by
``_assert_importable``:

  - Module-level function (no closures, no lambdas)
  - Public name (no leading underscore)
  - Accessible as ``fn.__module__.<fn.__name__>`` by an RQ worker process

CONTRACT: MQP-CONTRACT:RQ_EXECUTION_SPINE_LOCK_V4 §2
"""

from __future__ import annotations

# Analysis job (defined in backend.app.analysis_job_processor)
from backend.app.analysis_job_processor import process_analysis_job

# New unified ingestion pipeline (replaces run_repo_ingestion)
from backend.app.ingest_pipeline import process_ingest_job

# Session extraction job (defined in backend.app.jobs)
from backend.app.jobs import run_extraction_job

# Worker pipeline jobs (defined in backend.app.worker)
from backend.app.worker import (
    run_analyze_optional_step,
    run_analyze_repo_step,
    run_analyze_step,
    run_blueprint,
    run_repo_ingestion,
    run_repo_validation,
)

# ---------------------------------------------------------------------------
# Canonical job registry
# ---------------------------------------------------------------------------
# Keys are the stable job-name strings passed to execute_job / enqueue_job.
# Values MUST be public, module-level callables.

JOB_REGISTRY: dict[str, object] = {
    # Worker pipeline jobs — keys match legacy enqueue_job job_type strings
    "analyze": run_analyze_step,
    "analyze_optional": run_analyze_optional_step,
    "blueprint": run_blueprint,
    "analyze_repo": run_analyze_repo_step,
    "repo_ingestion": run_repo_ingestion,
    # Canonical function-name keys (used by execute_job dispatcher)
    "run_repo_ingestion": run_repo_ingestion,
    "run_repo_validation": run_repo_validation,
    "run_extraction_job": run_extraction_job,
    "process_analysis_job": process_analysis_job,
    # New unified ingestion pipeline
    "process_ingest_job": process_ingest_job,
}
