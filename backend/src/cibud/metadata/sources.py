"""Bibliographic registries: Crossref, OpenAlex, arXiv (design doc §5, §7).

Each lookup returns a ``SourceRecord`` whose ``csl`` uses CSL-JSON field names and value
shapes, so values from different sources can be compared and stored side by side.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import httpx

from cibud.models.common import MetadataSource

CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"
ARXIV = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

CROSSREF_TYPES = {
    "journal-article": "article-journal",
    "proceedings-article": "paper-conference",
    "posted-content": "article",
    "book-chapter": "chapter",
    "book": "book",
    "dissertation": "thesis",
    "report": "report",
}
OPENALEX_TYPES = {"article": "article-journal", "preprint": "article", "book-chapter": "chapter"}


class SourceUnavailable(Exception):
    """Transient registry failure (rate limit, 5xx, network). Worth retrying."""


@dataclass
class SourceRecord:
    source: MetadataSource
    csl: dict[str, Any]
    retracted: bool = False
    corrected: bool = False
    notices: list[str] = field(default_factory=list)  # e.g. "retraction 10.1016/..."
    # A full-text PDF this source says is legally open access (arXiv, or an OA location).
    pdf_url: str | None = None

    @property
    def title(self) -> str | None:
        title = self.csl.get("title")
        return title if isinstance(title, str) else None


def arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{arxiv_id}".lower()


def split_name(full: str) -> dict[str, str]:
    """'Ada M. Lovelace' -> {'family': 'Lovelace', 'given': 'Ada M.'} (best effort)."""
    parts = full.split()
    if len(parts) < 2:
        return {"family": full}
    return {"family": parts[-1], "given": " ".join(parts[:-1])}


def _date_parts(parts: list[Any] | None) -> dict[str, Any] | None:
    clean = [int(p) for p in (parts or []) if p is not None]
    return {"date-parts": [clean]} if clean else None


def _strip_markup(value: str | None) -> str | None:
    """Crossref abstracts are JATS XML ("<jats:p>...</jats:p>")."""
    if not value:
        return None
    text = " ".join(re.sub(r"<[^>]+>", " ", value).split())
    return re.sub(r"^Abstract\s+", "", text) or None


def _inverted_index_text(index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}."""
    if not index:
        return None
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    return " ".join(word for _, word in words) or None


def _drop_empty(csl: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in csl.items() if v not in (None, "", [], {})}


# --- parsers (pure; tested with fixtures) ----------------------------------------------


def parse_crossref(message: dict[str, Any]) -> SourceRecord:
    issued = message.get("issued", {}).get("date-parts", [[None]])[0]
    csl = _drop_empty(
        {
            "type": CROSSREF_TYPES.get(message.get("type", ""), "article"),
            "title": (message.get("title") or [None])[0],
            "author": [
                {"family": a["family"], **({"given": a["given"]} if a.get("given") else {})}
                for a in message.get("author", [])
                if a.get("family")
            ],
            "issued": _date_parts(issued),
            "DOI": (message.get("DOI") or "").lower() or None,
            "container-title": (message.get("container-title") or [None])[0],
            "volume": message.get("volume"),
            "issue": message.get("issue"),
            "page": message.get("page"),
            "publisher": message.get("publisher"),
            "abstract": _strip_markup(message.get("abstract")),
        }
    )
    notices = [f"{u.get('type')} {u.get('DOI', '')}".strip() for u in message.get("updated-by", [])]
    kinds = {u.get("type") for u in message.get("updated-by", [])}
    return SourceRecord(
        source=MetadataSource.CROSSREF,
        csl=csl,
        retracted="retraction" in kinds,
        corrected=bool(kinds & {"correction", "erratum", "expression_of_concern"}),
        notices=notices,
    )


def parse_openalex(work: dict[str, Any]) -> SourceRecord:
    doi = (work.get("doi") or "").removeprefix("https://doi.org/").lower() or None
    biblio = work.get("biblio") or {}
    pages = "-".join(p for p in (biblio.get("first_page"), biblio.get("last_page")) if p)
    date = work.get("publication_date") or str(work.get("publication_year") or "")
    source = (work.get("primary_location") or {}).get("source") or {}
    # For preprints the "source" is the repository (e.g. "arXiv (Cornell University)"),
    # which is not a publication venue; leave the venue to the arXiv record.
    venue = None if work.get("type") == "preprint" else source.get("display_name")
    csl = _drop_empty(
        {
            "type": OPENALEX_TYPES.get(work.get("type", ""), "article"),
            "title": work.get("title") or work.get("display_name"),
            "author": [
                split_name(a["author"]["display_name"])
                for a in work.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ],
            "issued": _date_parts([int(p) for p in date.split("-") if p.isdigit()]),
            "DOI": doi,
            "container-title": venue,
            "volume": biblio.get("volume"),
            "issue": biblio.get("issue"),
            "page": pages or None,
            "abstract": _inverted_index_text(work.get("abstract_inverted_index")),
        }
    )
    retracted = bool(work.get("is_retracted"))
    best_oa = work.get("best_oa_location") or {}
    return SourceRecord(
        source=MetadataSource.OPENALEX,
        csl=csl,
        retracted=retracted,
        notices=["retraction (OpenAlex)"] if retracted else [],
        pdf_url=best_oa.get("pdf_url") if best_oa.get("is_oa") else None,
    )


def parse_arxiv(atom_xml: str) -> SourceRecord | None:
    entry = ET.fromstring(atom_xml).find("a:entry", ATOM)
    if entry is None or entry.find("a:title", ATOM) is None:
        return None  # arXiv returns an empty feed (or an error entry) for unknown IDs

    def text(path: str) -> str | None:
        node = entry.find(path, ATOM)
        return " ".join(node.text.split()) if node is not None and node.text else None

    entry_id = text("a:id") or ""
    arxiv_id = entry_id.rsplit("/abs/", 1)[-1].split("v")[0] if "/abs/" in entry_id else None
    published = text("a:published") or ""
    csl = _drop_empty(
        {
            "type": "article",
            "title": text("a:title"),
            "author": [
                split_name(n.text) for n in entry.findall("a:author/a:name", ATOM) if n.text
            ],
            "issued": _date_parts([int(p) for p in published[:10].split("-") if p.isdigit()]),
            "arxiv": arxiv_id,
            "container-title": "arXiv",
            # A published version's DOI is reported by the authors, not the preprint's own DOI.
            "published-doi": (text("arxiv:doi") or "").lower() or None,
            "abstract": text("a:summary"),
        }
    )
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None
    return SourceRecord(source=MetadataSource.ARXIV, csl=csl, pdf_url=pdf_url)


# --- client -----------------------------------------------------------------------------


class Registries:
    def __init__(self, client: httpx.AsyncClient, contact_email: str | None = None):
        self._client = client
        self._mailto = {"mailto": contact_email} if contact_email else {}

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response | None:
        try:
            resp = await self._client.get(url, params={**self._mailto, **(params or {})})
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{url}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SourceUnavailable(f"{url}: HTTP {resp.status_code}")
        if resp.status_code != 200:
            return None
        return resp

    async def crossref_by_doi(self, doi: str) -> SourceRecord | None:
        resp = await self._get(f"{CROSSREF}/works/{doi}")
        return parse_crossref(resp.json()["message"]) if resp else None

    async def crossref_search(self, title: str, author: str | None) -> list[SourceRecord]:
        params = {"query.bibliographic": title, "rows": 3}
        if author:
            params["query.author"] = author
        resp = await self._get(f"{CROSSREF}/works", params)
        items = resp.json()["message"].get("items", []) if resp else []
        return [parse_crossref(item) for item in items]

    async def openalex_by_doi(self, doi: str) -> SourceRecord | None:
        resp = await self._get(f"{OPENALEX}/works/doi:{doi}")
        return parse_openalex(resp.json()) if resp else None

    async def openalex_search(self, title: str) -> list[SourceRecord]:
        resp = await self._get(f"{OPENALEX}/works", {"search": title, "per-page": 3})
        return [parse_openalex(w) for w in resp.json().get("results", [])] if resp else []

    async def arxiv_by_id(self, arxiv_id: str) -> SourceRecord | None:
        resp = await self._get(ARXIV, {"id_list": arxiv_id})
        return parse_arxiv(resp.text) if resp else None
