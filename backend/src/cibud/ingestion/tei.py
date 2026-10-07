"""Parse GROBID TEI XML into structured records (deterministic, no network)."""

import re
import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import BaseModel, Field

from cibud.models.evidence import BoundingBox

NS = {"t": "http://www.tei-c.org/ns/1.0"}
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


class Author(BaseModel):
    family: str
    given: str | None = None


class HeaderMetadata(BaseModel):
    title: str | None = None
    authors: list[Author] = Field(default_factory=list)
    date: str | None = None  # ISO, as precise as the PDF states it: "2021", "2021-03-04"
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    abstract: str | None = None

    @property
    def year(self) -> int | None:
        return int(self.date[:4]) if self.date and self.date[:4].isdigit() else None


class Sentence(BaseModel):
    text: str
    boxes: list[BoundingBox] = Field(default_factory=list)

    @property
    def page(self) -> int | None:
        return self.boxes[0].page if self.boxes else None


class Paragraph(BaseModel):
    sentences: list[Sentence]


SectionKind = Literal["abstract", "body", "caption"]


class Section(BaseModel):
    heading: str | None
    number: str | None = None
    kind: SectionKind = "body"
    paragraphs: list[Paragraph]

    @property
    def label(self) -> str | None:
        """'4.2 Comparison with baselines', or just the heading."""
        if self.number and self.heading:
            return f"{self.number} {self.heading}"
        return self.heading


class BibEntry(BaseModel):
    """An entry from the paper's own reference list."""

    id: str
    title: str | None = None
    authors: list[Author] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None


class ParsedPaper(BaseModel):
    header: HeaderMetadata
    sections: list[Section]
    bibliography: list[BibEntry] = Field(default_factory=list)

    def sentence_count(self, kind: SectionKind) -> int:
        return sum(len(p.sentences) for s in self.sections if s.kind == kind for p in s.paragraphs)


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = " ".join("".join(el.itertext()).split())
    return text or None


# New-style (2104.01234) and old-style (hep-th/9901001) arXiv identifiers.
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)


def normalize_arxiv_id(value: str) -> str | None:
    """'arXiv:2607.25164v1 [cs.CV]' -> '2607.25164' (version dropped for lookups)."""
    match = _ARXIV_ID.search(value)
    return match.group(1) if match else None


def parse_coords(value: str | None) -> list[BoundingBox]:
    """GROBID coords: 'page,x,y,w,h;page,x,y,w,h;...'."""
    boxes: list[BoundingBox] = []
    for part in (value or "").split(";"):
        fields = part.split(",")
        if len(fields) != 5:
            continue
        page, x, y, w, h = fields
        boxes.append(
            BoundingBox(page=int(page), x=float(x), y=float(y), width=float(w), height=float(h))
        )
    return boxes


def _authors(parent: ET.Element | None) -> list[Author]:
    if parent is None:
        return []
    authors = []
    for pers in parent.findall("t:author/t:persName", NS):
        family = _text(pers.find("t:surname", NS))
        if not family:
            continue
        given = " ".join(t for f in pers.findall("t:forename", NS) if (t := _text(f)))
        authors.append(Author(family=family, given=given or None))
    return authors


def _idno(parent: ET.Element, kind: str) -> str | None:
    for idno in parent.iter(f"{{{NS['t']}}}idno"):
        if (idno.get("type") or "").lower() == kind.lower():
            value = _text(idno)
            if value and kind.lower() == "arxiv":
                return normalize_arxiv_id(value)
            return value
    return None


def _date(parent: ET.Element) -> str | None:
    for date in parent.iter(f"{{{NS['t']}}}date"):
        if when := date.get("when"):
            return when
    return None


def _venue(bibl: ET.Element) -> str | None:
    for level in ("j", "m"):
        if title := _text(bibl.find(f"t:monogr/t:title[@level='{level}']", NS)):
            return title
    return None


def _header(root: ET.Element) -> HeaderMetadata:
    header = root.find("t:teiHeader", NS)
    if header is None:
        return HeaderMetadata()
    bibl = header.find("t:fileDesc/t:sourceDesc/t:biblStruct", NS)
    abstract = header.find("t:profileDesc/t:abstract", NS)
    publication = header.find("t:fileDesc/t:publicationStmt", NS)
    date = _date(publication) if publication is not None else None
    if date is None and bibl is not None:
        date = _date(bibl)
    return HeaderMetadata(
        title=_text(header.find("t:fileDesc/t:titleStmt/t:title", NS)),
        authors=_authors(bibl.find("t:analytic", NS)) if bibl is not None else [],
        date=date,
        doi=_idno(bibl, "DOI") if bibl is not None else None,
        arxiv_id=_idno(bibl, "arXiv") if bibl is not None else None,
        venue=_venue(bibl) if bibl is not None else None,
        abstract=_text(abstract),
    )


def _paragraph(p: ET.Element) -> Paragraph:
    sentences = [
        Sentence(text=text, boxes=parse_coords(s.get("coords")))
        for s in p.findall("t:s", NS)
        if (text := _text(s))
    ]
    if not sentences and (text := _text(p)):
        # Sentence segmentation was off or failed: the paragraph is one unit.
        sentences = [Sentence(text=text, boxes=parse_coords(p.get("coords")))]
    return Paragraph(sentences=sentences)


def _sections(
    container: ET.Element | None,
    default_heading: str | None = None,
    kind: SectionKind = "body",
) -> list[Section]:
    sections: list[Section] = []
    if container is None:
        return sections
    heading, number = default_heading, None
    for div in container.findall("t:div", NS):
        head = div.find("t:head", NS)
        if head is not None:
            heading, number = _text(head), head.get("n")
        # A div without a head continues the previous section (GROBID splits at page breaks).
        paragraphs = [para for p in div.findall("t:p", NS) if (para := _paragraph(p)).sentences]
        if paragraphs:
            sections.append(
                Section(heading=heading, number=number, kind=kind, paragraphs=paragraphs)
            )
    return sections


def _captions(body: ET.Element | None) -> list[Section]:
    """Figure and table captions. Numeric results often live in tables, so captions are
    kept as evidence; table cell contents are not reliably extracted by GROBID."""
    sections: list[Section] = []
    if body is None:
        return sections
    for fig in body.findall("t:figure", NS):
        desc = fig.find("t:figDesc", NS)
        text = _text(desc)
        if not text:
            continue
        label = _text(fig.find("t:head", NS)) or (
            "Table" if fig.get("type") == "table" else "Figure"
        )
        if (n := _text(fig.find("t:label", NS))) and n not in label:
            label = f"{label} {n}"
        label = label.rstrip(" :.")
        sentence = Sentence(text=text, boxes=parse_coords(fig.get("coords")))
        sections.append(
            Section(heading=label, kind="caption", paragraphs=[Paragraph(sentences=[sentence])])
        )
    return sections


def _bibliography(root: ET.Element) -> list[BibEntry]:
    entries = []
    for i, bibl in enumerate(root.findall(".//t:back//t:listBibl/t:biblStruct", NS)):
        analytic = bibl.find("t:analytic", NS)
        monogr = bibl.find("t:monogr", NS)
        title = _text(analytic.find("t:title", NS)) if analytic is not None else None
        if title is None and monogr is not None:
            title = _text(monogr.find("t:title", NS))
        authors = _authors(analytic) or _authors(monogr)
        date = _date(bibl)
        entries.append(
            BibEntry(
                id=bibl.get(XML_ID) or f"b{i}",
                title=title,
                authors=authors,
                year=int(date[:4]) if date and date[:4].isdigit() else None,
                venue=_venue(bibl) if analytic is not None else None,
                doi=_idno(bibl, "DOI"),
                arxiv_id=_idno(bibl, "arXiv"),
            )
        )
    return entries


def parse_tei(xml: str | bytes) -> ParsedPaper:
    root = ET.fromstring(xml)
    header = _header(root)
    text = root.find("t:text", NS)
    sections = _sections(text.find("t:front", NS) if text is not None else None)
    abstract_div = root.find("t:teiHeader/t:profileDesc/t:abstract", NS)
    abstract = _sections(abstract_div, default_heading="Abstract", kind="abstract")
    if not abstract and abstract_div is not None:
        # Some TEI puts abstract paragraphs directly under <abstract>.
        paragraphs = [
            p for el in abstract_div.findall("t:p", NS) if (p := _paragraph(el)).sentences
        ]
        abstract = (
            [Section(heading="Abstract", kind="abstract", paragraphs=paragraphs)]
            if paragraphs
            else []
        )
    if abstract:
        # Built from sentences: GROBID writes adjacent <s> elements with no whitespace, so
        # the element's raw text would run sentences together ("data.Our model").
        header.abstract = " ".join(
            s.text for section in abstract for p in section.paragraphs for s in p.sentences
        )
    body_el = text.find("t:body", NS) if text is not None else None
    body = _sections(body_el)
    return ParsedPaper(
        header=header,
        sections=abstract + sections + body + _captions(body_el),
        bibliography=_bibliography(root),
    )
