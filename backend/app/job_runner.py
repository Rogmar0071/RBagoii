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
CONTRACT: MQP-CONTRACT:STATE-DRIVEN-EXECUTION-GATE v1.1
"""

from __future__ import annotations


def try_load_job(job_id: str):
    """Attempt to resolve a stateful job record for *job_id*.

    Tries each known job model in order.  Returns the first matching
    record, or ``None`` when *job_id* does not correspond to any
    governed job (non-UUID strings are silently ignored).

    Extending to new job types: add a lookup block below following the
    same pattern — no other changes are required.

    CONTRACT: MQP-CONTRACT:STATE-DRIVEN-EXECUTION-GATE v1.1 §4
    """
    import uuid as _uuid

    # Validate that job_id is a UUID before hitting the database.
    try:
        uid = _uuid.UUID(str(job_id))
    except (ValueError, AttributeError):
        return None

    # --- IngestJob -----------------------------------------------------------
    try:
        from sqlmodel import Session

        from backend.app.database import get_engine
        from backend.app.models import IngestJob

        with Session(get_engine()) as session:
            record = session.get(IngestJob, uid)
            if record is not None:
                return record
    except Exception as exc:
        print(f"WORKER:try_load_job:ingest_error job_id={job_id} err={repr(exc)}")

    # Future job model lookups go here, following the same pattern.

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
    CONTRACT: MQP-CONTRACT:STATE-DRIVEN-EXECUTION-GATE v1.1
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
    # MQP-CONTRACT: STATE-DRIVEN-EXECUTION-GATE v1.1 — HARD LOCK
    #
    # Execution authority follows state ownership, not function identity.
    #
    # If the first argument resolves to a known stateful job record the
    # state machine decides whether execution is allowed.  Jobs without a
    # governing record (non-UUID first arg or unknown model) pass through
    # unimpeded so that non-governed workers are never affected.
    # -----------------------------------------------------------------------
    if args:
        from backend.app.ingest_pipeline import IngestJobState

        job_id = args[0]
        job = try_load_job(job_id)
        if job is not None:
            if job.status in (IngestJobState.FAILED, IngestJobState.SUCCESS):
                print(
                    f"SKIPPED_TERMINAL: job_id={job_id} status={job.status}"
                )
                return None
            if job.status != IngestJobState.QUEUED:
                print(
                    f"SKIPPED_INVALID_STATE: job_id={job_id} status={job.status}"
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
