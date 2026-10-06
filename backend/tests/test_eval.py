import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from cibud.eval.cli import main
from cibud.eval.corpus import check_corpus, load_corpus
from cibud.eval.prefill import parse_arxiv, prefill
from cibud.eval.schemas import Case, EvalPaper
from cibud.models.common import EvidenceLevel

TEMPLATES = Path(__file__).resolve().parents[2] / "eval" / "templates"


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    target = tmp_path / "demo"
    shutil.copytree(TEMPLATES, target)
    return target


def test_templates_are_valid(corpus_dir: Path) -> None:
    report = check_corpus(load_corpus(corpus_dir))
    assert report.ok, report.errors
    assert report.stats["papers"] == 2
    assert any("target is 20-30" in w for w in report.warnings)


def test_cli_validate(corpus_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(corpus_dir)]) == 0
    assert "papers: 2" in capsys.readouterr().out


def test_unknown_paper_key_is_an_error(corpus_dir: Path) -> None:
    path = corpus_dir / "relevance.yaml"
    labels = yaml.safe_load(path.read_text())
    labels[0]["paper"] = "typo_key"
    path.write_text(yaml.safe_dump(labels))
    report = check_corpus(load_corpus(corpus_dir))
    assert any("unknown paper 'typo_key'" in e for e in report.errors)


def test_missing_profile_raises(corpus_dir: Path) -> None:
    (corpus_dir / "profile.yaml").unlink()
    with pytest.raises(FileNotFoundError):
        load_corpus(corpus_dir)


def test_paper_needs_identifier() -> None:
    with pytest.raises(ValidationError):
        EvalPaper(key="x", access=EvidenceLevel.FULL_TEXT)


def test_valid_claim_case_must_expect_supported() -> None:
    with pytest.raises(ValidationError):
        Case.model_validate(
            {
                "id": "c",
                "kind": "claim",
                "category": "valid",
                "sentence": "s",
                "expected_verdict": "unsupported",
            }
        )


def test_category_must_match_kind() -> None:
    with pytest.raises(ValidationError):
        Case.model_validate(
            {
                "id": "c",
                "kind": "claim",
                "category": "wrong_year",
                "sentence": "s",
                "expected_verdict": "unsupported",
            }
        )


ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>Attention for
      Clinical Prediction</title>
    <published>2021-03-04T00:00:00Z</published>
    <author><name>Ada M. Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:doi>10.1234/example</arxiv:doi>
  </entry>
</feed>"""

CROSSREF_MESSAGE = {
    "title": ["A Published Paper"],
    "author": [{"family": "Hopper", "given": "Grace"}, {"family": "Consortium"}],
    "issued": {"date-parts": [[2020, 5]]},
    "container-title": ["Journal of Examples"],
    "DOI": "10.5555/abc",
}


def test_parse_arxiv() -> None:
    meta = parse_arxiv(ARXIV_ATOM)
    assert meta.title == "Attention for Clinical Prediction"
    assert meta.authors == ["Lovelace, Ada M.", "Turing, Alan"]
    assert meta.year == 2021
    assert meta.doi == "10.1234/example"


def test_prefill_uses_crossref_then_arxiv_and_never_marks_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json={"message": CROSSREF_MESSAGE})
        return httpx.Response(200, text=ARXIV_ATOM)

    papers = [
        EvalPaper(key="p_doi", doi="10.5555/abc", access=EvidenceLevel.ABSTRACT_ONLY),
        EvalPaper(key="p_arxiv", arxiv_id="2103.00001", access=EvidenceLevel.FULL_TEXT),
        EvalPaper(key="p_url", url="https://example.org", access=EvidenceLevel.METADATA_ONLY),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        filled, problems = prefill(papers, client)

    assert problems == []
    assert filled[0].metadata is not None
    assert filled[0].metadata.authors == ["Hopper, Grace", "Consortium"]
    assert filled[0].metadata.venue == "Journal of Examples"
    assert filled[1].metadata is not None
    assert filled[1].metadata.venue == "arXiv"
    assert filled[2].metadata is None
    assert not any(p.metadata_verified for p in filled)


def test_prefill_reports_http_errors() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    papers = [EvalPaper(key="p", doi="10.0/missing", access=EvidenceLevel.FULL_TEXT)]
    with httpx.Client(transport=transport) as client:
        filled, problems = prefill(papers, client)
    assert filled[0].metadata is None
    assert len(problems) == 1
