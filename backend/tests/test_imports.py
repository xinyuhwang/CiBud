import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from test_metadata import ARXIV_ATOM, CROSSREF_MESSAGE, OPENALEX_WORK

from cibud.api.deps import get_session, get_store
from cibud.api.main import create_app
from cibud.db import repo
from cibud.db.session import transaction
from cibud.imports.parsing import (
    Identifier,
    parse_bibliography,
    parse_bibtex,
    parse_identifier,
    parse_ris,
)
from cibud.imports.service import (
    ENRICH_IMPORT,
    PdfFetcher,
    import_bibliography,
    import_identifiers,
    make_enrich_handler,
)
from cibud.ingestion.service import EXTRACT_PDF
from cibud.jobs import queue
from cibud.metadata.sources import Registries
from cibud.models import EvidenceLevel, MetadataSource, PaperState, Project, ResearchProfile
from cibud.models.job import Job
from cibud.storage import LocalObjectStore

DOI = "10.1000/jci.2022.42"
PDF = b"%PDF-1.7 open access paper"

BIBTEX = r"""
@article{chen2022attn,
  title   = {Attention-Based Sepsis Prediction from {Electronic Health Records}},
  author  = {Ch{\'e}n, Lin and Ito, Kai and van der Berg, Anna},
  journal = {Journal of Clinical Informatics},
  year    = {2022},
  month   = jan,
  volume  = {118},
  pages   = {103790--103795},
  doi     = {10.1000/JCI.2022.42},
}
@misc{rao2021graph,
  title         = {Graph Attention over Diagnosis Codes},
  author        = {Rao, Mira},
  year          = {2021},
  eprint        = {2103.01234},
  archivePrefix = {arXiv},
}
@inproceedings{nokey,
  author = {Nobody, A.},
}
"""

RIS = """TY  - JOUR
TI  - Calibration of clinical machine learning models
AU  - Gomez, Ana
AU  - Lee, Min
PY  - 2023
JO  - Clinical AI
VL  - 4
SP  - 12
EP  - 20
DO  - 10.1000/cai.2023.7
AB  - We review calibration.
ER  -
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.1371/journal.pmed.1002686", Identifier("doi", "10.1371/journal.pmed.1002686")),
        ("https://doi.org/10.1000/ABC", Identifier("doi", "10.1000/abc")),
        ("doi:10.1000/abc.", Identifier("doi", "10.1000/abc")),
        ("https://www.nature.com/articles/10.1038/s41591-020-1034-x.pdf",
         Identifier("doi", "10.1038/s41591-020-1034-x")),
        ("https://arxiv.org/abs/2607.25164v1", Identifier("arxiv", "2607.25164")),
        ("https://arxiv.org/pdf/2607.25164", Identifier("arxiv", "2607.25164")),
        ("arXiv:hep-th/9901001", Identifier("arxiv", "hep-th/9901001")),
        ("2103.01234", Identifier("arxiv", "2103.01234")),
        ("10.48550/arXiv.2103.01234", Identifier("arxiv", "2103.01234")),
        ("https://example.com/some-paper", None),
        ("", None),
    ],
)  # fmt: skip
def test_parse_identifier(text: str, expected: Identifier | None) -> None:
    assert parse_identifier(text) == expected


class TestBibtex:
    def test_entries(self) -> None:
        parsed = parse_bibtex(BIBTEX)
        assert parsed.source is MetadataSource.BIBTEX
        assert [e.key for e in parsed.entries] == ["chen2022attn", "rao2021graph", "nokey"]
        chen = parsed.entries[0].csl
        assert chen["title"] == "Attention-Based Sepsis Prediction from Electronic Health Records"
        assert chen["author"] == [
            {"family": "Chén", "given": "Lin"},
            {"family": "Ito", "given": "Kai"},
            {"family": "van der Berg", "given": "Anna"},
        ]
        assert chen["issued"] == {"date-parts": [[2022, 1]]}
        assert chen["type"] == "article-journal"
        assert chen["DOI"] == "10.1000/JCI.2022.42"  # normalized later, on resolution
        assert chen["page"] == "103790\u2013103795"

    def test_arxiv_eprint(self) -> None:
        rao = parse_bibtex(BIBTEX).entries[1].csl
        assert rao["arxiv"] == "2103.01234"
        assert rao["type"] == "article"

    def test_broken_entry_is_reported(self) -> None:
        parsed = parse_bibtex("@article{ok, title={T}}\n@article{broken, title={unclosed\n")
        assert [e.key for e in parsed.entries] == ["ok"]
        assert parsed.errors


def test_ris() -> None:
    parsed = parse_ris(RIS)
    [entry] = parsed.entries
    assert entry.csl == {
        "type": "article-journal",
        "title": "Calibration of clinical machine learning models",
        "author": [{"family": "Gomez", "given": "Ana"}, {"family": "Lee", "given": "Min"}],
        "issued": {"date-parts": [[2023]]},
        "container-title": "Clinical AI",
        "volume": "4",
        "page": "12-20",
        "DOI": "10.1000/cai.2023.7",
        "abstract": "We review calibration.",
    }


def test_format_detection() -> None:
    assert parse_bibliography("refs.ris", RIS).source is MetadataSource.RIS
    assert parse_bibliography("export.txt", RIS).source is MetadataSource.RIS
    assert parse_bibliography("refs.bib", BIBTEX).source is MetadataSource.BIBTEX


# --- service ----------------------------------------------------------------------------


@pytest.fixture
def project_id(factory: sessionmaker[Session]) -> str:
    profile = ResearchProfile(problem="p", method="m", data="d", contribution="c")
    with transaction(factory) as s:
        return repo.create_project(s, Project(name="T", profile=profile)).id


def test_import_identifiers(session: Session, project_id: str) -> None:
    results = import_identifiers(
        session, project_id, [DOI, f"https://doi.org/{DOI.upper()}", "https://example.com", "  "]
    )
    assert [r.status for r in results] == ["created", "existing", "invalid"]
    assert results[1].paper_id == results[0].paper_id
    paper = repo.get_paper(session, results[0].paper_id or "")
    assert paper.state is PaperState.IMPORTED
    assert paper.evidence_level is EvidenceLevel.METADATA_ONLY
    job = queue.get(session, results[0].job_id or "")
    assert job is not None and job.type == ENRICH_IMPORT


def test_import_bibliography_keeps_keys(session: Session, project_id: str) -> None:
    results = import_bibliography(session, project_id, parse_bibtex(BIBTEX))
    assert [r.status for r in results] == ["created", "created", "invalid"]
    ref = repo.get_reference(
        session, repo.get_paper(session, results[0].paper_id or "").reference_id
    )
    assert (ref.citation_key, ref.citation_key_locked) == ("chen2022attn", True)
    assert ref.field_provenance["title"][0].source is MetadataSource.BIBTEX

    again = import_bibliography(session, project_id, parse_bibtex(BIBTEX))
    assert [r.status for r in again][:2] == ["existing", "existing"]


def test_colliding_bibtex_key_is_not_reused(session: Session, project_id: str) -> None:
    import_bibliography(session, project_id, parse_bibtex("@article{k, title={One}}"))
    [result] = import_bibliography(session, project_id, parse_bibtex("@article{k, title={Two}}"))
    assert result.status == "created"
    assert "already used" in (result.message or "")
    ref = repo.get_reference(session, repo.get_paper(session, result.paper_id or "").reference_id)
    assert not ref.citation_key_locked


def registries(routes: dict[str, Any]) -> Registries:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        for prefix, body in routes.items():
            if key.startswith(prefix):
                if isinstance(body, str):
                    return httpx.Response(200, text=body)
                return httpx.Response(200, text=json.dumps(body))
        return httpx.Response(404)

    return Registries(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def fetcher(responses: dict[str, httpx.Response], max_bytes: int = 1024) -> PdfFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        return responses.get(str(request.url), httpx.Response(404))

    return PdfFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_bytes)


OA_WORK = {
    **OPENALEX_WORK,
    "best_oa_location": {"is_oa": True, "pdf_url": "https://oa.test/paper.pdf"},
    "abstract_inverted_index": {"We": [0], "predict": [1], "sepsis.": [2]},
}


async def enrich(
    factory: sessionmaker[Session],
    store: LocalObjectStore,
    project_id: str,
    routes: dict[str, Any],
    pdfs: dict[str, httpx.Response] | None = None,
    identifier: str = DOI,
) -> tuple[str, dict[str, Any] | None]:
    with transaction(factory) as s:
        [result] = import_identifiers(s, project_id, [identifier])
    handler = make_enrich_handler(factory, registries(routes), store, fetcher(pdfs or {}))
    job = Job.create(ENRICH_IMPORT, {"paper_id": result.paper_id})
    return result.paper_id or "", await handler(job)


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path)


class TestEnrich:
    async def test_open_access_pdf_is_downloaded_and_queued_for_extraction(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        paper_id, result = await enrich(
            factory, store, project_id,
            {f"api.crossref.org/works/{DOI}": {"message": CROSSREF_MESSAGE},
             f"api.openalex.org/works/doi:{DOI}": OA_WORK},
            {"https://oa.test/paper.pdf": httpx.Response(200, content=PDF)},
        )  # fmt: skip
        assert result is not None and result["full_text"] == "https://oa.test/paper.pdf"
        with transaction(factory) as s:
            paper = repo.get_paper(s, paper_id)
            ref = repo.get_reference(s, paper.reference_id)
            job = queue.claim(s, "w", types=[EXTRACT_PDF])
        assert paper.state is PaperState.METADATA_ONLY  # until extraction runs
        assert store.get(paper.source_files[-1]) == PDF
        assert job is not None and job.inputs["paper_id"] == paper_id
        assert ref.citation_key == "chen2022attention"  # placeholder replaced
        assert ref.csl["title"].startswith("Attention-Based Sepsis")

    async def test_abstract_only_without_accessible_pdf(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        closed = {**OA_WORK, "best_oa_location": {"is_oa": False, "pdf_url": "https://x/p.pdf"}}
        paper_id, result = await enrich(
            factory, store, project_id, {f"api.openalex.org/works/doi:{DOI}": closed}
        )
        assert result == {"evidence_level": "abstract_only", "sources": ["openalex"]}
        with factory() as s:
            paper = repo.get_paper(s, paper_id)
            [passage] = repo.list_passages(s, paper_id)
        assert paper.evidence_level is EvidenceLevel.ABSTRACT_ONLY
        assert paper.state is PaperState.METADATA_ONLY
        assert (passage.text, passage.section) == ("We predict sepsis.", "Abstract")

    async def test_metadata_only_when_nothing_else(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        _, result = await enrich(
            factory,
            store,
            project_id,
            {f"api.crossref.org/works/{DOI}": {"message": CROSSREF_MESSAGE}},
        )
        assert result is not None and result["evidence_level"] == "metadata_only"

    async def test_failed_download_falls_back_to_abstract(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        _, result = await enrich(
            factory, store, project_id,
            {f"api.openalex.org/works/doi:{DOI}": OA_WORK},
            {"https://oa.test/paper.pdf": httpx.Response(200, content=b"<html>login</html>")},
        )  # fmt: skip
        assert result is not None and result["evidence_level"] == "abstract_only"

    async def test_arxiv_import_downloads_from_arxiv(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        _, result = await enrich(
            factory, store, project_id,
            {"export.arxiv.org/api/query": ARXIV_ATOM},
            {"https://arxiv.org/pdf/2103.01234": httpx.Response(200, content=PDF)},
            identifier="arXiv:2103.01234",
        )  # fmt: skip
        assert result is not None and result["full_text"] == "https://arxiv.org/pdf/2103.01234"

    async def test_unknown_identifier_needs_attention(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        paper_id, _ = await enrich(factory, store, project_id, {})
        with factory() as s:
            paper = repo.get_paper(s, paper_id)
        assert paper.state is PaperState.NEEDS_ATTENTION
        assert paper.issues[0].message == "no metadata found for this identifier"

    async def test_bibtex_key_survives_resolution(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        with transaction(factory) as s:
            [result] = import_bibliography(
                s, project_id, parse_bibtex("@article{mykey, title={T}, doi={" + DOI + "}}")
            )
        handler = make_enrich_handler(
            factory,
            registries({f"api.crossref.org/works/{DOI}": {"message": CROSSREF_MESSAGE}}),
            store,
            fetcher({}),
        )
        await handler(Job.create(ENRICH_IMPORT, {"paper_id": result.paper_id}))
        with factory() as s:
            paper = repo.get_paper(s, result.paper_id or "")
            ref = repo.get_reference(s, paper.reference_id)
        assert ref.citation_key == "mykey"
        # The .bib title ("T") has nothing in common with the DOI's Crossref record, so the
        # record is rejected as a different paper rather than merged (§7.1: detect errors).
        assert paper.state is PaperState.NEEDS_ATTENTION
        assert "is a different paper" in paper.issues[0].message
        assert ref.csl["title"] == "T"


async def test_pdf_fetcher_limits() -> None:
    big = fetcher({"https://x/big.pdf": httpx.Response(200, content=b"%PDF" + b"0" * 2000)})
    assert await big.fetch("https://x/big.pdf") is None
    assert await fetcher({}).fetch("https://x/missing.pdf") is None


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()

    def session() -> Iterator[Session]:
        with factory() as s, s.begin():
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_store] = lambda: LocalObjectStore(tmp_path)
    yield TestClient(app)


def test_api(client: TestClient, project_id: str) -> None:
    resp = client.post(
        f"/projects/{project_id}/imports/identifiers", json={"identifiers": [DOI, "nope"]}
    )
    assert resp.status_code == 202
    assert [r["status"] for r in resp.json()] == ["created", "invalid"]

    resp = client.post(
        f"/projects/{project_id}/imports/bibliography",
        files={"file": ("refs.ris", RIS.encode(), "application/x-research-info-systems")},
    )
    assert [r["status"] for r in resp.json()] == ["created"]
    assert len(client.get(f"/projects/{project_id}/papers").json()) == 2

    assert (
        client.post(
            "/projects/proj_nope/imports/identifiers", json={"identifiers": [DOI]}
        ).status_code
        == 404
    )
