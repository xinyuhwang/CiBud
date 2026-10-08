from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from cibud.models.common import MetadataSource, new_id, utcnow


class FieldCandidate(BaseModel):
    """One source's value for a bibliographic field. Conflicts keep every candidate (§7.3)."""

    value: Any
    source: MetadataSource
    retrieved_at: datetime = Field(default_factory=utcnow)


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    NOT_FOUND = "not_found"


class ReferenceVerification(BaseModel):
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    retracted: bool = False
    has_correction: bool = False
    # Looked-up records rejected because they describe a different paper (e.g. a wrong DOI).
    rejected_records: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None


class Reference(BaseModel):
    """The single source of truth for bibliographic data. ``id`` and ``citation_key`` are stable."""

    id: str = Field(default_factory=lambda: new_id("ref"))
    project_id: str
    csl: dict[str, Any]  # CSL-JSON item
    citation_key: str
    # Keys the user chose (e.g. from their .bib file) are never regenerated.
    citation_key_locked: bool = False
    field_provenance: dict[str, list[FieldCandidate]] = Field(default_factory=dict)
    verification: ReferenceVerification = Field(default_factory=ReferenceVerification)
    # References the user confirmed are different works, so they are never flagged again.
    distinct_from: list[str] = Field(default_factory=list)
