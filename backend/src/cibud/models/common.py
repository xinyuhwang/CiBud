"""Shared types: IDs, enums, and provenance records (design doc §3, §6)."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    """Stable, prefixed ID, e.g. ``ref_8f2a0c1d9e3b``."""
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def stable_hash(payload: Any) -> str:
    """SHA-256 over canonical JSON. Used for job idempotency and claim staleness."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class EvidenceLevel(StrEnum):
    FULL_TEXT = "full_text"
    ABSTRACT_ONLY = "abstract_only"
    METADATA_ONLY = "metadata_only"


class MetadataSource(StrEnum):
    PDF_HEADER = "pdf_header"
    CROSSREF = "crossref"
    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    ARXIV = "arxiv"
    BIBTEX = "bibtex"
    RIS = "ris"
    USER = "user"


class LLMProvenance(BaseModel):
    """Attached to every LLM-produced record so each output is reproducible."""

    model: str
    prompt_name: str
    prompt_version: str
    created_at: datetime = Field(default_factory=utcnow)
