from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cibud.api.deps import get_session, get_store
from cibud.api.main import create_app
from cibud.db import repo
from cibud.db.session import transaction
from cibud.db.tables import DocumentRow
from cibud.dedup.match import DuplicateKind, match
from cibud.dedup.service import MergeError, find_duplicates, mark_distinct, merge_papers
from cibud.metadata.normalize import normalize_csl
from cibud.metadata.service import apply_resolution
from cibud.models import (
    EvidenceLevel,
    EvidencePassage,
    FieldCandidate,
    MetadataSource,
    Paper,
    PaperState,
    Project,
    Reference,
    ResearchProfile,
)
from cibud.storage import LocalObjectStore

TITLE = "Attention-Based Sepsis Prediction from Electronic Health Records"
CHEN = [{"family": "Chen", "given": "Lin"}, {"family": "Ito", "given": "Kai"}]


def csl(**fields: Any) -> dict[str, Any]:
    return {"title": TITLE, "author": CHEN, "issued": {"date-parts": [[2022]]}, **fields}


PUBLISHED = csl(DOI="10.1000/jci.2022.42", **{"container-title": "J Clin Inform"})
PREPRINT = csl(
    arxiv="2103.01234",
    DOI="10.48550/arxiv.2103.01234",
    issued={"date-parts": [[2021]]},
)


class TestMatch:
    def test_same_doi_ignores_case_and_prefix(self) -> None:
        other = {"DOI": "https://doi.org/10.1000/JCI.2022.42", "title": "Totally different"}
        assert match(PUBLISHED, other) is DuplicateKind.SAME_DOI

    def test_same_arxiv(self) -> None:
        assert match({"arxiv": "2103.01234"}, {"arxiv": "2103.01234"}) is DuplicateKind.SAME_ARXIV

    def test_preprint_reports_published_doi(self) -> None:
        preprint = {"arxiv": "2103.01234", "published-doi": "10.1000/jci.2022.42", "title": "x"}
        assert match(preprint, PUBLISHED) is DuplicateKind.VERSION
        assert match(PUBLISHED, preprint) is DuplicateKind.VERSION

    def test_preprint_and_published_by_title(self) -> None:
        assert match(PREPRINT, PUBLISHED) is DuplicateKind.VERSION

    def test_same_title_author_year_without_identifiers(self) -> None:
        assert match(csl(), csl(title=TITLE.upper() + ".")) is DuplicateKind.SIMILAR

    def test_near_identical_titles_by_different_authors_are_not_duplicates(self) -> None:
        """Seeded defect from design doc §16: two papers with near-identical titles."""
        other = csl(author=[{"family": "Okafor"}])
        assert match(csl(), other) is None

    def test_years_far_apart_are_not_duplicates(self) -> None:
        assert match(csl(), csl(issued={"date-parts": [[2015]]})) is None

    def test_different_titles(self) -> None:
        assert match(csl(), csl(title="Graph attention over diagnosis codes")) is None


class TestNormalize:
    def test_values(self) -> None:
        out = normalize_csl(
            {
                "title": "  A   Title ",
                "author": [
                    {"family": "CHEN", "given": "LIN"},
                    {"family": "AL-KINDI"},
                    {"family": "McDonald"},
                ],
                "container-title": "J. Clin.  Inform.",
                "page": "pp. 103790\u2013103795",
                "DOI": "https://doi.org/10.1000/ABC",
            }
        )
        assert out["title"] == "A Title"
        assert [a["family"] for a in out["author"]] == ["Chen", "Al-Kindi", "McDonald"]
        assert out["author"][0]["given"] == "Lin"
        assert out["container-title"] == "J. Clin. Inform"
        assert out["page"] == "103790-103795"
        assert out["DOI"] == "10.1000/abc"

    def test_leaves_unknown_fields_alone(self) -> None:
        assert normalize_csl({"note": "  x  "}) == {"note": "  x  "}


# --- service ----------------------------------------------------------------------------


@pytest.fixture
def project_id(factory: sessionmaker[Session]) -> str:
    profile = ResearchProfile(problem="p", method="m", data="d", contribution="c")
    with transaction(factory) as s:
        return repo.create_project(s, Project(name="T", profile=profile)).id


def add_paper(
    session: Session,
    project_id: str,
    key: str,
    data: dict[str, Any],
    *,
    full_text: bool = False,
    source: MetadataSource = MetadataSource.CROSSREF,
) -> Paper:
    ref = repo.add_reference(
        session,
        Reference(
            project_id=project_id,
            citation_key=key,
            csl=data,
            field_provenance={f: [FieldCandidate(value=v, source=source)] for f, v in data.items()},
        ),
    )
    paper = repo.add_paper(
        session,
        Paper(
            project_id=project_id,
            reference_id=ref.id,
            source_files=[f"files/{key}.pdf"],
            extracted_text_ref=f"{key}/tei.xml" if full_text else None,
            evidence_level=EvidenceLevel.FULL_TEXT if full_text else EvidenceLevel.METADATA_ONLY,
        ),
    )
    if full_text:
        repo.set_paper_state(session, paper.id, PaperState.EXTRACTING)
        repo.set_paper_state(session, paper.id, PaperState.METADATA_REVIEW)
        repo.replace_passages(
            session,
            paper.id,
            [EvidencePassage(paper_id=paper.id, text="AUROC 0.84", char_span=(0, 10))],
        )
    else:
        repo.set_paper_state(session, paper.id, PaperState.METADATA_ONLY)
    return repo.get_paper(session, paper.id)


def flag(session: Session, paper: Paper) -> Paper:
    """Run the post-resolution step, which is where duplicates get flagged."""
    apply_resolution(session, paper, [], [])
    return repo.get_paper(session, paper.id)


def test_resolution_flags_the_newcomer(session: Session, project_id: str) -> None:
    published = add_paper(session, project_id, "chen2022attention", PUBLISHED)
    preprint = add_paper(session, project_id, "chen2021attention", PREPRINT, full_text=True,
                         source=MetadataSource.ARXIV)  # fmt: skip

    flagged = flag(session, preprint)
    assert flagged.state is PaperState.NEEDS_ATTENTION
    [issue] = flagged.issues
    assert issue.kind == "duplicate"
    assert issue.message == (
        "possible duplicate of chen2022attention: preprint and published version of the same work"
    )
    assert [
        m.reference_id
        for m in find_duplicates(session, repo.get_reference(session, preprint.reference_id))
    ] == [published.reference_id]


def test_merge_preprint_into_published(session: Session, project_id: str) -> None:
    published = add_paper(session, project_id, "chen2022attention", PUBLISHED)
    preprint = add_paper(session, project_id, "chen2021attention", PREPRINT, full_text=True,
                         source=MetadataSource.ARXIV)  # fmt: skip
    flag(session, preprint)

    kept = merge_papers(session, preprint.id, published.id)

    # Published metadata wins; the arXiv ID is gained; nothing uploaded is lost.
    ref = repo.get_reference(session, kept.reference_id)
    assert ref.csl["DOI"] == "10.1000/jci.2022.42"
    assert ref.csl["issued"] == {"date-parts": [[2022]]}
    assert ref.csl["arxiv"] == "2103.01234"
    assert set(kept.source_files) == {"files/chen2022attention.pdf", "files/chen2021attention.pdf"}
    # The preprint's full text now backs the published paper.
    assert kept.evidence_level is EvidenceLevel.FULL_TEXT
    assert kept.state is PaperState.METADATA_REVIEW
    assert [p.text for p in repo.list_passages(session, kept.id)] == ["AUROC 0.84"]
    # The preprint is gone, and nothing is still flagged.
    assert [p.id for p in repo.list_papers(session, project_id)] == [published.id]
    assert kept.issues == []


def test_mark_distinct_clears_and_prevents_flags(session: Session, project_id: str) -> None:
    a = add_paper(session, project_id, "chen2022a", csl(DOI="10.1/a"), full_text=True)
    b = add_paper(session, project_id, "chen2022b", csl(DOI="10.1/b"), full_text=True)
    assert flag(session, b).state is PaperState.NEEDS_ATTENTION

    mark_distinct(session, b.id, a.id)
    assert repo.get_paper(session, b.id).state is PaperState.METADATA_REVIEW
    assert flag(session, b).issues == []
    assert flag(session, a).issues == []


def test_cannot_merge_away_a_cited_paper(session: Session, project_id: str) -> None:
    a = add_paper(session, project_id, "a", csl(DOI="10.1/a"))
    b = add_paper(session, project_id, "b", csl(DOI="10.1/a"))
    content = {"type": "citation", "attrs": {"items": [{"ref": b.reference_id}]}}
    session.add(DocumentRow(id="doc_1", project_id=project_id, content=content))
    session.flush()
    with pytest.raises(MergeError, match="cited"):
        merge_papers(session, b.id, a.id)
    merge_papers(session, a.id, b.id)  # the other direction is fine


def test_merge_requires_two_papers(session: Session, project_id: str) -> None:
    a = add_paper(session, project_id, "a", csl())
    with pytest.raises(MergeError):
        merge_papers(session, a.id, a.id)


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()

    def session() -> Iterator[Session]:
        with factory() as s, s.begin():
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_store] = lambda: LocalObjectStore(tmp_path)
    yield TestClient(app)


def test_api(client: TestClient, factory: sessionmaker[Session], project_id: str) -> None:
    with transaction(factory) as s:
        published = add_paper(s, project_id, "chen2022attention", PUBLISHED)
        preprint = add_paper(s, project_id, "chen2021attention", PREPRINT, full_text=True)
        other = add_paper(
            s, project_id, "okafor2022", csl(author=[{"family": "Okafor"}], DOI="10.1/o")
        )

    [dup] = client.get(f"/papers/{preprint.id}/duplicates").json()
    assert (dup["paper_id"], dup["kind"]) == (published.id, "version")
    assert client.get(f"/papers/{other.id}/duplicates").json() == []

    resp = client.post(f"/papers/{preprint.id}/merge", json={"into": published.id})
    assert resp.status_code == 200
    assert resp.json()["paper"]["evidence_level"] == "full_text"
    assert client.get(f"/papers/{preprint.id}").status_code == 404

    bad = client.post(f"/papers/{other.id}/not-duplicate", json={"of": other.id})
    assert bad.status_code == 409
