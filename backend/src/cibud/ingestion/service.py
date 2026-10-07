"""PDF ingestion: upload → background extraction with GROBID → evidence passages (§7).

``import_pdf`` runs in the request: it stores the file, creates the Paper and a placeholder
Reference, and enqueues an ``extract_pdf`` job. The job handler does the slow work.
"""

import hashlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cibud.db import repo
from cibud.db.session import transaction
from cibud.ingestion.chunking import chunk
from cibud.ingestion.citation_keys import base_key, unique_key
from cibud.ingestion.grobid import GrobidClient, GrobidError, GrobidUnavailable
from cibud.ingestion.tei import HeaderMetadata, ParsedPaper, parse_tei
from cibud.jobs import queue
from cibud.metadata.normalize import normalize_csl
from cibud.metadata.service import enqueue_resolution
from cibud.models.common import EvidenceLevel, MetadataSource, new_id
from cibud.models.evidence import EvidencePassage
from cibud.models.job import Job
from cibud.models.paper import Paper, PaperIssue, PaperState
from cibud.models.reference import FieldCandidate, Reference
from cibud.storage import ObjectStore, paper_key

log = logging.getLogger(__name__)

EXTRACT_PDF = "extract_pdf"
PLACEHOLDER_KEY_PREFIX = "pending_"


class InvalidUpload(ValueError):
    pass


def _source_key(project_id: str, data: bytes) -> str:
    # Content-addressed, so uploading the same file twice is detected and never duplicated.
    return f"projects/{project_id}/files/{hashlib.sha256(data).hexdigest()}.pdf"


def import_pdf(
    session: Session,
    store: ObjectStore,
    project_id: str,
    filename: str,
    data: bytes,
    *,
    max_bytes: int,
) -> tuple[Paper, Job, bool]:
    """Register an uploaded PDF. Returns (paper, extraction job, created)."""
    if not data.startswith(b"%PDF"):
        raise InvalidUpload("file is not a PDF")
    if len(data) > max_bytes:
        raise InvalidUpload(f"file exceeds {max_bytes // (1024 * 1024)} MB")
    repo.get_project(session, project_id)  # raises NotFound

    source_key = _source_key(project_id, data)
    existing = repo.find_paper_by_source(session, project_id, source_key)
    if existing is not None:
        job = queue.enqueue(session, EXTRACT_PDF, {"paper_id": existing.id})
        return existing, job, False

    store.put(source_key, data)
    paper_id = new_id("paper")
    reference = repo.add_reference(
        session,
        Reference(
            project_id=project_id,
            citation_key=f"{PLACEHOLDER_KEY_PREFIX}{paper_id}",
            csl={"type": "article", "title": filename.removesuffix(".pdf")},
        ),
    )
    paper = repo.add_paper(
        session,
        Paper(
            id=paper_id,
            project_id=project_id,
            reference_id=reference.id,
            source_files=[source_key],
        ),
    )
    job = queue.enqueue(session, EXTRACT_PDF, {"paper_id": paper.id})
    return paper, job, True


def header_to_csl(header: HeaderMetadata) -> dict[str, Any]:
    csl: dict[str, Any] = {"type": "article-journal" if header.venue else "article"}
    if header.title:
        csl["title"] = header.title
    if header.authors:
        csl["author"] = [
            {"family": a.family, **({"given": a.given} if a.given else {})} for a in header.authors
        ]
    if header.date:
        csl["issued"] = {"date-parts": [[int(p) for p in header.date.split("-") if p.isdigit()]]}
    if header.doi:
        csl["DOI"] = header.doi
    if header.venue:
        csl["container-title"] = header.venue
    if header.arxiv_id:
        csl["arxiv"] = header.arxiv_id  # not a standard CSL field; used for resolution
    if header.abstract:
        csl["abstract"] = header.abstract
    return csl


def apply_header(reference: Reference, header: HeaderMetadata, taken_keys: set[str]) -> Reference:
    """Fill the reference from the PDF header, recording PDF_HEADER provenance per field.

    Candidates from other sources (Crossref, user edits) are kept; only previous PDF_HEADER
    candidates are replaced, so re-extraction is idempotent. A real citation key replaces
    the placeholder once, and is never changed after that.
    """
    header_csl = header_to_csl(header)
    provenance = {
        field: [c for c in candidates if c.source is not MetadataSource.PDF_HEADER]
        for field, candidates in reference.field_provenance.items()
    }
    for field, value in header_csl.items():
        if field == "type":
            continue
        provenance.setdefault(field, []).append(
            FieldCandidate(value=value, source=MetadataSource.PDF_HEADER)
        )
    # Fields that already came from a better source keep their value (resolver decides, §7.3).
    other_sourced = {
        f
        for f, cands in provenance.items()
        if any(c.source is not MetadataSource.PDF_HEADER for c in cands)
    }
    csl = {**header_csl, **{f: v for f, v in reference.csl.items() if f in other_sourced}}
    if "title" not in csl and "title" in reference.csl:
        csl["title"] = reference.csl["title"]  # keep the filename placeholder
    csl = normalize_csl(csl)

    key = reference.citation_key
    if key.startswith(PLACEHOLDER_KEY_PREFIX):
        first_author = header.authors[0].family if header.authors else None
        key = unique_key(base_key(first_author, header.year, header.title), taken_keys - {key})
    return reference.model_copy(
        update={"csl": csl, "field_provenance": provenance, "citation_key": key}
    )


def build_passages(paper_id: str, parsed: ParsedPaper) -> tuple[str, list[EvidencePassage]]:
    chunked = chunk(parsed)
    passages = [
        EvidencePassage(
            paper_id=paper_id,
            ordinal=i,
            text=d.text,
            page=d.page,
            section=d.section,
            char_span=d.char_span,
            bboxes=d.bboxes,
        )
        for i, d in enumerate(chunked.passages)
    ]
    return chunked.text, passages


# Papers in these states may (re-)enter extraction.
_EXTRACTABLE = {
    PaperState.IMPORTED,
    PaperState.EXTRACTING,  # a reclaimed job after a worker crash
    PaperState.NEEDS_ATTENTION,
    PaperState.METADATA_ONLY,
}


def make_extract_handler(
    factory: sessionmaker[Session], store: ObjectStore, grobid: GrobidClient
) -> Callable[[Job], Awaitable[dict[str, Any] | None]]:
    def needs_attention(paper_id: str, issue: str) -> dict[str, Any]:
        with transaction(factory) as session:
            repo.update_issues(
                session, paper_id, {"extraction"}, [PaperIssue(kind="extraction", message=issue)]
            )
        return {"state": PaperState.NEEDS_ATTENTION.value, "issue": issue}

    async def extract_pdf(job: Job) -> dict[str, Any] | None:
        try:
            return await _extract(job)
        except Exception as exc:
            # Never leave a paper stuck in EXTRACTING once retries are used up.
            if job.attempts >= job.max_attempts and not isinstance(exc, GrobidUnavailable):
                try:
                    needs_attention(job.inputs["paper_id"], f"extraction error: {exc}")
                except Exception:
                    log.exception("could not flag paper %s", job.inputs["paper_id"])
            raise

    async def _extract(job: Job) -> dict[str, Any] | None:
        paper_id = job.inputs["paper_id"]
        with transaction(factory) as session:
            paper = repo.get_paper(session, paper_id)
            if paper.state not in _EXTRACTABLE:
                return {"skipped": f"paper is {paper.state}"}
            if paper.state is not PaperState.EXTRACTING:
                repo.set_paper_state(session, paper_id, PaperState.EXTRACTING)

        pdf = store.get(paper.source_files[-1])
        try:
            tei = await grobid.process_fulltext(pdf)
        except GrobidUnavailable as exc:
            if job.attempts >= job.max_attempts:
                needs_attention(paper_id, f"GROBID unavailable: {exc}")
            raise  # transient: let the queue retry with backoff
        except GrobidError as exc:
            return needs_attention(paper_id, f"PDF extraction failed: {exc}")

        try:
            parsed = parse_tei(tei)
        except ET.ParseError as exc:
            return needs_attention(paper_id, f"could not parse GROBID output: {exc}")

        if parsed.sentence_count("body") == 0:
            # Likely a scanned PDF. OCR fallback is a planned 1A item.
            return needs_attention(
                paper_id, "no extractable body text (scanned PDF?); OCR is not supported yet"
            )

        tei_key = paper_key(paper.project_id, paper_id, "grobid.tei.xml")
        text_key = paper_key(paper.project_id, paper_id, "text.txt")
        text, passages = build_passages(paper_id, parsed)
        store.put(tei_key, tei.encode())
        store.put(text_key, text.encode())

        with transaction(factory) as session:
            reference = repo.get_reference(session, paper.reference_id)
            taken = repo.citation_keys(session, paper.project_id)
            repo.save_reference(session, apply_header(reference, parsed.header, taken))
            repo.replace_passages(session, paper_id, passages)
            repo.record_extraction(
                session,
                paper_id,
                extracted_text_ref=tei_key,
                evidence_level=EvidenceLevel.FULL_TEXT,
            )
            repo.set_paper_state(session, paper_id, PaperState.METADATA_REVIEW)
            tei_digest = hashlib.sha256(tei.encode()).hexdigest()[:16]
            enqueue_resolution(session, paper_id, f"extracted:{tei_digest}")

        log.info("extracted %s: %d passages", paper_id, len(passages))
        return {
            "state": PaperState.METADATA_REVIEW.value,
            "passages": len(passages),
            "references_in_paper": len(parsed.bibliography),
        }

    return extract_pdf
