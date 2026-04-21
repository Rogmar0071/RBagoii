from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_ACTIVE_EXECUTION_JOB: ContextVar[str | None] = ContextVar(
    "ACTIVE_EXECUTION_JOB",
    default=None,
)


@contextmanager
def execution_route(job_name: str) -> Iterator[None]:
    token = _ACTIVE_EXECUTION_JOB.set(job_name)
    try:
        yield
    finally:
        _ACTIVE_EXECUTION_JOB.reset(token)


def require_execute_job_route(job_name: str) -> None:
    if os.environ.get("BACKEND_DISABLE_JOBS", "0") == "1":
        return
    if _ACTIVE_EXECUTION_JOB.get() != job_name:
        raise RuntimeError("DIRECT_EXECUTION_BLOCKED")


def is_execute_job_route(job_name: str) -> bool:
    return _ACTIVE_EXECUTION_JOB.get() == job_name
