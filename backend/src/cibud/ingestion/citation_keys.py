"""Stable, human-readable citation keys for LaTeX export, e.g. ``chen2021attention``."""

import re
import unicodedata
from collections.abc import Set

STOPWORDS = {
    "a", "an", "the", "on", "of", "for", "in", "to", "and", "with", "via", "from", "by",
    "is", "are", "towards", "toward", "at", "as", "using",
}  # fmt: skip


def _ascii_slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


# Registries prefix some titles with an editorial status; it is not part of the work's name.
_STATUS_PREFIX = re.compile(r"^\s*(retracted|withdrawn|retraction|expression of concern)"
                            r"(\s+article)?\s*:\s*", re.I)  # fmt: skip


def base_key(family: str | None, year: int | None, title: str | None) -> str:
    author = _ascii_slug(family or "") or "anon"
    title = _STATUS_PREFIX.sub("", title or "")
    word = next(
        (
            slug
            for w in re.split(r"[\s\-:]+", title or "")
            if (slug := _ascii_slug(w)) and slug not in STOPWORDS
        ),
        "",
    )
    return f"{author}{year or 'nd'}{word}"


def unique_key(base: str, taken: Set[str]) -> str:
    """``base``, or ``base`` + a, b, c... if that key is already used in the project."""
    if base not in taken:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        if (candidate := f"{base}{suffix}") not in taken:
            return candidate
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"
