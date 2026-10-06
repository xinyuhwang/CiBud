"""Structured draft document (design doc §6.1).

Content is a ProseMirror/Tiptap JSON tree. Citations are atomic inline nodes that point at
stable reference IDs; rendered citation text is never stored.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from cibud.models.common import new_id


class CitationMode(StrEnum):
    PARENTHETICAL = "parenthetical"  # (Smith et al., 2020)
    NARRATIVE = "narrative"  # Smith et al. (2020)
    SUPPRESS_AUTHOR = "suppress_author"  # (2020)


class CitationItem(BaseModel):
    ref: str  # Reference.id
    locator: str | None = None
    label: str | None = None  # CSL locator label, e.g. "page", "section"


class CitationNodeAttrs(BaseModel):
    """``attrs`` of a ``{"type": "citation"}`` node in the document tree."""

    items: list[CitationItem] = Field(min_length=1)
    mode: CitationMode = CitationMode.PARENTHETICAL


class Document(BaseModel):
    id: str = Field(default_factory=lambda: new_id("doc"))
    project_id: str
    version: int = Field(default=1, ge=1)
    content: dict[str, Any]


def iter_citation_nodes(node: dict[str, Any]) -> list[CitationNodeAttrs]:
    """All citation nodes in a document subtree, in document order."""
    found: list[CitationNodeAttrs] = []
    if node.get("type") == "citation":
        found.append(CitationNodeAttrs.model_validate(node.get("attrs", {})))
    for child in node.get("content", []):
        found.extend(iter_citation_nodes(child))
    return found
