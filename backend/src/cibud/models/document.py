"""Structured draft document (design doc §6.1).

Content is a ProseMirror/Tiptap JSON tree. Citations are atomic inline nodes that point at
stable reference IDs; rendered citation text is never stored.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from cibud.models.common import new_id


class CitationMode(StrEnum):
    PARENTHETICAL = "parenthetical"  # (Smith et al., 2020)
    NARRATIVE = "narrative"  # Smith et al. (2020)
    SUPPRESS_AUTHOR = "suppress_author"  # (2020)


class CitationItem(BaseModel):
    ref: str | None = None  # Reference.id
    # The citation key as written, kept when it matched no reference (e.g. pasted text).
    key: str | None = None
    locator: str | None = None
    label: str | None = None  # CSL locator label, e.g. "page", "section"

    @model_validator(mode="after")
    def _has_target(self) -> "CitationItem":
        if self.ref is None and self.key is None:
            raise ValueError("citation item needs a ref or a key")
        return self


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
    return [attrs for _, attrs in citation_nodes_with_paths(node)]


def citation_nodes_with_paths(
    node: dict[str, Any], path: str = ""
) -> list[tuple[str, CitationNodeAttrs]]:
    """(node_path, attrs) for every citation node; node_path is child indices, e.g. "2.5"."""
    found: list[tuple[str, CitationNodeAttrs]] = []
    if node.get("type") == "citation":
        found.append((path, CitationNodeAttrs.model_validate(node.get("attrs", {}))))
    for i, child in enumerate(node.get("content", [])):
        found.extend(citation_nodes_with_paths(child, f"{path}.{i}" if path else str(i)))
    return found


def block_text(node: dict[str, Any]) -> str:
    """Plain text of a block, with citation nodes shown by key or ref (for issue context)."""
    parts: list[str] = []
    for child in node.get("content", []):
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "citation":
            items = child.get("attrs", {}).get("items", [])
            parts.append("[" + "; ".join(i.get("key") or i.get("ref") or "?" for i in items) + "]")
        else:
            parts.append(block_text(child))
    return "".join(parts)
