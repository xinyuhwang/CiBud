from enum import StrEnum

from pydantic import BaseModel, Field

from cibud.models.common import EvidenceLevel, new_id


class PaperState(StrEnum):
    """Per-paper lifecycle (design doc §11.1)."""

    IMPORTED = "imported"
    EXTRACTING = "extracting"
    METADATA_ONLY = "metadata_only"
    NEEDS_ATTENTION = "needs_attention"
    METADATA_REVIEW = "metadata_review"
    ANALYZING = "analyzing"
    RELEVANCE_REVIEW = "relevance_review"
    APPROVED = "approved"
    EXCLUDED = "excluded"


class Paper(BaseModel):
    id: str = Field(default_factory=lambda: new_id("paper"))
    project_id: str
    reference_id: str
    source_files: list[str] = Field(default_factory=list)  # object-store keys
    extracted_text_ref: str | None = None  # object-store key of GROBID TEI
    evidence_level: EvidenceLevel = EvidenceLevel.METADATA_ONLY
    state: PaperState = PaperState.IMPORTED
    issues: list[str] = Field(default_factory=list)
