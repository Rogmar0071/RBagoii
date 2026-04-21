from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


def _session():
    import backend.app.database as db_module

    return Session(db_module.get_engine())


def test_reexecution_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.job_registry import JOB_REGISTRY
    from backend.app.job_runner import execute_job
    from backend.app.models import Job
    from backend.app.worker import _update_job

    job_id = uuid.uuid4()
    with _session() as session:
        session.add(Job(id=job_id, folder_id=uuid.uuid4(), type="analyze", status="queued"))
        session.commit()

    calls: list[str] = []

    def _terminal_job(job_id_str: str) -> None:
        calls.append(job_id_str)
        _update_job(job_id_str, status="failed", error="forced failure")

    monkeypatch.setitem(JOB_REGISTRY, "execution_lock_terminal", _terminal_job)

    execute_job("execution_lock_terminal", str(job_id))
    execute_job("execution_lock_terminal", str(job_id))

    with _session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.execution_locked is True
        assert job.execution_attempts == 1
    assert calls == [str(job_id)]


def test_execution_lock_persistence() -> None:
    from backend.app.ingest_pipeline import IngestJobState, _transition
    from backend.app.models import IngestJob

    job_id = uuid.uuid4()
    with _session() as session:
        session.add(
            IngestJob(
                id=job_id,
                kind="file",
                source="test.txt",
                status=IngestJobState.QUEUED,
                blob_data=b"hello",
                blob_mime_type="text/plain",
                blob_size_bytes=5,
            )
        )
        session.commit()

    _transition(str(job_id), IngestJobState.FAILED, error="forced failure")

    with _session() as session:
        job = session.get(IngestJob, job_id)
        assert job is not None
        assert job.execution_locked is True


def test_queue_violation_hard_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.worker as worker_module
    from backend.app.models import Job

    monkeypatch.delenv("BACKEND_DISABLE_JOBS", raising=False)

    job_id = uuid.uuid4()
    with _session() as session:
        session.add(Job(id=job_id, folder_id=uuid.uuid4(), type="analyze", status="queued"))
        session.commit()

    mock_queue = MagicMock()
    mock_queue.name = "default:intermediate"
    monkeypatch.setattr(worker_module, "_redis_queue", lambda name="default": mock_queue)

    with pytest.raises(RuntimeError, match="QUEUE_VIOLATION_HARD_STOP"):
        worker_module.enqueue_job(str(job_id), "analyze")


def test_atomic_claim_allows_single_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.job_registry import JOB_REGISTRY
    from backend.app.job_runner import execute_job
    from backend.app.models import Job

    job_id = uuid.uuid4()
    with _session() as session:
        session.add(Job(id=job_id, folder_id=uuid.uuid4(), type="analyze", status="queued"))
        session.commit()

    calls: list[str] = []

    def _claimed_once(job_id_str: str) -> None:
        sleep(0.1)
        calls.append(job_id_str)

    monkeypatch.setitem(JOB_REGISTRY, "execution_lock_atomic", _claimed_once)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(execute_job, "execution_lock_atomic", str(job_id)),
            executor.submit(execute_job, "execution_lock_atomic", str(job_id)),
        ]
        for future in futures:
            future.result()

    with _session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.execution_attempts == 1
        assert job.status == "running"
    assert calls == [str(job_id)]


def test_multiple_worker_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.job_registry import JOB_REGISTRY
    from backend.app.job_runner import execute_job
    from backend.app.models import Job

    job_id = uuid.uuid4()
    with _session() as session:
        session.add(Job(id=job_id, folder_id=uuid.uuid4(), type="analyze", status="queued"))
        session.commit()

    calls: list[str] = []

    def _non_terminal_job(job_id_str: str) -> None:
        calls.append(job_id_str)

    monkeypatch.setitem(JOB_REGISTRY, "execution_lock_running", _non_terminal_job)

    execute_job("execution_lock_running", str(job_id))
    execute_job("execution_lock_running", str(job_id))

    with _session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "running"
        assert job.execution_attempts == 1
    assert calls == [str(job_id)]


def test_direct_execution_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.worker import run_analyze_step

    monkeypatch.setenv("BACKEND_DISABLE_JOBS", "0")

    with pytest.raises(RuntimeError, match="DIRECT_EXECUTION_BLOCKED"):
        run_analyze_step(str(uuid.uuid4()))
