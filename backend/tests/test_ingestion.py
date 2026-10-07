from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from cibud.db import repo
from cibud.db.session import transaction
from cibud.ingestion.grobid import GrobidClient, GrobidUnavailable
from cibud.ingestion.service import (
    EXTRACT_PDF,
    InvalidUpload,
    apply_header,
    import_pdf,
    make_extract_handler,
)
from cibud.ingestion.tei import Author, HeaderMetadata
from cibud.jobs import queue
from cibud.models import (
    EvidenceLevel,
    FieldCandidate,
    MetadataSource,
    PaperState,
    Project,
    Reference,
    ResearchProfile,
)
from cibud.models.job import Job
from cibud.models.paper import PaperIssue
from cibud.storage import LocalObjectStore

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_TEI = (FIXTURES / "sample.tei.xml").read_text()
EMPTY_TEI = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body/></text></TEI>'
PDF = b"%PDF-1.7 fake pdf bytes for tests"
MB = 1024 * 1024


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def project_id(factory: sessionmaker[Session]) -> str:
    profile = ResearchProfile(problem="p", method="m", data="d", contribution="c")
    with transaction(factory) as s:
        return repo.create_project(s, Project(name="Test", profile=profile)).id


def grobid_returning(status: int, body: str = "") -> GrobidClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    return GrobidClient("http://grobid.test", client=httpx.AsyncClient(transport=transport))


def upload(factory: sessionmaker[Session], store: LocalObjectStore, project_id: str) -> Job:
    with transaction(factory) as s:
        _, job, _ = import_pdf(s, store, project_id, "sepsis.pdf", PDF, max_bytes=MB)
    return job


def claimed(factory: sessionmaker[Session]) -> Job:
    with transaction(factory) as s:
        job = queue.claim(s, "test-worker")
    assert job is not None
    return job


class TestImport:
    def test_creates_paper_reference_and_job(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        with transaction(factory) as s:
            paper, job, created = import_pdf(s, store, project_id, "sepsis.pdf", PDF, max_bytes=MB)
            reference = repo.get_reference(s, paper.reference_id)
        assert created
        assert paper.state is PaperState.IMPORTED
        assert store.get(paper.source_files[0]) == PDF
        assert reference.csl["title"] == "sepsis"
        assert reference.citation_key.startswith("pending_")
        assert (job.type, job.inputs) == (EXTRACT_PDF, {"paper_id": paper.id})

    def test_duplicate_upload_returns_existing_paper(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        with transaction(factory) as s:
            first, job1, _ = import_pdf(s, store, project_id, "a.pdf", PDF, max_bytes=MB)
            second, job2, created = import_pdf(s, store, project_id, "b.pdf", PDF, max_bytes=MB)
            assert len(repo.list_papers(s, project_id)) == 1
        assert not created
        assert (second.id, job2.id) == (first.id, job1.id)

    @pytest.mark.parametrize(
        ("data", "message"), [(b"hello", "not a PDF"), (b"%PDF" + b"x" * MB, "exceeds")]
    )
    def test_rejects_bad_uploads(
        self, session: Session, store: LocalObjectStore, project_id: str, data: bytes, message: str
    ) -> None:
        with pytest.raises(InvalidUpload, match=message):
            import_pdf(session, store, project_id, "x.pdf", data, max_bytes=MB)

    def test_unknown_project(self, session: Session, store: LocalObjectStore) -> None:
        with pytest.raises(repo.NotFound):
            import_pdf(session, store, "proj_nope", "x.pdf", PDF, max_bytes=MB)


class TestExtract:
    async def test_success(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        upload(factory, store, project_id)
        handler = make_extract_handler(factory, store, grobid_returning(200, SAMPLE_TEI))
        result = await handler(claimed(factory))

        assert result is not None
        assert result["state"] == "metadata_review"
        with factory() as s:
            [paper] = repo.list_papers(s, project_id)
            reference = repo.get_reference(s, paper.reference_id)
            passages = repo.list_passages(s, paper.id)

        assert paper.state is PaperState.METADATA_REVIEW
        assert paper.evidence_level is EvidenceLevel.FULL_TEXT
        assert paper.extracted_text_ref is not None
        assert store.exists(paper.extracted_text_ref)

        assert reference.citation_key == "chen2021attention"
        assert reference.csl["DOI"] == "10.1000/jci.2021.42"
        assert reference.csl["issued"] == {"date-parts": [[2021, 3, 4]]}
        assert reference.field_provenance["title"][0].source is MetadataSource.PDF_HEADER

        assert result["passages"] == len(passages) > 0
        assert [p.ordinal for p in passages] == list(range(len(passages)))
        text = store.get(paper.extracted_text_ref.replace("grobid.tei.xml", "text.txt")).decode()
        for p in passages:
            assert text[p.char_span[0] : p.char_span[1]] == p.text

    async def test_rerun_on_flagged_paper_replaces_passages(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        upload(factory, store, project_id)
        handler = make_extract_handler(factory, store, grobid_returning(200, SAMPLE_TEI))
        job = claimed(factory)
        await handler(job)
        with transaction(factory) as s:
            [paper] = repo.list_papers(s, project_id)
            count = len(repo.list_passages(s, paper.id))
            repo.update_issues(
                s, paper.id, {"extraction"}, [PaperIssue(kind="extraction", message="re-run")]
            )

        await handler(job)
        with factory() as s:
            assert len(repo.list_passages(s, paper.id)) == count
            assert repo.get_reference(s, paper.reference_id).citation_key == "chen2021attention"

    async def test_skips_papers_past_extraction(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        upload(factory, store, project_id)
        handler = make_extract_handler(factory, store, grobid_returning(200, SAMPLE_TEI))
        job = claimed(factory)
        await handler(job)
        assert await handler(job) == {"skipped": "paper is metadata_review"}

    @pytest.mark.parametrize(
        ("status", "body", "issue"),
        [
            (500, "boom", "PDF extraction failed"),
            (204, "", "no content"),
            (200, "<not-xml", "could not parse"),
            (200, EMPTY_TEI, "no extractable body text"),
        ],
    )
    async def test_permanent_failures_flag_the_paper(
        self,
        factory: sessionmaker[Session],
        store: LocalObjectStore,
        project_id: str,
        status: int,
        body: str,
        issue: str,
    ) -> None:
        upload(factory, store, project_id)
        handler = make_extract_handler(factory, store, grobid_returning(status, body))
        result = await handler(claimed(factory))
        assert result is not None
        assert issue in result["issue"]
        with factory() as s:
            [paper] = repo.list_papers(s, project_id)
        assert paper.state is PaperState.NEEDS_ATTENTION
        assert paper.issues[0].kind == "extraction"
        assert issue in paper.issues[0].message

    async def test_busy_grobid_is_retried_then_flagged(
        self, factory: sessionmaker[Session], store: LocalObjectStore, project_id: str
    ) -> None:
        upload(factory, store, project_id)
        handler = make_extract_handler(factory, store, grobid_returning(503))
        job = claimed(factory)

        with pytest.raises(GrobidUnavailable):
            await handler(job)
        with factory() as s:
            assert repo.list_papers(s, project_id)[0].state is PaperState.EXTRACTING

        final = job.model_copy(update={"attempts": job.max_attempts})
        with pytest.raises(GrobidUnavailable):
            await handler(final)
        with factory() as s:
            paper = repo.list_papers(s, project_id)[0]
        assert paper.state is PaperState.NEEDS_ATTENTION
        assert "GROBID unavailable" in paper.issues[0].message


class TestApplyHeader:
    header = HeaderMetadata(
        title="Header Title", authors=[Author(family="Ito", given="Kai")], date="2020"
    )

    def reference(self, **kwargs: object) -> Reference:
        defaults: dict[str, object] = {
            "project_id": "proj_1",
            "citation_key": "pending_paper_1",
            "csl": {"title": "filename"},
        }
        return Reference.model_validate({**defaults, **kwargs})

    def test_assigns_key_once(self) -> None:
        updated = apply_header(self.reference(), self.header, {"ito2020header"})
        assert updated.citation_key == "ito2020headera"
        again = apply_header(updated, self.header, {"ito2020header", "ito2020headera"})
        assert again.citation_key == "ito2020headera"

    def test_keeps_values_from_better_sources(self) -> None:
        crossref = FieldCandidate(value="Crossref Title", source=MetadataSource.CROSSREF)
        ref = self.reference(
            csl={"title": "Crossref Title"}, field_provenance={"title": [crossref]}
        )
        updated = apply_header(ref, self.header, set())
        assert updated.csl["title"] == "Crossref Title"
        sources = [c.source for c in updated.field_provenance["title"]]
        assert sources == [MetadataSource.CROSSREF, MetadataSource.PDF_HEADER]

    def test_reapplying_does_not_duplicate_provenance(self) -> None:
        once = apply_header(self.reference(), self.header, set())
        twice = apply_header(once, self.header, set())
        assert len(twice.field_provenance["title"]) == 1

    def test_missing_title_keeps_filename(self) -> None:
        updated = apply_header(self.reference(), HeaderMetadata(), set())
        assert updated.csl["title"] == "filename"
        assert updated.citation_key == "anonnd"


async def test_grobid_request_options() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text=SAMPLE_TEI)

    client = GrobidClient(
        "http://grobid.test/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await client.process_fulltext(PDF) == SAMPLE_TEI

    request = sent[0]
    body = request.read().decode(errors="replace")
    assert request.url == "http://grobid.test/api/processFulltextDocument"
    assert body.count('name="teiCoordinates"') == 4
    for option in ('name="segmentSentences"', 'name="consolidateHeader"', 'filename="paper.pdf"'):
        assert option in body
