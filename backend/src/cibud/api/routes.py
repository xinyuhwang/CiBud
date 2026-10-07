from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from cibud.api.deps import get_session, get_store
from cibud.db import repo
from cibud.dedup.match import DuplicateKind
from cibud.dedup.service import MergeError, find_duplicates, mark_distinct, merge_papers
from cibud.ingestion.service import InvalidUpload, import_pdf
from cibud.jobs import queue
from cibud.metadata.service import (
    InvalidFieldChoice,
    conflicts_for,
    enqueue_resolution,
    resolve_field,
)
from cibud.models import (
    EvidencePassage,
    Job,
    MetadataSource,
    Paper,
    PaperState,
    Project,
    Reference,
    ResearchProfile,
)
from cibud.settings import get_settings
from cibud.storage import ObjectStore

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
StoreDep = Annotated[ObjectStore, Depends(get_store)]


class ProjectCreate(BaseModel):
    name: str
    profile: ResearchProfile
    default_style: str = "apa"


class ConflictOut(BaseModel):
    field: str
    values: dict[MetadataSource, Any]  # each source's value, highest precedence first


class PaperDetail(BaseModel):
    paper: Paper
    reference: Reference
    conflicts: list[ConflictOut]


class FieldChoice(BaseModel):
    """Resolve a metadata field: pick a source's value, or enter one."""

    source: MetadataSource | None = None
    value: Any = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "FieldChoice":
        if (self.source is None) == (self.value is None):
            raise ValueError("give exactly one of source or value")
        return self


def _paper_detail(session: Session, paper: Paper) -> PaperDetail:
    reference = repo.get_reference(session, paper.reference_id)
    conflicts = [ConflictOut(field=c.field, values=c.values) for c in conflicts_for(reference)]
    return PaperDetail(paper=paper, reference=reference, conflicts=conflicts)


class DuplicateOut(BaseModel):
    paper_id: str
    citation_key: str
    title: str | None
    kind: DuplicateKind


class MergeRequest(BaseModel):
    into: str  # paper ID to keep


class DistinctRequest(BaseModel):
    of: str  # paper ID that is a different work


class UploadResult(BaseModel):
    paper: Paper
    job: Job
    created: bool  # False when the identical file was already in the project


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, session: SessionDep) -> Project:
    project = Project(name=body.name, profile=body.profile, default_style=body.default_style)
    return repo.create_project(session, project)


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: SessionDep) -> Project:
    return repo.get_project(session, project_id)


@router.post("/projects/{project_id}/papers", status_code=status.HTTP_202_ACCEPTED)
def upload_paper(
    project_id: str, file: UploadFile, session: SessionDep, store: StoreDep
) -> UploadResult:
    """Upload a PDF. Extraction runs in the background; poll the paper or job for progress."""
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)
    try:
        paper, job, created = import_pdf(
            session, store, project_id, file.filename or "paper.pdf", data, max_bytes=max_bytes
        )
    except InvalidUpload as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return UploadResult(paper=paper, job=job, created=created)


@router.get("/projects/{project_id}/papers")
def list_papers(project_id: str, session: SessionDep) -> list[Paper]:
    repo.get_project(session, project_id)
    return repo.list_papers(session, project_id)


@router.get("/papers/{paper_id}")
def get_paper(paper_id: str, session: SessionDep) -> PaperDetail:
    return _paper_detail(session, repo.get_paper(session, paper_id))


@router.post("/papers/{paper_id}/resolve-metadata", status_code=status.HTTP_202_ACCEPTED)
def resolve_metadata(paper_id: str, session: SessionDep) -> Job:
    """Re-run the Crossref/OpenAlex/arXiv lookup for a paper."""
    repo.get_paper(session, paper_id)
    return enqueue_resolution(session, paper_id, f"manual:{datetime.now(UTC).isoformat()}")


@router.post("/papers/{paper_id}/exclude")
def exclude_paper(paper_id: str, session: SessionDep) -> PaperDetail:
    return _paper_detail(session, repo.set_paper_state(session, paper_id, PaperState.EXCLUDED))


@router.get("/papers/{paper_id}/duplicates")
def list_duplicates(paper_id: str, session: SessionDep) -> list[DuplicateOut]:
    paper = repo.get_paper(session, paper_id)
    out = []
    for m in find_duplicates(session, repo.get_reference(session, paper.reference_id)):
        other = repo.paper_for_reference(session, m.reference_id)
        if other is None:
            continue
        title = repo.get_reference(session, m.reference_id).csl.get("title")
        out.append(
            DuplicateOut(paper_id=other.id, citation_key=m.citation_key, title=title, kind=m.kind)
        )
    return out


@router.post("/papers/{paper_id}/merge")
def merge_paper(paper_id: str, body: MergeRequest, session: SessionDep) -> PaperDetail:
    """Merge this paper into ``into`` (which is kept); this paper is deleted."""
    try:
        return _paper_detail(session, merge_papers(session, paper_id, body.into))
    except MergeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/papers/{paper_id}/not-duplicate")
def not_duplicate(paper_id: str, body: DistinctRequest, session: SessionDep) -> PaperDetail:
    try:
        mark_distinct(session, paper_id, body.of)
    except MergeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _paper_detail(session, repo.get_paper(session, paper_id))


@router.post("/references/{reference_id}/fields/{field}")
def choose_field(
    reference_id: str, field: str, choice: FieldChoice, session: SessionDep
) -> PaperDetail:
    """Settle a metadata conflict (or correct a field). Recorded with USER provenance."""
    try:
        resolve_field(session, reference_id, field, value=choice.value, source=choice.source)
    except InvalidFieldChoice as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    paper = repo.paper_for_reference(session, reference_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no paper for this reference")
    return _paper_detail(session, paper)


@router.get("/papers/{paper_id}/passages")
def list_passages(paper_id: str, session: SessionDep) -> list[EvidencePassage]:
    repo.get_paper(session, paper_id)
    return repo.list_passages(session, paper_id)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: SessionDep) -> Job:
    job = queue.get(session, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return job


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, session: SessionDep) -> Job:
    if queue.get(session, job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    try:
        return queue.retry(session, job_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
