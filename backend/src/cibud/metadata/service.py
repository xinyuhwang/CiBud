"""Metadata resolution job and user conflict resolution (design doc §7.3)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cibud.db import repo
from cibud.db.session import transaction
from cibud.dedup.service import duplicate_issues
from cibud.ingestion.citation_keys import base_key, unique_key
from cibud.jobs import queue
from cibud.metadata.compare import normalize_doi, year_of
from cibud.metadata.resolver import (
    Conflict,
    Provenance,
    best_search_match,
    choose,
    find_conflicts,
    merge_record,
    same_paper,
    set_user_value,
    verify,
)
from cibud.metadata.sources import Registries, SourceRecord, arxiv_doi
from cibud.models.common import MetadataSource, utcnow
from cibud.models.job import Job
from cibud.models.paper import Paper, PaperIssue, PaperState
from cibud.models.reference import Reference

log = logging.getLogger(__name__)

RESOLVE_METADATA = "resolve_metadata"
EDITABLE_FIELDS = frozenset(
    {"title", "author", "issued", "DOI", "container-title", "volume", "issue", "page", "publisher"}
)
_RESOLVABLE = {PaperState.METADATA_REVIEW, PaperState.NEEDS_ATTENTION, PaperState.METADATA_ONLY}


def enqueue_resolution(session: Session, paper_id: str, reason: str) -> Job:
    """``reason`` makes the job distinct, so a new trigger re-runs resolution."""
    return queue.enqueue(session, RESOLVE_METADATA, {"paper_id": paper_id, "reason": reason})


def _header_value(reference: Reference, field: str) -> Any:
    for candidate in reference.field_provenance.get(field, []):
        if candidate.source is MetadataSource.PDF_HEADER:
            return candidate.value
    return None


async def lookup(
    registries: Registries, reference: Reference
) -> tuple[list[SourceRecord], list[str]]:
    """Fetch registry records for a reference. Returns (accepted records, rejection notes)."""
    csl = reference.csl
    known_title = _header_value(reference, "title") or csl.get("title")
    doi = normalize_doi(csl.get("DOI"))
    arxiv_id = csl.get("arxiv")

    lookups: list[Awaitable[SourceRecord | None]] = []
    if doi and not doi.startswith("10.48550/arxiv."):
        lookups += [registries.crossref_by_doi(doi), registries.openalex_by_doi(doi)]
    if arxiv_id:
        lookups += [
            registries.arxiv_by_id(arxiv_id),
            registries.openalex_by_doi(arxiv_doi(arxiv_id)),
        ]
    found = [r for r in await asyncio.gather(*lookups) if r is not None]

    if not lookups and known_title:
        # No identifier: search by title (and first author), accepting only close matches.
        authors = _header_value(reference, "author") or csl.get("author") or []
        first = authors[0].get("family") if authors and isinstance(authors[0], dict) else None
        crossref, openalex = await asyncio.gather(
            registries.crossref_search(known_title, first), registries.openalex_search(known_title)
        )
        found = [
            m for m in (best_search_match(known_title, first, crossref),
                        best_search_match(known_title, first, openalex)) if m
        ]  # fmt: skip

    accepted, rejected = [], []
    for record in found:
        if same_paper(known_title, record):
            accepted.append(record)
        else:
            rejected.append(
                f"{record.source.value} record for this identifier is a different paper: "
                f"{record.title!r}"
            )
    return accepted, rejected


def _refresh_key(session: Session, reference: Reference) -> str:
    """Regenerate an automatic key from resolved metadata, unless the reference is cited."""
    if reference.citation_key_locked or repo.is_reference_cited(session, reference.id):
        return reference.citation_key
    csl = reference.csl
    authors = csl.get("author") or []
    family = authors[0].get("family") if authors and isinstance(authors[0], dict) else None
    base = base_key(family, year_of(csl.get("issued")), csl.get("title"))
    if reference.citation_key == base or reference.citation_key.startswith(base):
        return reference.citation_key
    taken = repo.citation_keys(session, reference.project_id) - {reference.citation_key}
    return unique_key(base, taken)


def _sync_paper_state(
    session: Session, paper: Paper, reference: Reference, conflicts: list[Conflict]
) -> None:
    """Replace the paper's metadata and duplicate issues (extraction issues are untouched).

    Papers already past metadata review keep their state; an edit there doesn't re-open it.
    """
    if paper.state not in _RESOLVABLE:
        return
    metadata = [
        PaperIssue(kind="metadata", message=m)
        for m in [*reference.verification.rejected_records, *(c.describe() for c in conflicts)]
    ]
    # Retractions are recorded on the reference (verification.retracted) and enforced by the
    # reference validator, not here: there is nothing for the user to "resolve" about them.
    repo.update_issues(
        session,
        paper.id,
        {"metadata", "duplicate"},
        [*metadata, *duplicate_issues(session, reference)],
    )


def apply_resolution(
    session: Session,
    paper: Paper,
    records: list[SourceRecord],
    rejected: list[str],
) -> Reference:
    reference = repo.get_reference(session, paper.reference_id)
    provenance: Provenance = reference.field_provenance
    for record in records:
        provenance = merge_record(provenance, record)
    conflicts = find_conflicts(provenance)
    resolved = reference.model_copy(
        update={
            "field_provenance": provenance,
            "csl": choose(provenance, reference.csl),
            "verification": verify(records, rejected, conflicts),
        }
    )
    resolved = resolved.model_copy(update={"citation_key": _refresh_key(session, resolved)})
    repo.save_reference(session, resolved)
    _sync_paper_state(session, paper, resolved, conflicts)
    return resolved


def make_resolve_handler(
    factory: sessionmaker[Session], registries: Registries
) -> Callable[[Job], Awaitable[dict[str, Any] | None]]:
    async def resolve_metadata(job: Job) -> dict[str, Any] | None:
        paper_id = job.inputs["paper_id"]
        with transaction(factory) as session:
            paper = repo.get_paper(session, paper_id)
            if paper.state not in _RESOLVABLE:
                return {"skipped": f"paper is {paper.state}"}
            reference = repo.get_reference(session, paper.reference_id)

        records, rejected = await lookup(registries, reference)  # SourceUnavailable -> retry

        with transaction(factory) as session:
            paper = repo.get_paper(session, paper_id)
            resolved = apply_resolution(session, paper, records, rejected)
        log.info("resolved %s: %s", paper_id, resolved.verification.status)
        return {
            "status": resolved.verification.status.value,
            "sources": [r.source.value for r in records],
            "issues": resolved.verification.issues,
        }

    return resolve_metadata


class InvalidFieldChoice(ValueError):
    pass


def resolve_field(
    session: Session,
    reference_id: str,
    field: str,
    *,
    value: Any = None,
    source: MetadataSource | None = None,
) -> Reference:
    """The user decides a field: either pick a source's value or enter their own."""
    if field not in EDITABLE_FIELDS:
        raise InvalidFieldChoice(f"{field!r} cannot be edited")
    if (value is None) == (source is None):
        raise InvalidFieldChoice("give exactly one of value or source")
    reference = repo.get_reference(session, reference_id)
    if source is not None:
        match = [c for c in reference.field_provenance.get(field, []) if c.source is source]
        if not match:
            raise InvalidFieldChoice(f"no {source.value} value for {field!r}")
        value = match[0].value

    provenance = set_user_value(reference.field_provenance, field, value)
    conflicts = find_conflicts(provenance)
    verification = reference.verification.model_copy(
        update={"issues": [*reference.verification.rejected_records,
                           *(c.describe() for c in conflicts)],
                "checked_at": utcnow()}
    )  # fmt: skip
    updated = reference.model_copy(
        update={
            "field_provenance": provenance,
            "csl": choose(provenance, reference.csl),
            "verification": verification,
        }
    )
    repo.save_reference(session, updated)

    paper = repo.paper_for_reference(session, reference_id)
    if paper is not None:
        if field == "DOI":
            # A corrected identifier needs a fresh lookup; the job re-evaluates everything.
            enqueue_resolution(session, paper.id, f"doi:{normalize_doi(value)}")
        else:
            _sync_paper_state(session, paper, updated, conflicts)
    return updated


def conflicts_for(reference: Reference) -> list[Conflict]:
    return find_conflicts(reference.field_provenance)
