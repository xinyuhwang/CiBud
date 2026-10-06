"""Durable Postgres-backed job queue (design doc §11.3).

- Idempotent: ``enqueue`` keys jobs by ``inputs_hash``; enqueueing the same work twice
  returns the existing job instead of duplicating it.
- Concurrent: workers claim jobs with ``FOR UPDATE SKIP LOCKED``, so two workers never
  run the same job.
- Retryable: failures are re-queued with exponential backoff until ``max_attempts``.
- Durable: a job whose worker died (lock older than ``lease``) is reclaimed, so a crash
  resumes the work instead of losing it.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cibud.db.tables import JobRow
from cibud.models.common import new_id, utcnow
from cibud.models.job import Job, JobStatus, job_inputs_hash

DEFAULT_LEASE = timedelta(minutes=10)
BACKOFF_BASE = timedelta(seconds=30)


def _to_model(row: JobRow) -> Job:
    return Job.model_validate(row, from_attributes=True)


def enqueue(session: Session, type: str, inputs: dict[str, Any], *, max_attempts: int = 3) -> Job:
    """Queue a job, or return the existing job for identical (type, inputs)."""
    inputs_hash = job_inputs_hash(type, inputs)
    stmt = (
        insert(JobRow)
        .values(
            id=new_id("job"),
            type=type,
            inputs=inputs,
            inputs_hash=inputs_hash,
            status=JobStatus.QUEUED,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=[JobRow.inputs_hash])
    )
    session.execute(stmt)
    row = session.scalars(select(JobRow).where(JobRow.inputs_hash == inputs_hash)).one()
    return _to_model(row)


def claim(
    session: Session,
    worker_id: str,
    *,
    types: list[str] | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> Job | None:
    """Lock and return the oldest runnable job, or None if there is nothing to do."""
    now = utcnow()
    runnable = or_(
        (JobRow.status == JobStatus.QUEUED) & (JobRow.run_after <= now),
        # A RUNNING job whose lease expired belongs to a worker that crashed.
        (JobRow.status == JobStatus.RUNNING) & (JobRow.locked_at < now - lease),
    )
    stmt = select(JobRow).where(runnable)
    if types:
        stmt = stmt.where(JobRow.type.in_(types))
    stmt = stmt.order_by(JobRow.run_after, JobRow.created_at).limit(1)
    row = session.scalars(stmt.with_for_update(skip_locked=True)).first()
    if row is None:
        return None
    row.status = JobStatus.RUNNING
    row.attempts += 1
    row.locked_by = worker_id
    row.locked_at = now
    session.flush()
    return _to_model(row)


def _locked_row(session: Session, job_id: str) -> JobRow:
    return session.scalars(select(JobRow).where(JobRow.id == job_id).with_for_update()).one()


def complete(session: Session, job_id: str, result: dict[str, Any] | None = None) -> None:
    row = _locked_row(session, job_id)
    row.status = JobStatus.SUCCEEDED
    row.result = result
    row.error = None
    row.locked_by = row.locked_at = None


def fail(session: Session, job_id: str, error: str) -> Job:
    """Record a failure: re-queue with backoff, or mark FAILED when attempts are used up."""
    row = _locked_row(session, job_id)
    row.error = error
    row.locked_by = row.locked_at = None
    if row.attempts >= row.max_attempts:
        row.status = JobStatus.FAILED
    else:
        row.status = JobStatus.QUEUED
        row.run_after = utcnow() + BACKOFF_BASE * 2 ** (row.attempts - 1)
    session.flush()
    return _to_model(row)


def retry(session: Session, job_id: str) -> Job:
    """Manually re-run a FAILED job (the "retry" button in §13.9). Resets the attempt count."""
    row = _locked_row(session, job_id)
    if row.status is not JobStatus.FAILED:
        raise ValueError(f"job {job_id} is {row.status}, not failed")
    row.status = JobStatus.QUEUED
    row.attempts = 0
    row.run_after = utcnow()
    session.flush()
    return _to_model(row)


def get(session: Session, job_id: str) -> Job | None:
    row = session.get(JobRow, job_id)
    return _to_model(row) if row else None
