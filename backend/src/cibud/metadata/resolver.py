"""Merge per-source metadata, pick values by precedence, and detect conflicts (§7.3).

Everything here works on a reference's stored ``field_provenance``: each field keeps one
candidate per source. The displayed CSL value is the highest-precedence candidate; conflicts
are computed from the candidates, so a user override resolves a conflict without re-fetching.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Any

from cibud.metadata.compare import (
    authors_match,
    first_author_matches,
    normalize_doi,
    title_similarity,
    year_of,
)
from cibud.metadata.sources import SourceRecord
from cibud.models.common import MetadataSource, utcnow
from cibud.models.reference import FieldCandidate, ReferenceVerification, VerificationStatus

S = MetadataSource
# Publisher/registry records beat what was extracted from the PDF (§7.3); the user beats all.
PRECEDENCE: tuple[MetadataSource, ...] = (
    S.USER,
    S.CROSSREF,
    S.OPENALEX,
    S.ARXIV,
    S.SEMANTIC_SCHOLAR,
    S.BIBTEX,
    S.RIS,
    S.PDF_HEADER,
)
REGISTRIES = frozenset({S.CROSSREF, S.OPENALEX, S.ARXIV, S.SEMANTIC_SCHOLAR})

# Below this, a looked-up record describes a different paper and is rejected outright.
IDENTITY_MIN_SIMILARITY = 0.75
# Search results must match the paper's title at least this closely to be accepted.
SEARCH_MIN_SIMILARITY = 0.92
# Below this, two sources' titles disagree enough to need a human decision.
TITLE_CONFLICT_BELOW = 0.9

Provenance = dict[str, list[FieldCandidate]]


@dataclass(frozen=True)
class Conflict:
    field: str
    values: dict[MetadataSource, Any]

    def describe(self) -> str:
        shown = ", ".join(f"{_show(self.field, v)} ({s.value})" for s, v in self.values.items())
        return f"{self.field} disagrees: {shown}"


def _show(field: str, value: Any) -> str:
    if field == "issued":
        return str(year_of(value))
    if field == "author":
        families = [a.get("family", "?") for a in value if isinstance(a, dict)]
        more = ", ..." if len(families) > 3 else ""
        return f"{len(families)} authors ({', '.join(families[:3])}{more})"
    return repr(value)


def _rank(source: MetadataSource) -> int:
    return PRECEDENCE.index(source) if source in PRECEDENCE else len(PRECEDENCE)


def merge_record(provenance: Provenance, record: SourceRecord) -> Provenance:
    """Replace ``record.source``'s candidates with this record's values."""
    merged = {
        f: [c for c in cands if c.source is not record.source] for f, cands in provenance.items()
    }
    for field, value in record.csl.items():
        merged.setdefault(field, []).append(FieldCandidate(value=value, source=record.source))
    return {f: cands for f, cands in merged.items() if cands}


def set_user_value(provenance: Provenance, field: str, value: Any) -> Provenance:
    others = [c for c in provenance.get(field, []) if c.source is not S.USER]
    return {**provenance, field: [FieldCandidate(value=value, source=S.USER), *others]}


def choose(provenance: Provenance, base: dict[str, Any]) -> dict[str, Any]:
    """CSL with each field taken from its highest-precedence candidate."""
    csl = dict(base)
    for field, candidates in provenance.items():
        best = min(candidates, key=lambda c: _rank(c.source))
        csl[field] = best.value
    return csl


def _by_source(candidates: list[FieldCandidate]) -> dict[MetadataSource, Any]:
    return {c.source: c.value for c in sorted(candidates, key=lambda c: _rank(c.source))}


def find_conflicts(provenance: Provenance) -> list[Conflict]:
    conflicts: list[Conflict] = []
    for field, candidates in provenance.items():
        if any(c.source is S.USER for c in candidates):
            continue  # the user has decided
        values = _by_source(candidates)
        if len(values) < 2:
            continue
        if _field_conflicts(field, values):
            conflicts.append(Conflict(field, values))
    return conflicts


def _field_conflicts(field: str, values: dict[MetadataSource, Any]) -> bool:
    pairs = list(combinations(values.items(), 2))
    if field == "title":
        return any(
            title_similarity(str(a), str(b)) < TITLE_CONFLICT_BELOW for (_, a), (_, b) in pairs
        )
    if field == "issued":
        return len({y for v in values.values() if (y := year_of(v)) is not None}) > 1
    if field == "DOI":
        return len({d for v in values.values() if (d := normalize_doi(v))}) > 1
    if field == "author":
        # GROBID's header author list is often incomplete, so the PDF header is only checked
        # on the first author; registries must agree on the whole list.
        for (sa, a), (sb, b) in pairs:
            if S.PDF_HEADER in (sa, sb):
                if not first_author_matches(a, b):
                    return True
            elif not authors_match(a, b):
                return True
        return False
    return False  # venue, pages, etc.: precedence decides, no human needed


def same_paper(title: str | None, record: SourceRecord) -> bool:
    """Identity check for a looked-up record. Unknown titles are given the benefit of doubt."""
    if not title or not record.title:
        return True
    return title_similarity(title, record.title) >= IDENTITY_MIN_SIMILARITY


def best_search_match(
    title: str, first_author: str | None, results: list[SourceRecord]
) -> SourceRecord | None:
    def ok(r: SourceRecord) -> bool:
        if not r.title or title_similarity(title, r.title) < SEARCH_MIN_SIMILARITY:
            return False
        if first_author and r.csl.get("author"):
            return first_author_matches([{"family": first_author}], r.csl["author"])
        return True

    matches = [r for r in results if ok(r)]
    return max(matches, key=lambda r: title_similarity(title, r.title or ""), default=None)


def verify(
    records: list[SourceRecord], rejected: list[str], conflicts: list[Conflict]
) -> ReferenceVerification:
    if rejected or conflicts:
        status = VerificationStatus.MISMATCH
    elif any(r.source in REGISTRIES for r in records):
        status = VerificationStatus.VERIFIED
    else:
        status = VerificationStatus.NOT_FOUND
    notices = [n for r in records for n in r.notices]
    return ReferenceVerification(
        status=status,
        retracted=any(r.retracted for r in records),
        has_correction=any(r.corrected for r in records),
        rejected_records=rejected,
        issues=[*rejected, *(c.describe() for c in conflicts), *notices],
        checked_at=utcnow(),
    )
