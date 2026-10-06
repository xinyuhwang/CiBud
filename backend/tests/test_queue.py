from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from cibud.db.session import transaction
from cibud.db.tables import JobRow
from cibud.jobs import queue
from cibud.jobs.worker import Worker
from cibud.models.common import utcnow
from cibud.models.job import Job, JobStatus


def test_enqueue_is_idempotent(session: Session) -> None:
    a = queue.enqueue(session, "extract", {"paper_id": "p1"})
    b = queue.enqueue(session, "extract", {"paper_id": "p1"})
    c = queue.enqueue(session, "extract", {"paper_id": "p2"})
    assert a.id == b.id != c.id


def test_claim_order_and_type_filter(session: Session) -> None:
    first = queue.enqueue(session, "extract", {"n": 1})
    queue.enqueue(session, "analyze", {"n": 2})

    claimed = queue.claim(session, "w1", types=["extract"])
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    assert queue.claim(session, "w1", types=["extract"]) is None


def test_concurrent_workers_never_share_a_job(factory: sessionmaker[Session]) -> None:
    with transaction(factory) as s:
        queue.enqueue(s, "extract", {"n": 1})
        queue.enqueue(s, "extract", {"n": 2})

    # Worker A holds its claim's row lock (transaction still open) while B polls.
    with factory() as a, a.begin():
        job_a = queue.claim(a, "A")
        with factory() as b, b.begin():
            job_b = queue.claim(b, "B")
            with factory() as c, c.begin():
                job_c = queue.claim(c, "C")
    assert job_a is not None and job_b is not None
    assert job_a.id != job_b.id
    assert job_c is None


def test_failure_backs_off_then_gives_up(session: Session) -> None:
    job = queue.enqueue(session, "extract", {"n": 1}, max_attempts=2)

    assert queue.claim(session, "w") is not None
    requeued = queue.fail(session, job.id, "boom")
    assert requeued.status is JobStatus.QUEUED
    assert requeued.run_after > utcnow()
    assert queue.claim(session, "w") is None  # still backing off

    session.execute(update(JobRow).values(run_after=utcnow() - timedelta(seconds=1)))
    assert queue.claim(session, "w") is not None
    failed = queue.fail(session, job.id, "boom again")
    assert failed.status is JobStatus.FAILED
    assert failed.error == "boom again"

    retried = queue.retry(session, job.id)
    assert retried.status is JobStatus.QUEUED
    assert retried.attempts == 0


def test_retry_rejects_jobs_that_have_not_failed(session: Session) -> None:
    job = queue.enqueue(session, "extract", {"n": 1})
    with pytest.raises(ValueError, match="not failed"):
        queue.retry(session, job.id)


def test_expired_lease_is_reclaimed(session: Session) -> None:
    job = queue.enqueue(session, "extract", {"n": 1})
    assert queue.claim(session, "crashed-worker") is not None
    assert queue.claim(session, "w2") is None  # lease still valid

    session.execute(update(JobRow).values(locked_at=utcnow() - timedelta(hours=1)))
    reclaimed = queue.claim(session, "w2")
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.locked_by == "w2"
    assert reclaimed.attempts == 2


async def test_worker_runs_handler_and_records_result(factory: sessionmaker[Session]) -> None:
    async def handler(job: Job) -> dict[str, Any]:
        return {"doubled": job.inputs["n"] * 2}

    with transaction(factory) as s:
        job = queue.enqueue(s, "double", {"n": 21})

    worker = Worker(factory, {"double": handler}, worker_id="w")
    assert await worker.run_once() is not None
    assert await worker.run_once() is None

    with factory() as s:
        done = queue.get(s, job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result == {"doubled": 42}


async def test_worker_records_handler_failure(factory: sessionmaker[Session]) -> None:
    async def handler(job: Job) -> None:
        raise RuntimeError("GROBID unreachable")

    with transaction(factory) as s:
        job = queue.enqueue(s, "extract", {"n": 1})

    await Worker(factory, {"extract": handler}).run_once()

    with factory() as s:
        after = queue.get(s, job.id)
    assert after is not None
    assert after.status is JobStatus.QUEUED
    assert after.error == "RuntimeError: GROBID unreachable"
