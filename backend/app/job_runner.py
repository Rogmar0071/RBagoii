"""
backend.app.job_runner
======================
Public RQ entry point for all background jobs.

The queue ALWAYS stores the stable string path:

    "backend.app.job_runner.execute_job"

rather than pickling a function object.  The worker resolves the actual
function at execution time via ``JOB_REGISTRY``, so no ``AttributeError``
or ``DeserializationError`` can occur due to renamed or moved functions.

CONTRACT: MQP-CONTRACT:RQ_EXECUTION_SPINE_LOCK_V4 §5
CONTRACT: MQP-CONTRACT:WORKER-EXECUTION-GATE v1.0
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Ingest-job names that carry an IngestJob record identified by their first
# argument (job_id).  Terminal-state validation is enforced for these jobs
# before the registered function is ever called.
# ---------------------------------------------------------------------------
_INGEST_JOB_NAMES: frozenset[str] = frozenset({"process_ingest_job"})


def _load_ingest_job(job_id: str):
    """Load an IngestJob from the database.  Returns None on failure."""
    try:
        import uuid as _uuid

        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            return session.get(IngestJob, _uuid.UUID(job_id))
    except Exception:
        return None


def execute_job(job_name: str, *args) -> object:
    """Resolve *job_name* via the registry and execute it with *args*.

    This is the ONLY function enqueued directly into RQ.  Storing its
    fully-qualified string path (``backend.app.job_runner.execute_job``)
    keeps the queue stable across code deployments: even if a job
    function moves, only the registry needs updating.

    Parameters
    ----------
    job_name:
        Key in ``JOB_REGISTRY`` — e.g. ``"analyze"``, ``"run_extraction_job"``.
    *args:
        Positional arguments forwarded to the resolved job function
        (typically just ``job_id: str``).

    Raises
    ------
    RuntimeError
        If *job_name* is not registered.

    CONTRACT: MQP-CONTRACT:RQ_EXECUTION_SPINE_LOCK_V4 §5
    CONTRACT: MQP-CONTRACT:WORKER-EXECUTION-GATE v1.0
    """
    print(f"WORKER:execute_job:start job_name={job_name} args={args}")
    # Lazy import avoids circular-import issues at module load time.
    from backend.app.job_registry import JOB_REGISTRY

    fn = JOB_REGISTRY.get(job_name)
    print(f"WORKER:resolved fn={fn}")
    if not fn:
        print(f"WORKER:invalid_job_name={job_name}")
        raise RuntimeError("INVALID_JOB_NAME")

    # -----------------------------------------------------------------------
    # MQP-CONTRACT: WORKER-EXECUTION-GATE v1.0 — HARD LOCK
    #
    # For ingest jobs the first argument is always the job_id.  Load the
    # IngestJob record and refuse execution if the job is already in a
    # terminal state (FAILED or SUCCESS).  The state machine — not the
    # worker — decides whether execution is allowed.
    # -----------------------------------------------------------------------
    if job_name in _INGEST_JOB_NAMES and args:
        from backend.app.ingest_pipeline import IngestJobState

        job_id = args[0]
        job = _load_ingest_job(job_id)
        if job is not None:
            if job.status in (IngestJobState.FAILED, IngestJobState.SUCCESS):
                print(
                    f"SKIPPED_TERMINAL: job_id={job_id} "
                    f"status={job.status} "
                    f"reason=job_already_in_terminal_state"
                )
                return None
            if job.status != IngestJobState.QUEUED:
                print(
                    f"SKIPPED_TERMINAL: job_id={job_id} "
                    f"status={job.status} "
                    f"reason=job_not_in_queued_state"
                )
                return None

    print(f"WORKER:executing {job_name}")
    try:
        result = fn(*args)
    except Exception as e:
        import traceback
        print(f"WORKER:error job_name={job_name} err={repr(e)}")
        traceback.print_exc()
        raise
    print(f"WORKER:completed {job_name}")
    return result
