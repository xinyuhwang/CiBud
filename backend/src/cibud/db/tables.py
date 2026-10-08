"""Database tables (design doc §6).

Pydantic models in ``cibud.models`` are the domain types; these tables are their storage.
Nested structures (CSL-JSON, provenance, document trees) are JSONB.
"""

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cibud.models.claim import Verdict
from cibud.models.common import EvidenceLevel
from cibud.models.job import JobStatus
from cibud.models.paper import PaperState
from cibud.models.relevance import RelevanceLevel

# Deterministic constraint names so Alembic migrations are stable.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JsonDict = dict[str, Any]
JsonList = list[Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        JsonDict: JSON().with_variant(JSONB(), "postgresql"),
        JsonList: JSON().with_variant(JSONB(), "postgresql"),
        datetime: DateTime(timezone=True),
    }


def _enum(cls: type, name: str) -> Enum:
    # Stored as VARCHAR + CHECK rather than a native Postgres ENUM, so adding a value
    # is a plain migration instead of ALTER TYPE.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ProjectRow(Timestamps, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    default_style: Mapped[str] = mapped_column(String(128))
    settings: Mapped[JsonDict] = mapped_column(default=dict)


class ResearchProfileRow(Base):
    """Every profile version is kept; relevance results are keyed by version."""

    __tablename__ = "research_profiles"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    data: Mapped[str] = mapped_column(Text)
    contribution: Mapped[str] = mapped_column(Text)
    differentiation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReferenceRow(Timestamps, Base):
    __tablename__ = "references"
    __table_args__ = (
        UniqueConstraint("project_id", "citation_key", name="uq_references_project_key"),
        Index("ix_references_project_doi", "project_id", "doi"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    citation_key: Mapped[str] = mapped_column(String(128))
    citation_key_locked: Mapped[bool] = mapped_column(default=False, server_default="false")
    # Lower-cased copy of csl["DOI"], indexed for deduplication lookups.
    doi: Mapped[str | None] = mapped_column(String(256))
    csl: Mapped[JsonDict]
    field_provenance: Mapped[JsonDict] = mapped_column(default=dict)
    verification: Mapped[JsonDict] = mapped_column(default=dict)
    distinct_from: Mapped[JsonList] = mapped_column(default=list, server_default="[]")


class PaperRow(Timestamps, Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    reference_id: Mapped[str] = mapped_column(
        ForeignKey("references.id", ondelete="CASCADE"), unique=True
    )
    source_files: Mapped[JsonList] = mapped_column(default=list)
    extracted_text_ref: Mapped[str | None] = mapped_column(Text)
    evidence_level: Mapped[EvidenceLevel] = mapped_column(_enum(EvidenceLevel, "evidence_level"))
    state: Mapped[PaperState] = mapped_column(_enum(PaperState, "paper_state"))
    issues: Mapped[JsonList] = mapped_column(default=list)


class EvidencePassageRow(Base):
    __tablename__ = "evidence_passages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int]  # position in the paper's canonical text
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None]
    section: Mapped[str | None] = mapped_column(Text)
    char_start: Mapped[int]
    char_end: Mapped[int]
    bboxes: Mapped[JsonList] = mapped_column(default=list)
    # Embedding and full-text columns are added with the retrieval step, once the
    # embedding model (and therefore the vector dimension) is chosen.


class RelevanceAssessmentRow(Base):
    __tablename__ = "relevance_assessments"
    __table_args__ = (
        UniqueConstraint("paper_id", "profile_version", name="uq_relevance_paper_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    profile_version: Mapped[int]
    relationships: Mapped[JsonList]
    level: Mapped[RelevanceLevel] = mapped_column(_enum(RelevanceLevel, "relevance_level"))
    reasons: Mapped[JsonList]
    evidence_ids: Mapped[JsonList] = mapped_column(default=list)
    cautions: Mapped[JsonList] = mapped_column(default=list)
    potential_use: Mapped[JsonList] = mapped_column(default=list)
    provenance: Mapped[JsonDict]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DocumentRow(Timestamps, Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[JsonDict]


class ClaimRow(Base):
    __tablename__ = "claims"
    __table_args__ = (
        # "Which claims cite reference X?" -> mark them stale when X is edited or removed.
        Index("ix_claims_reference_ids", "reference_ids", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    node_path: Mapped[str] = mapped_column(Text)
    sentence_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    reference_ids: Mapped[JsonList]
    evidence_ids: Mapped[JsonList] = mapped_column(default=list)
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict, "verdict"))
    supporting_span: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    validated_at: Mapped[datetime | None]
    provenance: Mapped[JsonDict | None]


class JobRow(Timestamps, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # The queue poll: oldest runnable job first.
        Index("ix_jobs_runnable", "status", "run_after"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[JsonDict]
    inputs_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus, "job_status"))
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None]
    result: Mapped[JsonDict | None]
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
