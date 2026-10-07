"""Optional end-to-end check against a running GROBID and a local example PDF.

Skipped unless GROBID is reachable and a PDF exists in the repo root (git-ignored) or at
CIBUD_SAMPLE_PDF. Never runs in CI.
"""

import os
from pathlib import Path

import pytest

from cibud.ingestion.chunking import chunk
from cibud.ingestion.grobid import GrobidClient
from cibud.ingestion.tei import parse_tei
from cibud.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def sample_pdf() -> Path | None:
    if env := os.environ.get("CIBUD_SAMPLE_PDF"):
        return Path(env)
    return next(iter(sorted(REPO_ROOT.glob("*.pdf"))), None)


async def test_real_pdf_end_to_end() -> None:
    pdf = sample_pdf()
    if pdf is None or not pdf.exists():
        pytest.skip("no example PDF")
    grobid = GrobidClient(get_settings().grobid_url)
    try:
        if not await grobid.is_alive():
            pytest.skip("GROBID not running")
        tei = await grobid.process_fulltext(pdf.read_bytes(), pdf.name)
    finally:
        await grobid.aclose()

    parsed = parse_tei(tei)
    assert parsed.header.title
    assert parsed.header.authors
    assert parsed.sentence_count("body") > 20
    chunked = chunk(parsed)
    with_boxes = [p for p in chunked.passages if p.bboxes]
    assert len(with_boxes) / len(chunked.passages) > 0.9
    for p in chunked.passages:
        assert chunked.text[p.char_span[0] : p.char_span[1]] == p.text
