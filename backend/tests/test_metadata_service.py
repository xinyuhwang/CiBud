"""Metadata resolution against Postgres, with registries mocked at the HTTP layer."""

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from test_metadata import ARXIV_ATOM, CROSSREF_MESSAGE, OPENALEX_WORK, TITLE

from cibud.api.deps import get_session, get_store
from cibud.api.main import create_app
from cibud.db import repo
from cibud.db.session import transaction
from cibud.db.tables import DocumentRow
from cibud.jobs import queue
from cibud.metadata.service import (
    RESOLVE_METADATA,
    lookup,
    make_resolve_handler,
    resolve_field,
)
from cibud.metadata.sources import Registries, SourceUnavailable
from cibud.models import (
    FieldCandidate,
    MetadataSource,
    Paper,
    PaperState,
    Project,
    Reference,
    ResearchProfile,
)
from cibud.models.job import Job
from cibud.models.reference import VerificationStatus
from cibud.storage import LocalObjectStore

S = MetadataSource
DOI = "10.1000/jci.2022.42"
Route = Callable[[httpx.Request], httpx.Response]


def registries(routes: Mapping[str, Route | int]) -> Registries:
    """Route requests by 'host path' prefix; unmatched requests return 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        for prefix, route in routes.items():
            if key.startswith(prefix):
                return httpx.Response(route) if isinstance(route, int) else route(request)
        return httpx.Response(404)

    return Registries(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok_json(body: Any) -> Route:
    return lambda request: httpx.Response(200, text=json.dumps(body))


def ok_text(body: str) -> Route:
    return lambda request: httpx.Response(200, text=body)


CROSSREF_BY_DOI = {f"api.crossref.org/works/{DOI}": ok_json({"message": CROSSREF_MESSAGE})}
OPENALEX_BY_DOI = {f"api.openalex.org/works/doi:{DOI}": ok_json(OPENALEX_WORK)}
ARXIV = {"export.arxiv.org/api/query": ok_text(ARXIV_ATOM)}


def reference(**header: Any) -> Reference:
    """A reference as extraction leaves it: CSL and PDF_HEADER provenance for each field."""
    return Reference(
        project_id="proj_x",
        citation_key="chennd" + "attention",
        csl={"type": "article", **header},
        field_provenance={
            f: [FieldCandidate(value=v, source=S.PDF_HEADER)] for f, v in header.items()
        },
    )


class TestLookup:
    async def test_by_doi(self) -> None:
        reg = registries({**CROSSREF_BY_DOI, **OPENALEX_BY_DOI})
        records, rejected = await lookup(reg, reference(title=TITLE, DOI=DOI))
        assert [r.source for r in records] == [S.CROSSREF, S.OPENALEX]
        assert rejected == []

    async def test_by_arxiv_id(self) -> None:
        reg = registries(ARXIV)  # OpenAlex 404s for the arXiv DOI
        records, _ = await lookup(reg, reference(title=TITLE, arxiv="2103.01234"))
        assert [r.source for r in records] == [S.ARXIV]

    async def test_wrong_doi_is_rejected_not_merged(self) -> None:
        other = {**CROSSREF_MESSAGE, "title": ["Deep learning for protein structure"]}
        reg = registries({f"api.crossref.org/works/{DOI}": ok_json({"message": other})})
        records, rejected = await lookup(reg, reference(title=TITLE, DOI=DOI))
        assert records == []
        assert "different paper" in rejected[0]

    async def test_title_search_without_identifiers(self) -> None:
        searched: list[httpx.Request] = []

        def crossref_search(request: httpx.Request) -> httpx.Response:
            searched.append(request)
            return httpx.Response(200, text=json.dumps({"message": {"items": [CROSSREF_MESSAGE]}}))

        reg = registries({"api.crossref.org/works": crossref_search})
        records, _ = await lookup(reg, reference(title=TITLE, author=[{"family": "Chen"}]))
        assert [r.source for r in records] == [S.CROSSREF]
        assert searched[0].url.params["query.author"] == "Chen"

    async def test_rate_limit_is_transient(self) -> None:
        reg = registries({"api.crossref.org": 429})
        with pytest.raises(SourceUnavailable):
            await lookup(reg, reference(title=TITLE, DOI=DOI))


@pytest.fixture
def project_id(factory: sessionmaker[Session]) -> str:
    profile = ResearchProfile(problem="p", method="m", data="d", contribution="c")
    with transaction(factory) as s:
        return repo.create_project(s, Project(name="T", profile=profile)).id


def extracted_paper(factory: sessionmaker[Session], project_id: str, **header: Any) -> Paper:
    """A paper in MetadataReview, as the extraction step leaves it."""
    ref = reference(**header).model_copy(update={"project_id": project_id})
    with transaction(factory) as s:
        repo.add_reference(s, ref)
        paper = repo.add_paper(
            s,
            Paper(project_id=project_id, reference_id=ref.id, extracted_text_ref="tei.xml"),
        )
        repo.set_paper_state(s, paper.id, PaperState.EXTRACTING)
        return repo.set_paper_state(s, paper.id, PaperState.METADATA_REVIEW)


def job_for(paper: Paper) -> Job:
    return Job.create(RESOLVE_METADATA, {"paper_id": paper.id, "reason": "test"})


class TestResolveJob:
    async def test_clean_resolution(self, factory: sessionmaker[Session], project_id: str) -> None:
        # The PDF header had no date, so extraction produced a "nd" key.
        paper = extracted_paper(
            factory, project_id, title=TITLE, DOI=DOI, author=[{"family": "Chen"}]
        )
        handler = make_resolve_handler(factory, registries({**CROSSREF_BY_DOI, **OPENALEX_BY_DOI}))
        result = await handler(job_for(paper))

        assert result is not None
        assert result["status"] == "verified"
        with factory() as s:
            ref = repo.get_reference(s, paper.reference_id)
            after = repo.get_paper(s, paper.id)
        assert after.state is PaperState.METADATA_REVIEW
        assert ref.verification.status is VerificationStatus.VERIFIED
        assert ref.verification.has_correction  # Crossref "updated-by: correction"
        assert ref.csl["container-title"] == "Journal of Clinical Informatics"
        assert ref.csl["volume"] == "118"
        assert ref.citation_key == "chen2022attention"  # refreshed: uncited, now has a year
        assert {c.source for c in ref.field_provenance["title"]} == {
            S.PDF_HEADER, S.CROSSREF, S.OPENALEX,
        }  # fmt: skip

    async def test_conflict_needs_attention_until_user_decides(
        self, factory: sessionmaker[Session], project_id: str
    ) -> None:
        paper = extracted_paper(
            factory, project_id, title=TITLE, DOI=DOI, arxiv="2103.01234",
            issued={"date-parts": [[2021, 3, 4]]},
        )  # fmt: skip
        handler = make_resolve_handler(factory, registries({**CROSSREF_BY_DOI, **ARXIV}))
        await handler(job_for(paper))

        with factory() as s:
            flagged = repo.get_paper(s, paper.id)
            ref = repo.get_reference(s, paper.reference_id)
        assert flagged.state is PaperState.NEEDS_ATTENTION
        assert flagged.issues == [
            "issued disagrees: 2022 (crossref), 2021 (arxiv), 2021 (pdf_header)"
        ]
        assert ref.verification.status is VerificationStatus.MISMATCH
        assert ref.csl["issued"] == {"date-parts": [[2022, 1]]}  # precedence still applies

        with transaction(factory) as s:
            resolve_field(s, ref.id, "issued", source=S.ARXIV)
        with factory() as s:
            resolved = repo.get_paper(s, paper.id)
            ref = repo.get_reference(s, paper.reference_id)
        assert resolved.state is PaperState.METADATA_REVIEW
        assert resolved.issues == []
        assert ref.csl["issued"] == {"date-parts": [[2021, 3, 4]]}
        assert ref.field_provenance["issued"][0].source is S.USER

    async def test_cited_reference_keeps_its_key(
        self, factory: sessionmaker[Session], project_id: str
    ) -> None:
        paper = extracted_paper(factory, project_id, title=TITLE, DOI=DOI)
        content = {
            "type": "doc",
            "content": [{"type": "citation", "attrs": {"items": [{"ref": paper.reference_id}]}}],
        }
        with transaction(factory) as s:
            s.add(DocumentRow(id="doc_1", project_id=project_id, content=content))
        handler = make_resolve_handler(factory, registries(CROSSREF_BY_DOI))
        await handler(job_for(paper))
        with factory() as s:
            assert repo.get_reference(s, paper.reference_id).citation_key == "chenndattention"

    async def test_not_found_does_not_block(
        self, factory: sessionmaker[Session], project_id: str
    ) -> None:
        paper = extracted_paper(factory, project_id, title=TITLE, DOI=DOI)
        await make_resolve_handler(factory, registries({}))(job_for(paper))
        with factory() as s:
            assert repo.get_paper(s, paper.id).state is PaperState.METADATA_REVIEW
            ref = repo.get_reference(s, paper.reference_id)
        assert ref.verification.status is VerificationStatus.NOT_FOUND

    async def test_skips_papers_in_other_states(
        self, factory: sessionmaker[Session], project_id: str
    ) -> None:
        paper = extracted_paper(factory, project_id, title=TITLE)
        with transaction(factory) as s:
            repo.set_paper_state(s, paper.id, PaperState.ANALYZING)
        result = await make_resolve_handler(factory, registries({}))(job_for(paper))
        assert result == {"skipped": "paper is analyzing"}


def test_correcting_doi_queues_a_new_lookup(
    factory: sessionmaker[Session], project_id: str
) -> None:
    paper = extracted_paper(factory, project_id, title=TITLE, DOI="10.1/wrong")
    with transaction(factory) as s:
        resolve_field(s, paper.reference_id, "DOI", value=DOI)
        job = queue.claim(s, "w", types=[RESOLVE_METADATA])
    assert job is not None
    assert job.inputs == {"paper_id": paper.id, "reason": f"doi:{DOI}"}


@pytest.mark.parametrize(
    ("field", "kwargs", "message"),
    [
        ("abstract", {"value": "x"}, "cannot be edited"),
        ("title", {}, "exactly one"),
        ("volume", {"source": S.CROSSREF}, "no crossref value"),
    ],
)
def test_resolve_field_validation(
    factory: sessionmaker[Session], project_id: str, field: str, kwargs: Any, message: str
) -> None:
    from cibud.metadata.service import InvalidFieldChoice

    paper = extracted_paper(factory, project_id, title=TITLE)
    with transaction(factory) as s, pytest.raises(InvalidFieldChoice, match=message):
        resolve_field(s, paper.reference_id, field, **kwargs)


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()

    def session() -> Iterator[Session]:
        with factory() as s, s.begin():
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_store] = lambda: LocalObjectStore(tmp_path)
    yield TestClient(app)


async def test_api_conflict_flow(
    client: TestClient, factory: sessionmaker[Session], project_id: str
) -> None:
    paper = extracted_paper(
        factory, project_id, title=TITLE, DOI=DOI, arxiv="2103.01234",
        issued={"date-parts": [[2021]]},
    )  # fmt: skip
    await make_resolve_handler(factory, registries({**CROSSREF_BY_DOI, **ARXIV}))(job_for(paper))

    detail = client.get(f"/papers/{paper.id}").json()
    assert detail["paper"]["state"] == "needs_attention"
    [conflict] = detail["conflicts"]
    assert conflict["field"] == "issued"
    assert list(conflict["values"]) == ["crossref", "arxiv", "pdf_header"]

    ref_id = detail["reference"]["id"]
    resp = client.post(f"/references/{ref_id}/fields/issued", json={"source": "crossref"})
    assert resp.status_code == 200
    assert resp.json()["paper"]["state"] == "metadata_review"
    assert resp.json()["conflicts"] == []

    bad = client.post(f"/references/{ref_id}/fields/issued", json={})
    assert bad.status_code == 422

    job = client.post(f"/papers/{paper.id}/resolve-metadata")
    assert job.status_code == 202
    assert job.json()["type"] == RESOLVE_METADATA


def test_api_exclude_from_needs_attention(
    client: TestClient, factory: sessionmaker[Session], project_id: str
) -> None:
    paper = extracted_paper(factory, project_id, title=TITLE)
    with transaction(factory) as s:
        repo.flag_paper(s, paper.id, ["unfixable"])
    resp = client.post(f"/papers/{paper.id}/exclude")
    assert resp.status_code == 200
    assert resp.json()["paper"]["state"] == "excluded"
    # Excluding twice is not a valid transition.
    assert client.post(f"/papers/{paper.id}/exclude").status_code == 409
