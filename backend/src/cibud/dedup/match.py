"""Decide whether two references describe the same work (design doc §5). Pure functions."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cibud.metadata.compare import first_author_matches, normalize_doi, title_similarity, year_of

# Near-identical titles alone are not enough (a seeded defect in §16 is two different papers
# with near-identical titles): the first author must match and the years must be close.
TITLE_MIN_SIMILARITY = 0.95
MAX_YEAR_GAP = 1


class DuplicateKind(StrEnum):
    SAME_DOI = "same_doi"
    SAME_ARXIV = "same_arxiv"
    VERSION = "version"  # a preprint and its published version
    SIMILAR = "similar"  # same title, first author, and year; no shared identifier


@dataclass(frozen=True)
class DuplicateMatch:
    reference_id: str
    citation_key: str
    kind: DuplicateKind

    def describe(self) -> str:
        reason = {
            DuplicateKind.SAME_DOI: "same DOI",
            DuplicateKind.SAME_ARXIV: "same arXiv ID",
            DuplicateKind.VERSION: "preprint and published version of the same work",
            DuplicateKind.SIMILAR: "same title, first author, and year",
        }[self.kind]
        return f"possible duplicate of {self.citation_key}: {reason}"


def _is_preprint(csl: dict[str, Any]) -> bool:
    doi = normalize_doi(csl.get("DOI")) or ""
    return bool(csl.get("arxiv")) and (not doi or doi.startswith("10.48550/arxiv."))


def match(a: dict[str, Any], b: dict[str, Any]) -> DuplicateKind | None:
    """How ``a`` and ``b`` (CSL-JSON) are the same work, or None if they are not."""
    doi_a, doi_b = normalize_doi(a.get("DOI")), normalize_doi(b.get("DOI"))
    if doi_a and doi_a == doi_b:
        return DuplicateKind.SAME_DOI
    if a.get("arxiv") and a.get("arxiv") == b.get("arxiv"):
        return DuplicateKind.SAME_ARXIV

    published_a = normalize_doi(a.get("published-doi"))
    published_b = normalize_doi(b.get("published-doi"))
    if (published_a and published_a == doi_b) or (published_b and published_b == doi_a):
        return DuplicateKind.VERSION

    title_a, title_b = a.get("title"), b.get("title")
    if not (isinstance(title_a, str) and isinstance(title_b, str)):
        return None
    if title_similarity(title_a, title_b) < TITLE_MIN_SIMILARITY:
        return None
    if not first_author_matches(a.get("author"), b.get("author")):
        return None
    year_a, year_b = year_of(a.get("issued")), year_of(b.get("issued"))
    if year_a is not None and year_b is not None and abs(year_a - year_b) > MAX_YEAR_GAP:
        return None
    if _is_preprint(a) != _is_preprint(b):
        return DuplicateKind.VERSION
    return DuplicateKind.SIMILAR
