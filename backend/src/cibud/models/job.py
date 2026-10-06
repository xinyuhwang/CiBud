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


class Job(BaseModel):
    """A unit of background work. ``inputs_hash`` makes retries idempotent (§11.3)."""

    id: str = Field(default_factory=lambda: new_id("job"))
    type: str
    inputs: dict[str, Any]
    inputs_hash: str
    model: str | None = None
    prompt_version: str | None = None
    status: JobStatus = JobStatus.QUEUED
    attempts: int = Field(default=0, ge=0)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def create(cls, type: str, inputs: dict[str, Any], **kwargs: Any) -> "Job":
        return cls(type=type, inputs=inputs, inputs_hash=stable_hash([type, inputs]), **kwargs)
