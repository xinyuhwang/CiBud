from enum import StrEnum

from pydantic import BaseModel, Field

from cibud.models.common import LLMProvenance, new_id


class RelationshipType(StrEnum):
    """Design doc §8. A paper may carry several."""

    DIRECT = "direct"
    METHODOLOGICAL = "methodological"
    DATA_DOMAIN = "data_domain"
    CONTRASTING = "contrasting"


class RelevanceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCERTAIN = "uncertain"


class RelevanceAssessment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rel"))
    paper_id: str
    profile_version: int = Field(ge=1)
    relationships: list[RelationshipType]
    level: RelevanceLevel
    reasons: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    potential_use: list[str] = Field(default_factory=list)
    provenance: LLMProvenance
