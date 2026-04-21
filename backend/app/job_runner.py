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
CONTRACT: MQP-CONTRACT:WORKER-EXECUTION-GOVERNANCE-LOCK v1.2
"""

from __future__ import annotations


def try_load_job(job_id: str):
    """Attempt to resolve a stateful job record for *job_id*.

    Tries each known job model in order.  Returns the first matching
    record, or ``None`` when *job_id* does not correspond to any
    governed job (non-UUID strings are silently ignored).

    The returned object has at minimum a ``status`` attribute that is
    guaranteed to be loaded (read within the session) so it remains
    accessible after the session closes.

    Extending to new job types: add a lookup block below following the
    same pattern — no other changes are required.

    CONTRACT: MQP-CONTRACT:STATE-DRIVEN-EXECUTION-GATE v1.1 §4
    """
    try:
        from backend.app.job_lifecycle import load_governed_job

        record, _spec = load_governed_job(job_id)
        if record is not None:
            return record
    except Exception as exc:
        print(f"WORKER:try_load_job:error job_id={job_id} err={repr(exc)}")

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
    CONTRACT: MQP-CONTRACT:WORKER-EXECUTION-GOVERNANCE-LOCK v1.2
    """
    # TRACE_ENTRY — first log, always emitted
    print(f"TRACE_ENTRY: job_name={job_name} args={args}")

    # -----------------------------------------------------------------------
    # MQP-CONTRACT: WORKER-EXECUTION-GOVERNANCE-LOCK v1.2 — STATE GATE FIRST
    #
    # Extract job_id from the first argument (may be None for arg-less jobs).
    # try_load_job handles non-UUID / None values safely and returns None.
    #
    # GOVERNED JOB   — record found → state controls execution.
    # NON-GOVERNED   — no record    → execution allowed; must be logged.
    #
    # This block runs before registry resolution and before any "executing"
    # log so that no blocked job can reach fn(*args) under any path.
    # -----------------------------------------------------------------------
    job_id = args[0] if args else None
    job = try_load_job(job_id)
    if job is not None and getattr(job, "execution_locked", False):
        print(f"REPLAY_BLOCKED: job_id={job_id}")
        return None

    from backend.app.job_lifecycle import claim_governed_job_execution

    gate = claim_governed_job_execution(job_id)
    if gate["state"] == "blocked_locked":
        print(f"REPLAY_BLOCKED: job_id={job_id}")
        return None
    if gate["state"] == "claim_rejected":
        print(f"CLAIM_REJECTED: job_id={job_id}")
        return None
    if gate["state"] == "not_found":
        print(f"NON_GOVERNED_JOB: job_name={job_name}")

    # Lazy import avoids circular-import issues at module load time.
    from backend.app.job_registry import JOB_REGISTRY

    fn = JOB_REGISTRY.get(job_name)
    print(f"WORKER:resolved fn={fn}")
    if not fn:
        print(f"INVALID_JOB_NAME: job_name={job_name}")
        raise RuntimeError("INVALID_JOB_NAME")

    print(f"WORKER:executing job_name={job_name} job_id={job_id}")
    from backend.app.execution_spine import execution_route

    try:
        with execution_route(job_name):
            result = fn(*args)
    except Exception as e:
        import traceback
        print(f"WORKER:error job_name={job_name} err={repr(e)}")
        traceback.print_exc()
        raise
    print(f"WORKER_COMPLETED: job_name={job_name} job_id={job_id}")
    return result
