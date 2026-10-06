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


_S = PaperState

# Allowed transitions (design doc §11.1, Figure 2). Beyond the happy path:
# - DOI/BibTeX imports with no legally accessible full text go straight to MetadataOnly.
# - A failed extraction or lookup goes to NeedsAttention and can be retried (§13.9).
# - Uploading a PDF later moves a MetadataOnly paper back into extraction (§7.2).
# - MetadataOnly papers skip analysis but can still be reviewed, approved, and cited.
# - Decisions can be revised, and a research-profile change sends papers back to review.
TRANSITIONS: dict[PaperState, frozenset[PaperState]] = {
    _S.IMPORTED: frozenset({_S.EXTRACTING, _S.METADATA_ONLY, _S.NEEDS_ATTENTION}),
    _S.EXTRACTING: frozenset({_S.METADATA_REVIEW, _S.METADATA_ONLY, _S.NEEDS_ATTENTION}),
    _S.METADATA_ONLY: frozenset({_S.EXTRACTING, _S.RELEVANCE_REVIEW, _S.NEEDS_ATTENTION}),
    _S.NEEDS_ATTENTION: frozenset({_S.EXTRACTING, _S.METADATA_REVIEW, _S.METADATA_ONLY}),
    _S.METADATA_REVIEW: frozenset({_S.ANALYZING, _S.NEEDS_ATTENTION}),
    _S.ANALYZING: frozenset({_S.RELEVANCE_REVIEW, _S.NEEDS_ATTENTION}),
    _S.RELEVANCE_REVIEW: frozenset({_S.APPROVED, _S.EXCLUDED}),
    _S.APPROVED: frozenset({_S.EXCLUDED, _S.RELEVANCE_REVIEW}),
    _S.EXCLUDED: frozenset({_S.APPROVED, _S.RELEVANCE_REVIEW}),
}


class InvalidTransition(ValueError):
    pass


def check_transition(current: PaperState, target: PaperState) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"paper cannot move from {current} to {target}")


class Paper(BaseModel):
    id: str = Field(default_factory=lambda: new_id("paper"))
    project_id: str
    reference_id: str
    source_files: list[str] = Field(default_factory=list)  # object-store keys
    extracted_text_ref: str | None = None  # object-store key of GROBID TEI
    evidence_level: EvidenceLevel = EvidenceLevel.METADATA_ONLY
    state: PaperState = PaperState.IMPORTED
    issues: list[str] = Field(default_factory=list)
