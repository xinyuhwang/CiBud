"""Prefill paper metadata from Crossref (DOI) or arXiv to save typing.

Prefilled metadata is always left with ``metadata_verified: false``. The annotator must check
it against the paper itself: the benchmark scores CiBud's own Crossref-based resolver, so
ground truth copied from Crossref would make that score meaningless.
"""

import xml.etree.ElementTree as ET
from typing import Any

import httpx

from cibud.eval.schemas import EvalPaper, PaperMetadata

CROSSREF_URL = "https://api.crossref.org/works/{doi}"
ARXIV_URL = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _family_given(full_name: str) -> str:
    """'Ada M. Lovelace' -> 'Lovelace, Ada M.' (best effort; annotator verifies)."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def parse_crossref(message: dict[str, Any]) -> PaperMetadata:
    authors = [
        f"{a['family']}, {a['given']}" if "given" in a else a["family"]
        for a in message.get("author", [])
        if "family" in a
    ]
    issued = message.get("issued", {}).get("date-parts", [[None]])
    containers = message.get("container-title") or [None]
    return PaperMetadata(
        title=(message.get("title") or [""])[0],
        authors=authors,
        year=issued[0][0],
        venue=containers[0],
        doi=message.get("DOI"),
    )


def parse_arxiv(atom_xml: str) -> PaperMetadata:
    entry = ET.fromstring(atom_xml).find("a:entry", ATOM)
    if entry is None or entry.find("a:title", ATOM) is None:
        raise ValueError("arXiv response has no entry")

    def text(path: str) -> str | None:
        node = entry.find(path, ATOM)
        return " ".join(node.text.split()) if node is not None and node.text else None

    names = [n.text for n in entry.findall("a:author/a:name", ATOM) if n.text]
    published = text("a:published") or ""
    return PaperMetadata(
        title=text("a:title") or "",
        authors=[_family_given(n) for n in names],
        year=int(published[:4]),
        venue="arXiv",
        doi=text("arxiv:doi"),
    )


def fetch_metadata(
    paper: EvalPaper, client: httpx.Client, contact_email: str | None = None
) -> PaperMetadata | None:
    """Look up metadata by DOI, then arXiv ID. Returns None if neither is available."""
    if paper.doi:
        params = {"mailto": contact_email} if contact_email else {}
        resp = client.get(CROSSREF_URL.format(doi=paper.doi), params=params)
        resp.raise_for_status()
        return parse_crossref(resp.json()["message"])
    if paper.arxiv_id:
        resp = client.get(ARXIV_URL, params={"id_list": paper.arxiv_id})
        resp.raise_for_status()
        return parse_arxiv(resp.text)
    return None


def prefill(
    papers: list[EvalPaper], client: httpx.Client, contact_email: str | None = None
) -> tuple[list[EvalPaper], list[str]]:
    """Fill ``metadata`` where missing. Never overwrites existing metadata."""
    filled: list[EvalPaper] = []
    problems: list[str] = []
    for paper in papers:
        if paper.metadata is not None:
            filled.append(paper)
            continue
        try:
            metadata = fetch_metadata(paper, client, contact_email)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            problems.append(f"{paper.key}: {exc}")
            metadata = None
        if metadata is None:
            filled.append(paper)
            continue
        filled.append(paper.model_copy(update={"metadata": metadata, "metadata_verified": False}))
    return filled, problems
