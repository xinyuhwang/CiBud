from pydantic import BaseModel, Field

from cibud.models.common import new_id


class ResearchProfile(BaseModel):
    """The researcher's own work. Editing it bumps ``version``; relevance is cached per version."""

    version: int = Field(default=1, ge=1)
    problem: str
    method: str
    data: str
    contribution: str
    differentiation: str = ""


class Project(BaseModel):
    id: str = Field(default_factory=lambda: new_id("proj"))
    name: str
    profile: ResearchProfile
    default_style: str = "apa"  # CSL style ID
    settings: dict[str, str] = Field(default_factory=dict)
