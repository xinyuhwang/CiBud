import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cibud.db import repo
from cibud.db.tables import PaperRow, ProjectRow, ReferenceRow
from cibud.models import (
    EvidenceLevel,
    FieldCandidate,
    MetadataSource,
    Paper,
    PaperState,
    Project,
    Reference,
    ResearchProfile,
)
from cibud.models.paper import InvalidTransition, PaperIssue

PROFILE = ResearchProfile(
    problem="Early sepsis prediction from EHR data",
    method="Transformer over irregular time series",
    data="MIMIC-IV",
    contribution="Calibrated risk scores",
)


def make_project(session: Session) -> Project:
    return repo.create_project(session, Project(name="Sepsis", profile=PROFILE))


def make_paper(session: Session, project: Project, key: str = "chen2021") -> Paper:
    ref = repo.add_reference(
        session,
        Reference(
            project_id=project.id,
            citation_key=key,
            csl={"title": "A paper", "DOI": " 10.1000/ABC "},
            field_provenance={
                "title": [FieldCandidate(value="A paper", source=MetadataSource.CROSSREF)]
            },
        ),
    )
    return repo.add_paper(session, Paper(project_id=project.id, reference_id=ref.id))


def test_project_round_trip_with_profile_versions(session: Session) -> None:
    project = make_project(session)
    assert repo.get_project(session, project.id).profile.version == 1

    edited = PROFILE.model_copy(update={"data": "MIMIC-IV and eICU"})
    assert repo.update_profile(session, project.id, edited).version == 2

    loaded = repo.get_project(session, project.id)
    assert loaded.profile.version == 2
    assert loaded.profile.data == "MIMIC-IV and eICU"


def test_missing_project(session: Session) -> None:
    with pytest.raises(repo.NotFound):
        repo.get_project(session, "proj_nope")


def test_reference_round_trip_and_doi_normalization(session: Session) -> None:
    project = make_project(session)
    paper = make_paper(session, project)
    ref = repo.get_reference(session, paper.reference_id)
    assert ref.field_provenance["title"][0].source is MetadataSource.CROSSREF
    stored_doi = session.scalar(select(ReferenceRow.doi).where(ReferenceRow.id == ref.id))
    assert stored_doi == "10.1000/abc"


def test_citation_key_is_unique_per_project(session: Session) -> None:
    project = make_project(session)
    make_paper(session, project, key="dup")
    with pytest.raises(IntegrityError):
        make_paper(session, project, key="dup")


def test_paper_defaults_and_listing(session: Session) -> None:
    project = make_project(session)
    paper = make_paper(session, project)
    loaded = repo.get_paper(session, paper.id)
    assert loaded.state is PaperState.IMPORTED
    assert loaded.evidence_level is EvidenceLevel.METADATA_ONLY
    assert [p.id for p in repo.list_papers(session, project.id)] == [paper.id]
    assert repo.list_papers(session, project.id, PaperState.APPROVED) == []


def test_state_transitions_are_enforced(session: Session) -> None:
    project = make_project(session)
    paper = make_paper(session, project)

    repo.set_paper_state(session, paper.id, PaperState.EXTRACTING)
    timeout = PaperIssue(kind="extraction", message="GROBID timeout")
    flagged = repo.update_issues(session, paper.id, {"extraction"}, [timeout])
    assert flagged.state is PaperState.NEEDS_ATTENTION
    assert flagged.issues == [timeout]

    retried = repo.set_paper_state(session, paper.id, PaperState.EXTRACTING)
    assert retried.issues == []

    with pytest.raises(InvalidTransition):
        repo.set_paper_state(session, paper.id, PaperState.APPROVED)
    with pytest.raises(ValueError, match="update_issues"):
        repo.set_paper_state(session, paper.id, PaperState.NEEDS_ATTENTION)


def test_issue_kinds_are_replaced_independently(session: Session) -> None:
    """A clean metadata lookup must not erase an extraction failure."""
    project = make_project(session)
    paper = make_paper(session, project)
    repo.set_paper_state(session, paper.id, PaperState.EXTRACTING)
    failed = PaperIssue(kind="extraction", message="GROBID failed")
    conflict = PaperIssue(kind="metadata", message="year disagrees")
    repo.update_issues(session, paper.id, {"extraction"}, [failed])
    repo.update_issues(session, paper.id, {"metadata"}, [conflict])

    after = repo.update_issues(session, paper.id, {"metadata"}, [])
    assert after.state is PaperState.NEEDS_ATTENTION
    assert after.issues == [failed]

    # No extracted text, so once every issue is gone the paper is metadata-only.
    cleared = repo.update_issues(session, paper.id, {"extraction"}, [])
    assert cleared.state is PaperState.METADATA_ONLY
    assert cleared.issues == []


def test_deleting_project_cascades(session: Session) -> None:
    project = make_project(session)
    make_paper(session, project)
    session.execute(delete(ProjectRow).where(ProjectRow.id == project.id))
    assert session.scalars(select(PaperRow)).all() == []
    assert session.scalars(select(ReferenceRow)).all() == []
