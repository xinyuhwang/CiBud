"""Normalize displayed CSL values: names, venues, pages, DOIs (design doc §5).

Applied to the chosen values only; ``field_provenance`` keeps each source's raw value.
"""

import re
from typing import Any

from cibud.metadata.compare import normalize_doi


def _space(value: str) -> str:
    return " ".join(value.split())


def _name_part(value: str) -> str:
    value = _space(value)
    # "CHEN" -> "Chen", "AL-KINDI" -> "Al-Kindi"; mixed case ("McDonald", "van der Berg") is kept.
    if len(value) > 1 and value.isupper():
        return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), value.lower())
    return value


def _person(person: Any) -> Any:
    if not isinstance(person, dict):
        return person
    out = dict(person)
    for part in ("family", "given"):
        if isinstance(out.get(part), str):
            out[part] = _name_part(out[part])
    return out


def _pages(value: str) -> str:
    value = re.sub(r"^\s*pp?\.\s*", "", value)
    # Hyphen, en/em dashes, and minus sign all become "-".
    return re.sub("\\s*[-\u2010-\u2015\u2212]+\\s*", "-", value.strip())


def normalize_csl(csl: dict[str, Any]) -> dict[str, Any]:
    out = dict(csl)
    for field in ("title", "container-title", "publisher"):
        if isinstance(out.get(field), str):
            out[field] = _space(out[field]).rstrip(".") if field != "title" else _space(out[field])
    if isinstance(out.get("author"), list):
        out["author"] = [_person(p) for p in out["author"]]
    if isinstance(out.get("page"), str):
        out["page"] = _pages(out["page"])
    for field in ("DOI", "published-doi"):
        if field in out and (doi := normalize_doi(out[field])):
            out[field] = doi
    return out
