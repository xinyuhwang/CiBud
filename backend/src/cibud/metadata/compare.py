"""Tolerant comparison of CSL values from different sources.

Differences in case, punctuation, diacritics, and given-name formatting are not conflicts;
a different year, a different author list, or a substantially different title are.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


def _fold(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _fold(a), _fold(b)).ratio()


def year_of(issued: Any) -> int | None:
    try:
        year = issued["date-parts"][0][0]
    except (KeyError, IndexError, TypeError):
        return None
    return int(year) if year is not None else None


def family_names(authors: Any) -> list[str]:
    if not isinstance(authors, list):
        return []
    return [_fold(a.get("family", "")).replace(" ", "") for a in authors if isinstance(a, dict)]


def authors_match(a: Any, b: Any) -> bool:
    """Same number of authors with the same family names, in order."""
    return family_names(a) == family_names(b)


def first_author_matches(a: Any, b: Any) -> bool:
    fa, fb = family_names(a), family_names(b)
    return bool(fa and fb) and fa[0] == fb[0]


def normalize_doi(doi: Any) -> str | None:
    if not isinstance(doi, str) or not doi.strip():
        return None
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip().lower())
