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
"""

from __future__ import annotations


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
    """
    print(f"WORKER:execute_job:start job_name={job_name} args={args}")
    # Lazy import avoids circular-import issues at module load time.
    from backend.app.job_registry import JOB_REGISTRY

    fn = JOB_REGISTRY.get(job_name)
    print(f"WORKER:resolved fn={fn}")
    if not fn:
        print(f"WORKER:invalid_job_name={job_name}")
        raise RuntimeError("INVALID_JOB_NAME")
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
