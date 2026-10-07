from pydantic import BaseModel, Field, model_validator

from cibud.models.common import new_id


class BoundingBox(BaseModel):
    """PDF coordinates from GROBID, used for click-to-highlight."""

    page: int = Field(ge=1)
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class EvidencePassage(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    paper_id: str
    ordinal: int = Field(default=0, ge=0)  # position in the paper's canonical text
    text: str
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    char_span: tuple[int, int]
    bboxes: list[BoundingBox] = Field(default_factory=list)

    @model_validator(mode="after")
    def _span_is_ordered(self) -> "EvidencePassage":
        start, end = self.char_span
        if not 0 <= start <= end:
            raise ValueError(f"invalid char_span {self.char_span}")
        return self
