"""Annotation format for evaluation corpora (design doc §16).

A corpus is a directory of YAML files:

    profile.yaml     research profile the relevance labels are judged against
    papers.yaml      papers, their identifiers, and hand-verified metadata
    relevance.yaml   relevance label per paper
    findings.yaml    key findings with the passages that support them
    cases.yaml       seeded defects plus valid controls
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from cibud.models.claim import Verdict
from cibud.models.common import EvidenceLevel
from cibud.models.project import ResearchProfile
from cibud.models.relevance import RelationshipType, RelevanceLevel


class PaperMetadata(BaseModel):
    title: str
    authors: list[str] = Field(min_length=1)  # "Family, Given"
    year: int = Field(ge=1800, le=2100)
    venue: str | None = None
    doi: str | None = None


class EvalPaper(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")  # e.g. chen2021attention
    doi: str | None = None
    arxiv_id: str | None = None
    pmcid: str | None = None
    url: str | None = None
    access: EvidenceLevel  # the evidence level the system should reach
    metadata: PaperMetadata | None = None
    # True only after a human has checked ``metadata`` against the paper itself.
    # Prefilled metadata is unverified: scoring the resolver against it would be circular.
    metadata_verified: bool = False

    @model_validator(mode="after")
    def _has_identifier(self) -> Self:
        if not any([self.doi, self.arxiv_id, self.pmcid, self.url]):
            raise ValueError(f"paper {self.key!r} needs at least one of doi/arxiv_id/pmcid/url")
        if self.metadata_verified and self.metadata is None:
            raise ValueError(f"paper {self.key!r} is marked verified but has no metadata")
        return self


class RelevanceLabel(BaseModel):
    paper: str
    relationships: list[RelationshipType] = Field(default_factory=list)  # empty = not relevant
    level: RelevanceLevel
    rationale: str


class PassageRef(BaseModel):
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    quote: str = Field(min_length=1)  # verbatim text from the paper


class Finding(BaseModel):
    id: str
    paper: str
    statement: str
    passages: list[PassageRef] = Field(min_length=1)


class CaseCategory(StrEnum):
    # Reference defects
    WRONG_YEAR = "wrong_year"
    WRONG_DOI = "wrong_doi"
    NEAR_DUPLICATE_TITLE = "near_duplicate_title"
    # Claim defects
    UNSUPPORTED_NUMERIC = "unsupported_numeric"
    PARTIAL_SUPPORT = "partial_support"
    CAUSAL_OVERCLAIM = "causal_overclaim"
    CONFLICTING_RESULTS = "conflicting_results"
    INCOMPATIBLE_COMPARISON = "incompatible_comparison"
    IRRELEVANT_SIMILAR = "irrelevant_similar"
    # Integrity defects
    MISSING_BIB_ENTRY = "missing_bib_entry"
    ORPHANED_REFERENCE = "orphaned_reference"
    # Control: a correct citation that must NOT be flagged
    VALID = "valid"


REFERENCE_CATEGORIES = {
    CaseCategory.WRONG_YEAR,
    CaseCategory.WRONG_DOI,
    CaseCategory.NEAR_DUPLICATE_TITLE,
}
INTEGRITY_CATEGORIES = {CaseCategory.MISSING_BIB_ENTRY, CaseCategory.ORPHANED_REFERENCE}


class Case(BaseModel):
    """One seeded defect or valid control.

    - ``claim`` cases: a sentence citing papers, with the verdict the auditor should return.
    - ``reference`` cases: a corrupted metadata field the reference validator should catch.
    - ``integrity`` cases: a citation/bibliography mismatch the integrity check should catch.
    """

    id: str
    kind: Literal["claim", "reference", "integrity"]
    category: CaseCategory
    description: str = ""
    # claim
    sentence: str | None = None
    cites: list[str] = Field(default_factory=list)
    expected_verdict: Verdict | None = None
    # reference
    paper: str | None = None
    field: str | None = None
    injected_value: str | int | None = None

    @property
    def is_defect(self) -> bool:
        return self.category is not CaseCategory.VALID

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        if self.kind == "claim":
            if not self.sentence or self.expected_verdict is None:
                raise ValueError(f"claim case {self.id!r} needs sentence and expected_verdict")
            if self.category in REFERENCE_CATEGORIES | INTEGRITY_CATEGORIES:
                raise ValueError(f"case {self.id!r}: {self.category} is not a claim category")
            if self.category is CaseCategory.VALID and self.expected_verdict != Verdict.SUPPORTED:
                raise ValueError(f"valid claim case {self.id!r} must expect 'supported'")
        elif self.kind == "reference":
            if not self.paper or not self.field:
                raise ValueError(f"reference case {self.id!r} needs paper and field")
            if self.category not in REFERENCE_CATEGORIES | {CaseCategory.VALID}:
                raise ValueError(f"case {self.id!r}: {self.category} is not a reference category")
        elif self.category not in INTEGRITY_CATEGORIES | {CaseCategory.VALID}:
            raise ValueError(f"case {self.id!r}: {self.category} is not an integrity category")
        return self


class Corpus(BaseModel):
    name: str
    profile: ResearchProfile
    papers: list[EvalPaper]
    relevance: list[RelevanceLabel] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    cases: list[Case] = Field(default_factory=list)
