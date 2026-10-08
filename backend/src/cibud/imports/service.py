"""Import papers without a PDF: DOIs/URLs and BibTeX/RIS files (design doc §7.1, §7.2).

The request creates metadata-only papers and queues an ``enrich_import`` job for each. The
job resolves metadata (Crossref/OpenAlex/arXiv), then tries to obtain full text **only from
legally open sources** (arXiv, or an open-access location reported by OpenAlex). Without full
text, the abstract becomes the paper's only evidence (abstract-only); without an abstract,
the paper stays metadata-only. Either way it can still be reviewed, approved, and cited.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy.orm import Session, sessionmaker

from cibud.db import repo
from cibud.db.session import transaction
from cibud.imports.parsing import ParsedBibliography, parse_identifier
from cibud.ingestion.service import EXTRACT_PDF, PLACEHOLDER_KEY_PREFIX
from cibud.jobs import queue
from cibud.metadata.compare import normalize_doi
from cibud.metadata.service import apply_resolution, lookup
from cibud.metadata.sources import Registries, SourceRecord
from cibud.models.common import EvidenceLevel, MetadataSource, new_id
from cibud.models.evidence import EvidencePassage
from cibud.models.job import Job
from cibud.models.paper import Paper, PaperIssue, PaperState
from cibud.models.reference import FieldCandidate, Reference
from cibud.storage import ObjectStore

log = logging.getLogger(__name__)

ENRICH_IMPORT = "enrich_import"


@dataclass
class ImportResult:
    input: str
    status: Literal["created", "existing", "invalid"]
    paper_id: str | None = None
    job_id: str | None = None
    message: str | None = None


def _create_paper(
    session: Session,
    project_id: str,
    csl: dict[str, Any],
    source: MetadataSource,
    *,
    key: str | None = None,
) -> tuple[Paper, Job, str | None]:
    """Create a metadata-only paper and queue its enrichment. Returns (paper, job, note)."""
    paper_id = new_id("paper")
    note = None
    locked = False
    if key:
        taken = repo.citation_keys(session, project_id)
        locked = key not in taken
        if not locked:
            note = f"citation key {key!r} is already used; a new key will be generated"
    reference = repo.add_reference(
        session,
        Reference(
            project_id=project_id,
            citation_key=key if locked and key else f"{PLACEHOLDER_KEY_PREFIX}{paper_id}",
            citation_key_locked=locked,
            csl=csl,
            field_provenance={f: [FieldCandidate(value=v, source=source)] for f, v in csl.items()},
        ),
    )
    paper = repo.add_paper(
        session,
        Paper(id=paper_id, project_id=project_id, reference_id=reference.id),
    )
    job = queue.enqueue(session, ENRICH_IMPORT, {"paper_id": paper.id})
    return paper, job, note


def _existing(session: Session, project_id: str, csl: dict[str, Any]) -> Paper | None:
    reference = repo.find_reference_by_identifier(
        session, project_id, doi=normalize_doi(csl.get("DOI")), arxiv=csl.get("arxiv")
    )
    return repo.paper_for_reference(session, reference.id) if reference else None


def import_identifiers(session: Session, project_id: str, raw: list[str]) -> list[ImportResult]:
    repo.get_project(session, project_id)  # raises NotFound
    results = []
    for text in raw:
        if not text.strip():
            continue
        identifier = parse_identifier(text)
        if identifier is None:
            results.append(
                ImportResult(
                    text, "invalid", message="no DOI or arXiv ID found; upload the PDF instead"
                )
            )
            continue
        csl = {"DOI": identifier.value} if identifier.kind == "doi" else {"arxiv": identifier.value}
        if existing := _existing(session, project_id, csl):
            results.append(ImportResult(text, "existing", paper_id=existing.id))
            continue
        paper, job, _ = _create_paper(session, project_id, csl, MetadataSource.USER)
        results.append(ImportResult(text, "created", paper_id=paper.id, job_id=job.id))
    return results


def import_bibliography(
    session: Session, project_id: str, parsed: ParsedBibliography
) -> list[ImportResult]:
    repo.get_project(session, project_id)
    results = [ImportResult(error, "invalid", message=error) for error in parsed.errors]
    for entry in parsed.entries:
        label = entry.key or str(entry.csl.get("title") or "entry")
        if not (entry.csl.get("title") or entry.csl.get("DOI") or entry.csl.get("arxiv")):
            results.append(
                ImportResult(label, "invalid", message="entry has no title, DOI, or arXiv ID")
            )
            continue
        if existing := _existing(session, project_id, entry.csl):
            results.append(ImportResult(label, "existing", paper_id=existing.id))
            continue
        paper, job, note = _create_paper(
            session, project_id, entry.csl, parsed.source, key=entry.key
        )
        results.append(
            ImportResult(label, "created", paper_id=paper.id, job_id=job.id, message=note)
        )
    return results


# --- enrichment job ---------------------------------------------------------------------


class PdfFetcher:
    """Download an open-access PDF, refusing anything that is not a PDF or is too large."""

    def __init__(self, client: httpx.AsyncClient, max_bytes: int):
        self._client = client
        self.max_bytes = max_bytes

    async def fetch(self, url: str) -> bytes | None:
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        return None
        except httpx.HTTPError as exc:
            log.info("PDF download failed for %s: %s", url, exc)
            return None
        return bytes(data) if data.startswith(b"%PDF") else None


def _pdf_url(records: list[SourceRecord]) -> str | None:
    # Prefer arXiv (always permitted), then an OpenAlex open-access location.
    ordered = sorted(records, key=lambda r: r.source is not MetadataSource.ARXIV)
    return next((r.pdf_url for r in ordered if r.pdf_url), None)


def make_enrich_handler(
    factory: sessionmaker[Session],
    registries: Registries,
    store: ObjectStore,
    fetcher: PdfFetcher,
) -> Callable[[Job], Awaitable[dict[str, Any] | None]]:
    async def enrich_import(job: Job) -> dict[str, Any] | None:
        paper_id = job.inputs["paper_id"]
        with transaction(factory) as session:
            paper = repo.get_paper(session, paper_id)
            if paper.state is not PaperState.IMPORTED:
                return {"skipped": f"paper is {paper.state}"}
            reference = repo.get_reference(session, paper.reference_id)

        records, rejected = await lookup(registries, reference)  # SourceUnavailable -> retry

        with transaction(factory) as session:
            repo.set_paper_state(session, paper_id, PaperState.METADATA_ONLY)
            resolved = apply_resolution(
                session, repo.get_paper(session, paper_id), records, rejected
            )
            if not resolved.csl.get("title"):
                repo.update_issues(
                    session,
                    paper_id,
                    {"metadata"},
                    [PaperIssue(kind="metadata", message="no metadata found for this identifier")],
                )
                return {"state": PaperState.NEEDS_ATTENTION.value, "found": False}

        if (url := _pdf_url(records)) and (pdf := await fetcher.fetch(url)):
            key = f"projects/{paper.project_id}/files/{hashlib.sha256(pdf).hexdigest()}.pdf"
            store.put(key, pdf)
            with transaction(factory) as session:
                repo.add_source_files(session, paper_id, [key])
                queue.enqueue(session, EXTRACT_PDF, {"paper_id": paper_id, "source": key})
            return {"full_text": url, "sources": [r.source.value for r in records]}

        abstract = resolved.csl.get("abstract")
        with transaction(factory) as session:
            if isinstance(abstract, str) and abstract.strip():
                passage = EvidencePassage(
                    paper_id=paper_id,
                    text=abstract,
                    section="Abstract",
                    char_span=(0, len(abstract)),
                )
                repo.replace_passages(session, paper_id, [passage])
                repo.set_evidence_level(session, paper_id, EvidenceLevel.ABSTRACT_ONLY)
                level = EvidenceLevel.ABSTRACT_ONLY
            else:
                level = EvidenceLevel.METADATA_ONLY
        return {"evidence_level": level.value, "sources": [r.source.value for r in records]}

    return enrich_import
