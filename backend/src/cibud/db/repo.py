"""Persistence for projects, references, and papers.

Domain code works with Pydantic models; this module converts to and from table rows and
enforces invariants that span rows (profile versioning, paper state transitions).
"""

from typing import Any

from sqlalchemy import String, cast, delete, func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from cibud.db.tables import (
    ClaimRow,
    DocumentRow,
    EvidencePassageRow,
    PaperRow,
    ProjectRow,
    ReferenceRow,
    ResearchProfileRow,
)
from cibud.models.common import EvidenceLevel
from cibud.models.evidence import EvidencePassage
from cibud.models.paper import Paper, PaperState, check_transition
from cibud.models.project import Project, ResearchProfile
from cibud.models.reference import Reference


class NotFound(LookupError):
    pass


# --- projects -------------------------------------------------------------------------


def create_project(session: Session, project: Project) -> Project:
    session.add(
        ProjectRow(
            id=project.id,
            name=project.name,
            default_style=project.default_style,
            settings=project.settings,
        )
    )
    profile = project.profile.model_copy(update={"version": 1})
    session.add(ResearchProfileRow(project_id=project.id, **profile.model_dump()))
    session.flush()
    return project.model_copy(update={"profile": profile})


def get_project(session: Session, project_id: str) -> Project:
    row = session.get(ProjectRow, project_id)
    if row is None:
        raise NotFound(f"project {project_id}")
    return Project(
        id=row.id,
        name=row.name,
        default_style=row.default_style,
        settings=row.settings,
        profile=current_profile(session, project_id),
    )


def current_profile(session: Session, project_id: str) -> ResearchProfile:
    row = session.scalars(
        select(ResearchProfileRow)
        .where(ResearchProfileRow.project_id == project_id)
        .order_by(ResearchProfileRow.version.desc())
        .limit(1)
    ).first()
    if row is None:
        raise NotFound(f"profile for project {project_id}")
    return ResearchProfile.model_validate(row, from_attributes=True)


def update_profile(session: Session, project_id: str, profile: ResearchProfile) -> ResearchProfile:
    """Store an edited profile as a new version. Earlier versions are kept (§6)."""
    # Lock the project row so concurrent edits can't both claim the same next version.
    locked = session.scalar(
        select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update()
    )
    if locked is None:
        raise NotFound(f"project {project_id}")
    latest = session.scalar(
        select(func.max(ResearchProfileRow.version)).where(
            ResearchProfileRow.project_id == project_id
        )
    )
    if latest is None:
        raise NotFound(f"profile for project {project_id}")
    new = profile.model_copy(update={"version": latest + 1})
    session.add(ResearchProfileRow(project_id=project_id, **new.model_dump()))
    session.flush()
    return new


# --- references and papers ------------------------------------------------------------


def _normalized_doi(csl: dict[str, Any]) -> str | None:
    doi = csl.get("DOI")
    return str(doi).strip().lower() if doi else None


def _reference_columns(reference: Reference) -> dict[str, Any]:
    return {
        "citation_key": reference.citation_key,
        "doi": _normalized_doi(reference.csl),
        "csl": reference.csl,
        "field_provenance": {
            field: [c.model_dump(mode="json") for c in candidates]
            for field, candidates in reference.field_provenance.items()
        },
        "verification": reference.verification.model_dump(mode="json"),
    }


def add_reference(session: Session, reference: Reference) -> Reference:
    session.add(
        ReferenceRow(
            id=reference.id, project_id=reference.project_id, **_reference_columns(reference)
        )
    )
    session.flush()
    return reference


def save_reference(session: Session, reference: Reference) -> Reference:
    """Overwrite an existing reference's bibliographic data."""
    row = session.get(ReferenceRow, reference.id, with_for_update=True)
    if row is None:
        raise NotFound(f"reference {reference.id}")
    for column, value in _reference_columns(reference).items():
        setattr(row, column, value)
    session.flush()
    return reference


def is_reference_cited(session: Session, reference_id: str) -> bool:
    """True once any claim or document cites the reference; its citation key is then frozen."""
    in_claims = select(ClaimRow.id).where(
        type_coerce(ClaimRow.reference_ids, JSONB).contains([reference_id])
    )
    # Citation nodes store {"ref": "<id>"}; IDs are unique random tokens, so a text search
    # over the document JSON cannot produce false positives in practice.
    in_documents = select(DocumentRow.id).where(
        cast(DocumentRow.content, String).contains(f'"{reference_id}"')
    )
    return bool(
        session.scalar(select(in_claims.exists())) or session.scalar(select(in_documents.exists()))
    )


def citation_keys(session: Session, project_id: str) -> set[str]:
    return set(
        session.scalars(
            select(ReferenceRow.citation_key).where(ReferenceRow.project_id == project_id)
        )
    )


def get_reference(session: Session, reference_id: str) -> Reference:
    row = session.get(ReferenceRow, reference_id)
    if row is None:
        raise NotFound(f"reference {reference_id}")
    return Reference.model_validate(row, from_attributes=True)


def add_paper(session: Session, paper: Paper) -> Paper:
    session.add(PaperRow(**paper.model_dump()))
    session.flush()
    return paper


def get_paper(session: Session, paper_id: str) -> Paper:
    row = session.get(PaperRow, paper_id)
    if row is None:
        raise NotFound(f"paper {paper_id}")
    return Paper.model_validate(row, from_attributes=True)


def paper_for_reference(session: Session, reference_id: str) -> Paper | None:
    row = session.scalars(select(PaperRow).where(PaperRow.reference_id == reference_id)).first()
    return Paper.model_validate(row, from_attributes=True) if row else None


def list_papers(session: Session, project_id: str, state: PaperState | None = None) -> list[Paper]:
    stmt = select(PaperRow).where(PaperRow.project_id == project_id)
    if state is not None:
        stmt = stmt.where(PaperRow.state == state)
    rows = session.scalars(stmt.order_by(PaperRow.created_at, PaperRow.id))
    return [Paper.model_validate(r, from_attributes=True) for r in rows]


def find_paper_by_source(session: Session, project_id: str, source_key: str) -> Paper | None:
    """The paper an uploaded file already belongs to, if any (exact-duplicate uploads)."""
    row = session.scalars(
        select(PaperRow).where(
            PaperRow.project_id == project_id,
            type_coerce(PaperRow.source_files, JSONB).contains([source_key]),
        )
    ).first()
    return Paper.model_validate(row, from_attributes=True) if row else None


def record_extraction(
    session: Session, paper_id: str, *, extracted_text_ref: str, evidence_level: EvidenceLevel
) -> None:
    row = session.get(PaperRow, paper_id, with_for_update=True)
    if row is None:
        raise NotFound(f"paper {paper_id}")
    row.extracted_text_ref = extracted_text_ref
    row.evidence_level = evidence_level
    session.flush()


def flag_paper(session: Session, paper_id: str, issues: list[str]) -> Paper:
    """Put a paper in NeedsAttention with exactly these issues (replacing earlier ones)."""
    row = session.scalars(select(PaperRow).where(PaperRow.id == paper_id).with_for_update()).first()
    if row is None:
        raise NotFound(f"paper {paper_id}")
    if row.state is not PaperState.NEEDS_ATTENTION:
        check_transition(row.state, PaperState.NEEDS_ATTENTION)
        row.state = PaperState.NEEDS_ATTENTION
    row.issues = list(issues)
    session.flush()
    return Paper.model_validate(row, from_attributes=True)


def set_paper_state(
    session: Session, paper_id: str, target: PaperState, *, issue: str | None = None
) -> Paper:
    """Move a paper through its lifecycle. Raises ``InvalidTransition`` on illegal moves."""
    row = session.scalars(select(PaperRow).where(PaperRow.id == paper_id).with_for_update()).first()
    if row is None:
        raise NotFound(f"paper {paper_id}")
    check_transition(row.state, target)
    row.state = target
    if issue:
        row.issues = [*row.issues, issue]
    elif target is not PaperState.NEEDS_ATTENTION:
        row.issues = []
    session.flush()
    return Paper.model_validate(row, from_attributes=True)


# --- evidence passages ----------------------------------------------------------------


def replace_passages(
    session: Session, paper_id: str, passages: list[EvidencePassage]
) -> list[EvidencePassage]:
    """Replace a paper's passages (re-extraction must not duplicate evidence)."""
    session.execute(delete(EvidencePassageRow).where(EvidencePassageRow.paper_id == paper_id))
    session.add_all(
        EvidencePassageRow(
            id=p.id,
            paper_id=paper_id,
            ordinal=p.ordinal,
            text=p.text,
            page=p.page,
            section=p.section,
            char_start=p.char_span[0],
            char_end=p.char_span[1],
            bboxes=[b.model_dump() for b in p.bboxes],
        )
        for p in passages
    )
    session.flush()
    return passages


def list_passages(session: Session, paper_id: str) -> list[EvidencePassage]:
    rows = session.scalars(
        select(EvidencePassageRow)
        .where(EvidencePassageRow.paper_id == paper_id)
        .order_by(EvidencePassageRow.ordinal)
    )
    return [
        EvidencePassage(
            id=r.id,
            paper_id=r.paper_id,
            ordinal=r.ordinal,
            text=r.text,
            page=r.page,
            section=r.section,
            char_span=(r.char_start, r.char_end),
            bboxes=r.bboxes,  # JSON dicts, validated into BoundingBox
        )
        for r in rows
    ]
