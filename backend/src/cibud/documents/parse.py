"""Convert pasted text with citation markers into a document tree (design doc §6.1, §15 1A).

Supported markers, matched against the project's citation keys:

    Pandoc   [@chen2021]  [@chen2021, p. 4; @ito2022]  [see @chen2021]  @chen2021  [-@chen2021]
    LaTeX    \\cite{a,b}  \\citep[p.~4]{a}  \\citet{a}  \\parencite{a}  \\textcite{a}  \\autocite{a}

Unknown keys still become citation nodes (``ref`` = None, ``key`` = the text), so the check
reports them instead of silently dropping them. Paragraphs are separated by blank lines;
Markdown ``#`` headings and LaTeX ``\\section{}`` become heading nodes.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cibud.models.document import CitationItem, CitationMode, CitationNodeAttrs

_KEY = r"[A-Za-z0-9_][\w:.#$%&+?<>~/-]*[A-Za-z0-9_]|[A-Za-z0-9_]"
_PANDOC_GROUP = re.compile(r"\[([^\[\]]*?-?@(?:" + _KEY + r")[^\[\]]*)\]")
_PANDOC_ITEM = re.compile(r"(?<![\w])(-?)@(" + _KEY + r")\s*(?:,\s*(.*))?$")
_PANDOC_NARRATIVE = re.compile(r"(?<![\w@\[-])@(" + _KEY + r")")
_LATEX = re.compile(
    r"\\(cite|citep|parencite|autocite|citet|textcite|citeauthor|citeyear)\*?"
    r"(?:\[([^\]]*)\])?(?:\[([^\]]*)\])?\{([^}]*)\}"
)
_NARRATIVE_COMMANDS = {"citet", "textcite", "citeauthor"}
_LOCATOR_LABELS = {
    "p": "page", "pp": "page", "page": "page", "pages": "page",
    "sec": "section", "section": "section", "§": "section",
    "ch": "chapter", "chap": "chapter", "chapter": "chapter",
    "fig": "figure", "figure": "figure", "tab": "table", "table": "table",
    "eq": "equation",
}  # fmt: skip


@dataclass
class ParseReport:
    content: dict[str, Any]
    citations: int = 0
    unresolved_keys: list[str] = field(default_factory=list)


def parse_locator(text: str | None) -> tuple[str | None, str | None]:
    """'p. 4' -> ('4', 'page'); 'sec. 3.2' -> ('3.2', 'section'); '4' -> ('4', 'page')."""
    if not text or not text.strip():
        return None, None
    text = text.replace("~", " ").strip()
    match = re.match(r"([A-Za-z§]+)\.?\s*(.+)", text)
    if match and (label := _LOCATOR_LABELS.get(match.group(1).lower())):
        return match.group(2).strip(), label
    if re.match(r"^[\divxlc]+([-\u2013][\divxlc]+)?$", text, re.I):
        return text, "page"
    return text, None


class _Builder:
    def __init__(self, keys: Mapping[str, str]):
        self.keys = keys
        self.citations = 0
        self.unresolved: list[str] = []

    def item(self, key: str, locator_text: str | None = None) -> CitationItem:
        ref = self.keys.get(key)
        if ref is None and key not in self.unresolved:
            self.unresolved.append(key)
        locator, label = parse_locator(locator_text)
        return CitationItem(ref=ref, key=None if ref else key, locator=locator, label=label)

    def node(self, items: list[CitationItem], mode: CitationMode) -> dict[str, Any]:
        self.citations += 1
        attrs = CitationNodeAttrs(items=items, mode=mode)
        return {"type": "citation", "attrs": attrs.model_dump(exclude_none=True, mode="json")}

    def pandoc_group(self, body: str) -> dict[str, Any] | None:
        items, mode = [], CitationMode.PARENTHETICAL
        for part in body.split(";"):
            # A prefix like "see " before @key is prose, not part of the reference.
            match = _PANDOC_ITEM.search(part.strip())
            if not match:
                continue
            if match.group(1) == "-":
                mode = CitationMode.SUPPRESS_AUTHOR
            items.append(self.item(match.group(2), match.group(3)))
        return self.node(items, mode) if items else None

    def latex(self, m: re.Match[str]) -> dict[str, Any]:
        command, first, second, keys = m.groups()
        # \citep[p.~4]{a} has one optional arg (the locator); \citep[see][p.~4]{a} has two.
        locator = second if second is not None else first
        mode = (
            CitationMode.NARRATIVE if command in _NARRATIVE_COMMANDS else CitationMode.PARENTHETICAL
        )
        if command == "citeyear":
            mode = CitationMode.SUPPRESS_AUTHOR
        names = [k.strip() for k in keys.split(",") if k.strip()]
        items = [
            self.item(k, locator if i == len(names) - 1 else None) for i, k in enumerate(names)
        ]
        return self.node(items, mode)


def _inline(text: str, builder: _Builder) -> list[dict[str, Any]]:
    """Split a paragraph into text and citation nodes."""
    spans: list[tuple[int, int, dict[str, Any]]] = []
    for m in _LATEX.finditer(text):
        spans.append((m.start(), m.end(), builder.latex(m)))
    for m in _PANDOC_GROUP.finditer(text):
        if any(s <= m.start() < e for s, e, _ in spans):
            continue
        if node := builder.pandoc_group(m.group(1)):
            spans.append((m.start(), m.end(), node))
    for m in _PANDOC_NARRATIVE.finditer(text):
        if any(s <= m.start() < e for s, e, _ in spans):
            continue
        item = builder.item(m.group(1))
        spans.append((m.start(), m.end(), builder.node([item], CitationMode.NARRATIVE)))

    nodes: list[dict[str, Any]] = []
    cursor = 0
    for start, end, node in sorted(spans, key=lambda s: s[0]):
        if start > cursor:
            nodes.append({"type": "text", "text": text[cursor:start]})
        nodes.append(node)
        cursor = end
    if cursor < len(text):
        nodes.append({"type": "text", "text": text[cursor:]})
    return nodes


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TEX_HEADING = re.compile(r"^\\(sub)*section\*?\{(.*)\}\s*$")


def parse_text(text: str, keys: Mapping[str, str]) -> ParseReport:
    """``keys`` maps citation key -> reference ID for the project."""
    builder = _Builder(keys)
    blocks: list[dict[str, Any]] = []
    # Heading lines are blocks of their own even without surrounding blank lines.
    lines = text.replace("\r\n", "\n").split("\n")
    text = "\n".join(
        f"\n{line}\n"
        if _MD_HEADING.match(line.strip()) or _TEX_HEADING.match(line.strip())
        else line
        for line in lines
    )
    for raw in re.split(r"\n\s*\n", text):
        block = " ".join(raw.split())
        if not block:
            continue
        if m := _MD_HEADING.match(block):
            blocks.append(_heading(len(m.group(1)), m.group(2)))
        elif m := _TEX_HEADING.match(block):
            blocks.append(_heading(1 + block.count("sub", 0, block.index("section")), m.group(2)))
        else:
            blocks.append({"type": "paragraph", "content": _inline(block, builder)})
    return ParseReport(
        content={"type": "doc", "content": blocks},
        citations=builder.citations,
        unresolved_keys=builder.unresolved,
    )


def _heading(level: int, text: str) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }
