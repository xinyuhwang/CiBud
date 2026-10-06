from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from cibud.models.common import new_id, stable_hash, utcnow


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def job_inputs_hash(type: str, inputs: dict[str, Any]) -> str:
    return stable_hash([type, inputs])


class Job(BaseModel):
    """A unit of background work. ``inputs_hash`` makes retries idempotent (§11.3)."""

    id: str = Field(default_factory=lambda: new_id("job"))
    type: str
    inputs: dict[str, Any]
    inputs_hash: str
    status: JobStatus = JobStatus.QUEUED
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    run_after: datetime = Field(default_factory=utcnow)
    locked_by: str | None = None
    locked_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def create(cls, type: str, inputs: dict[str, Any], **kwargs: Any) -> "Job":
        return cls(type=type, inputs=inputs, inputs_hash=job_inputs_hash(type, inputs), **kwargs)
