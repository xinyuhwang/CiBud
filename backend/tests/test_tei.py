from pathlib import Path

import pytest

from cibud.ingestion.chunking import chunk
from cibud.ingestion.citation_keys import base_key, unique_key
from cibud.ingestion.tei import (
    HeaderMetadata,
    Paragraph,
    ParsedPaper,
    Section,
    Sentence,
    normalize_arxiv_id,
    parse_coords,
    parse_tei,
)
from cibud.models.evidence import BoundingBox

SAMPLE = (Path(__file__).parent / "fixtures" / "sample.tei.xml").read_bytes()


@pytest.fixture(scope="module")
def parsed() -> ParsedPaper:
    return parse_tei(SAMPLE)


class TestHeader:
    def test_fields(self, parsed: ParsedPaper) -> None:
        h = parsed.header
        assert h.title == "Attention-Based Sepsis Prediction from Electronic Health Records"
        assert h.date == "2021-03-04"
        assert h.year == 2021
        assert h.doi == "10.1000/jci.2021.42"
        assert h.venue == "Journal of Clinical Informatics"
        assert h.abstract == "We predict sepsis onset from EHR data. Our model improves AUROC."

    def test_authors_join_forenames_and_skip_unnamed(self, parsed: ParsedPaper) -> None:
        assert [(a.given, a.family) for a in parsed.header.authors] == [
            ("Lin M", "Chén"),
            ("Kai", "Ito"),
        ]

    def test_arxiv_id_is_normalized(self, parsed: ParsedPaper) -> None:
        assert parsed.header.arxiv_id == "2103.01234"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("arXiv:2607.25164v1[cs.CV]", "2607.25164"),
        ("2103.01234", "2103.01234"),
        ("arXiv:hep-th/9901001v3", "hep-th/9901001"),
        ("not an id", None),
    ],
)
def test_normalize_arxiv_id(raw: str, expected: str | None) -> None:
    assert normalize_arxiv_id(raw) == expected


def test_parse_coords_skips_malformed_parts() -> None:
    boxes = parse_coords("1,1.5,2,3,4;bad;2,0,0,1,1")
    assert [b.page for b in boxes] == [1, 2]
    assert boxes[0].x == 1.5
    assert parse_coords(None) == []


class TestSections:
    def test_kinds_and_order(self, parsed: ParsedPaper) -> None:
        assert [(s.kind, s.label) for s in parsed.sections] == [
            ("abstract", "Abstract"),
            ("body", "1 Introduction"),
            ("body", "4.2 Comparison with baselines"),
            ("body", "4.2 Comparison with baselines"),  # head-less div continues the section
            ("body", "Discussion"),
            ("caption", "Figure 1"),
            ("caption", "Table 3"),
        ]

    def test_counts(self, parsed: ParsedPaper) -> None:
        assert parsed.sentence_count("abstract") == 2
        assert parsed.sentence_count("body") == 5
        assert parsed.sentence_count("caption") == 2

    def test_inline_refs_kept_in_text(self, parsed: ParsedPaper) -> None:
        first = parsed.sections[1].paragraphs[0].sentences[0]
        assert first.text == "Sepsis is a leading cause of death [1]."

    def test_sentence_spanning_pages(self, parsed: ParsedPaper) -> None:
        second = parsed.sections[1].paragraphs[0].sentences[1]
        assert [b.page for b in second.boxes] == [1, 2]
        assert second.page == 1

    def test_unsegmented_paragraph_uses_paragraph_coords(self, parsed: ParsedPaper) -> None:
        discussion = parsed.sections[4]
        assert len(discussion.paragraphs) == 1  # the blank paragraph is dropped
        assert discussion.paragraphs[0].sentences[0].page == 8

    def test_caption_coords(self, parsed: ParsedPaper) -> None:
        figure = parsed.sections[5].paragraphs[0].sentences[0]
        assert figure.text == "Model architecture overview."
        assert figure.page == 5


def test_bibliography(parsed: ParsedPaper) -> None:
    b0, b1 = parsed.bibliography
    assert (b0.id, b0.title, b0.year, b0.doi, b0.venue) == (
        "b0",
        "Sepsis epidemiology",
        2019,
        "10.1000/cc.2019.1",
        "Critical Care",
    )
    assert b0.authors[0].family == "Rao"
    assert (b1.title, b1.arxiv_id, b1.authors[0].family) == (
        "A Book on Machine Learning",
        "1901.00001",
        "Kim",
    )


def test_minimal_tei_without_header_or_body() -> None:
    parsed = parse_tei('<TEI xmlns="http://www.tei-c.org/ns/1.0"/>')
    assert parsed.header == HeaderMetadata()
    assert parsed.sections == []


class TestChunking:
    def test_spans_index_into_canonical_text(self, parsed: ParsedPaper) -> None:
        chunked = chunk(parsed)
        assert chunked.passages
        for p in chunked.passages:
            start, end = p.char_span
            assert chunked.text[start:end] == p.text

    def test_passage_metadata(self, parsed: ParsedPaper) -> None:
        passages = chunk(parsed).passages
        results = next(p for p in passages if "0.84" in p.text)
        assert results.section == "4.2 Comparison with baselines"
        assert results.page == 6
        assert results.bboxes[0] == BoundingBox(page=6, x=50, y=300, width=200, height=10)

    def test_headings_are_in_text_but_not_passages(self, parsed: ParsedPaper) -> None:
        chunked = chunk(parsed)
        assert "1 Introduction" in chunked.text
        assert all(p.text != "1 Introduction" for p in chunked.passages)

    def test_long_paragraph_splits_at_sentence_boundaries(self) -> None:
        sentences = [Sentence(text=f"Sentence number {i} is here.") for i in range(10)]
        paper = ParsedPaper(
            header=HeaderMetadata(),
            sections=[Section(heading="Results", paragraphs=[Paragraph(sentences=sentences)])],
        )
        chunked = chunk(paper, max_chars=60)
        assert len(chunked.passages) == 5
        assert all(p.text.endswith("here.") for p in chunked.passages)
        assert " ".join(p.text for p in chunked.passages) == " ".join(s.text for s in sentences)
        for p in chunked.passages:
            assert chunked.text[p.char_span[0] : p.char_span[1]] == p.text

    def test_oversized_sentence_is_kept_whole(self) -> None:
        long = Sentence(text="x" * 100)
        paper = ParsedPaper(
            header=HeaderMetadata(),
            sections=[Section(heading=None, paragraphs=[Paragraph(sentences=[long])])],
        )
        assert [p.text for p in chunk(paper, max_chars=10).passages] == ["x" * 100]

    def test_deterministic(self) -> None:
        assert chunk(parse_tei(SAMPLE)) == chunk(parse_tei(SAMPLE))


class TestCitationKeys:
    def test_base_key(self) -> None:
        assert base_key("Chén", 2021, "Attention-Based Sepsis Prediction") == "chen2021attention"
        assert base_key("Al-Kindi", 2026, "The OrganLens: Organ-specific") == "alkindi2026organlens"
        assert base_key(None, None, None) == "anonnd"

    def test_unique_key(self) -> None:
        assert unique_key("chen2021", set()) == "chen2021"
        assert unique_key("chen2021", {"chen2021"}) == "chen2021a"
        assert unique_key("chen2021", {"chen2021", "chen2021a"}) == "chen2021b"
