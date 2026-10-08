"""Parse pasted identifiers and bibliography files into CSL-JSON (design doc §7.1). Pure."""

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

import rispy
from bibtexparser import middlewares
from bibtexparser.entrypoint import parse_string

from cibud.ingestion.tei import normalize_arxiv_id
from cibud.models.common import MetadataSource

# --- identifiers ------------------------------------------------------------------------

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
_ARXIV_HINT = re.compile(r"arxiv", re.I)
_BARE_ARXIV = re.compile(r"^\s*\d{4}\.\d{4,5}(v\d+)?\s*$")


@dataclass(frozen=True)
class Identifier:
    kind: Literal["doi", "arxiv"]
    value: str


def parse_identifier(text: str) -> Identifier | None:
    """A DOI or arXiv ID from a pasted string: bare ID, 'doi:...', doi.org or arxiv.org URL,
    or a publisher URL that contains a DOI. Returns None if nothing usable is found."""
    text = unquote(text.strip())
    # arXiv's own DOIs (10.48550/arXiv.2103.01234) are arXiv IDs too.
    if (_ARXIV_HINT.search(text) or _BARE_ARXIV.match(text)) and (
        arxiv_id := normalize_arxiv_id(text.split("arXiv.")[-1])
    ):
        return Identifier("arxiv", arxiv_id)
    if match := _DOI.search(text):
        doi = match.group(0).rstrip(".,;)]}").lower()
        doi = re.sub(r"(\.pdf|/full|/abstract|/epdf)$", "", doi)
        return Identifier("doi", doi)
    return None


# --- bibliography files -----------------------------------------------------------------


@dataclass
class ParsedEntry:
    csl: dict[str, Any]
    key: str | None = None  # the user's own citation key (BibTeX), kept for LaTeX


@dataclass
class ParsedBibliography:
    source: MetadataSource
    entries: list[ParsedEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


BIBTEX_TYPES = {
    "article": "article-journal",
    "inproceedings": "paper-conference",
    "conference": "paper-conference",
    "incollection": "chapter",
    "inbook": "chapter",
    "book": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "misc": "article",
    "unpublished": "manuscript",
    "online": "webpage",
}
RIS_TYPES = {
    "JOUR": "article-journal",
    "JFULL": "article-journal",
    "CONF": "paper-conference",
    "CPAPER": "paper-conference",
    "CHAP": "chapter",
    "BOOK": "book",
    "THES": "thesis",
    "RPRT": "report",
    "ELEC": "webpage",
    "UNPB": "manuscript",
}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
)}  # fmt: skip


def _issued(year: Any, month: Any = None) -> dict[str, Any] | None:
    year_match = re.search(r"\d{4}", str(year or ""))
    if not year_match:
        return None
    parts = [int(year_match.group(0))]
    month_text = str(month or "").strip().lower()[:3]
    if month_text.isdigit() and 1 <= int(month_text) <= 12:
        parts.append(int(month_text))
    elif month_text in MONTHS:
        parts.append(MONTHS[month_text])
    return {"date-parts": [parts]}


def _clean(csl: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in csl.items() if v not in (None, "", [], {})}


def _bibtex_person(name: Any) -> dict[str, str] | None:
    family = " ".join([*name.von, *name.last]).strip()
    given = " ".join(name.first).strip()
    if not family:
        return None
    person = {"family": family}
    if given:
        person["given"] = given
    if name.jr:
        person["suffix"] = " ".join(name.jr)
    return person


def parse_bibtex(text: str) -> ParsedBibliography:
    library = parse_string(
        text,
        append_middleware=[
            middlewares.LatexDecodingMiddleware(),
            middlewares.SeparateCoAuthors(),
            middlewares.SplitNameParts(),
        ],
    )
    result = ParsedBibliography(source=MetadataSource.BIBTEX)
    for block in library.failed_blocks:
        line = f"line {block.start_line + 1}" if block.start_line is not None else "an entry"
        result.errors.append(f"{line}: could not parse entry")
    for entry in library.entries:
        f = {k.lower(): v.value for k, v in entry.fields_dict.items()}
        arxiv = None
        if (
            str(f.get("archiveprefix", "")).lower() == "arxiv"
            or "arxiv" in str(f.get("journal", "")).lower()
        ):
            arxiv = normalize_arxiv_id(str(f.get("eprint") or f.get("journal") or ""))
        authors = [p for n in f.get("author", []) if (p := _bibtex_person(n))]
        csl = _clean(
            {
                "type": BIBTEX_TYPES.get(entry.entry_type.lower(), "article"),
                "title": f.get("title"),
                "author": authors,
                "issued": _issued(f.get("year") or f.get("date"), f.get("month")),
                "container-title": f.get("journal") or f.get("booktitle"),
                "volume": f.get("volume"),
                "issue": f.get("number"),
                "page": f.get("pages"),
                "publisher": f.get("publisher"),
                "DOI": f.get("doi"),
                "URL": f.get("url"),
                "arxiv": arxiv,
                "abstract": f.get("abstract"),
            }
        )
        result.entries.append(ParsedEntry(csl=csl, key=entry.key))
    return result


def parse_ris(text: str) -> ParsedBibliography:
    result = ParsedBibliography(source=MetadataSource.RIS)
    try:
        records = rispy.loads(text)
    except (ValueError, IOError) as exc:  # noqa: UP024  # rispy raises IOError on bad tags
        result.errors.append(f"could not parse RIS: {exc}")
        return result
    for r in records:
        names = r.get("authors") or r.get("first_authors") or []
        authors = []
        for name in names:
            family, _, given = name.partition(",")
            person = {"family": family.strip()}
            if given.strip():
                person["given"] = given.strip()
            authors.append(person)
        pages = "-".join(p for p in (r.get("start_page"), r.get("end_page")) if p)
        date = r.get("year") or r.get("publication_year") or r.get("date") or ""
        csl = _clean(
            {
                "type": RIS_TYPES.get(r.get("type_of_reference", ""), "article"),
                "title": r.get("title") or r.get("primary_title"),
                "author": authors,
                "issued": _issued(date),
                "container-title": r.get("journal_name")
                or r.get("secondary_title")
                or r.get("alternate_title3"),
                "volume": r.get("volume"),
                "issue": r.get("number"),
                "page": pages or None,
                "publisher": r.get("publisher"),
                "DOI": r.get("doi"),
                "URL": (r.get("urls") or [None])[0],
                "abstract": r.get("abstract"),
            }
        )
        result.entries.append(ParsedEntry(csl=csl, key=r.get("id")))
    return result


def parse_bibliography(filename: str, text: str) -> ParsedBibliography:
    """Choose the parser by extension, falling back to content sniffing."""
    name = filename.lower()
    if name.endswith((".ris", ".txt")) or re.search(r"^TY  - ", text, re.M):
        return parse_ris(text)
    return parse_bibtex(text)
