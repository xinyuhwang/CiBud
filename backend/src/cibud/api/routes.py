from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cibud.api.deps import get_session, get_store
from cibud.db import repo
from cibud.ingestion.service import InvalidUpload, import_pdf
from cibud.jobs import queue
from cibud.models import EvidencePassage, Job, Paper, Project, Reference, ResearchProfile
from cibud.settings import get_settings
from cibud.storage import ObjectStore

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
StoreDep = Annotated[ObjectStore, Depends(get_store)]


class ProjectCreate(BaseModel):
    name: str
    profile: ResearchProfile
    default_style: str = "apa"


class PaperDetail(BaseModel):
    paper: Paper
    reference: Reference


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
    paper = repo.get_paper(session, paper_id)
    return PaperDetail(paper=paper, reference=repo.get_reference(session, paper.reference_id))


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
