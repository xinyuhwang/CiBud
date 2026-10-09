from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cibud.api.deps import get_session, get_store
from cibud.api.main import create_app
from cibud.db import repo
from cibud.db.session import transaction
from cibud.documents.parse import parse_locator, parse_text
from cibud.models import (
    EvidenceLevel,
    Paper,
    PaperState,
    Project,
    Reference,
    ReferenceVerification,
    ResearchProfile,
)
from cibud.models.common import utcnow
from cibud.models.document import block_text, citation_nodes_with_paths
from cibud.models.paper import PaperIssue
from cibud.models.reference import VerificationStatus
from cibud.storage import LocalObjectStore
from cibud.validation.references import Severity, check_references

KEYS = {"chen2021": "ref_chen", "ito2022": "ref_ito", "raj2018": "ref_raj"}


class TestParse:
    def test_pandoc(self) -> None:
        report = parse_text(
            "Gains [@chen2021, p. 6; @ito2022]. As @raj2018 showed [-@raj2018].", KEYS
        )
        nodes = citation_nodes_with_paths(report.content)
        assert report.citations == 3
        assert [(p, [i.ref for i in a.items], a.mode.value) for p, a in nodes] == [
            ("0.1", ["ref_chen", "ref_ito"], "parenthetical"),
            ("0.3", ["ref_raj"], "narrative"),
            ("0.5", ["ref_raj"], "suppress_author"),
        ]
        first = nodes[0][1].items[0]
        assert (first.locator, first.label) == ("6", "page")

    def test_latex(self) -> None:
        text = r"Rare \citep[p.~4]{chen2021,ito2022}. \citet{raj2018} disagree \citeyear{raj2018}."
        nodes = citation_nodes_with_paths(parse_text(text, KEYS).content)
        assert [a.mode.value for _, a in nodes] == ["parenthetical", "narrative", "suppress_author"]
        items = nodes[0][1].items
        assert (items[0].locator, items[1].locator, items[1].label) == (None, "4", "page")

    def test_latex_prenote_and_postnote(self) -> None:
        nodes = citation_nodes_with_paths(
            parse_text(r"\citep[see][sec.~3]{chen2021}", KEYS).content
        )
        item = nodes[0][1].items[0]
        assert (item.locator, item.label) == ("3", "section")

    def test_unknown_keys_are_kept(self) -> None:
        report = parse_text("Claim [@nobody2020].", KEYS)
        assert report.unresolved_keys == ["nobody2020"]
        [(_, attrs)] = citation_nodes_with_paths(report.content)
        assert (attrs.items[0].ref, attrs.items[0].key) == (None, "nobody2020")

    def test_emails_and_brackets_are_not_citations(self) -> None:
        report = parse_text("Write to a@b.com [see Table 2].", KEYS)
        assert report.citations == 0
        assert block_text(report.content["content"][0]) == "Write to a@b.com [see Table 2]."

    def test_blocks_and_headings(self) -> None:
        text = (
            "## Related Work\n\nFirst para\ncontinues.\n\n"
            "\\section{Calibration}\nSecond [@chen2021]."
        )
        blocks = parse_text(text, KEYS).content["content"]
        assert [b["type"] for b in blocks] == ["heading", "paragraph", "heading", "paragraph"]
        assert block_text(blocks[1]) == "First para continues."
        assert blocks[2]["attrs"] == {"level": 1}
        assert block_text(blocks[3]) == "Second [ref_chen]."

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("p. 4", ("4", "page")),
            ("pp. 4-5", ("4-5", "page")),
            ("12", ("12", "page")),
            ("sec. 3.2", ("3.2", "section")),
            ("Fig. 2", ("2", "figure")),
            ("emphasis added", ("emphasis added", None)),
            ("", (None, None)),
        ],
    )
    def test_locator(self, text: str, expected: tuple[str | None, str | None]) -> None:
        assert parse_locator(text) == expected


# --- validator (pure) -------------------------------------------------------------------

TITLE = "Attention-Based Sepsis Prediction from Electronic Health Records"


def ref(
    id: str, key: str, status: VerificationStatus = VerificationStatus.VERIFIED, **kw: Any
) -> Reference:
    verification = ReferenceVerification(status=status, checked_at=utcnow(), **kw.pop("v", {}))
    return Reference(
        id=id, project_id="p", citation_key=key, csl=kw.pop("csl", {"title": key}),
        verification=verification, **kw,
    )  # fmt: skip


def paper(ref_id: str, state: PaperState = PaperState.METADATA_REVIEW, **kw: Any) -> Paper:
    return Paper(
        project_id="p", reference_id=ref_id, state=state,
        evidence_level=kw.pop("level", EvidenceLevel.FULL_TEXT), **kw,
    )  # fmt: skip


def check(
    text: str, refs: list[Reference], papers: list[Paper] | None = None
) -> list[tuple[str, str, list[str]]]:
    keys = {r.citation_key: r.id for r in refs}
    content = parse_text(text, keys).content
    report = check_references(content, refs, {p.reference_id: p for p in papers or []})
    return [
        (i.code.value, i.citation_key or i.reference_id or "", i.node_paths) for i in report.issues
    ]


def test_clean_document_has_no_issues() -> None:
    refs = [ref("ref_a", "a"), ref("ref_b", "b")]
    report = check_references(
        parse_text("X [@a]. Y [@b].", {"a": "ref_a", "b": "ref_b"}).content,
        refs,
        {"ref_a": paper("ref_a"), "ref_b": paper("ref_b")},
    )
    assert report.issues == []
    assert report.ok
    assert (report.citation_nodes, report.cited_references) == (2, 2)


def test_missing_and_removed_references() -> None:
    refs = [ref("ref_a", "a")]
    content = parse_text("X [@a; @ghost]. Y [@ghost].", {"a": "ref_a"}).content
    # Simulate a reference that was deleted after being cited.
    content["content"][0]["content"].append(
        {"type": "citation", "attrs": {"items": [{"ref": "ref_deleted"}]}}
    )
    report = check_references(content, refs, {"ref_a": paper("ref_a")})
    missing = [(i.citation_key, i.reference_id, i.node_paths) for i in report.issues]
    assert missing == [("ghost", None, ["0.1", "0.3"]), (None, "ref_deleted", ["0.5"])]
    assert all(i.severity is Severity.ERROR for i in report.issues)
    assert not report.ok


def test_retracted_and_corrected() -> None:
    retracted = ref("ref_r", "r", v={"retracted": True, "issues": ["retraction 10.1/x"]})
    corrected = ref("ref_c", "c", v={"has_correction": True})
    issues = check("A [@r]. B [@c].", [retracted, corrected], [paper("ref_r"), paper("ref_c")])
    assert issues == [("retracted", "r", ["0.1"]), ("corrected", "c", ["0.3"])]


def test_verification_statuses() -> None:
    refs = [
        ref("ref_n", "n", VerificationStatus.NOT_FOUND),
        ref(
            "ref_m",
            "m",
            VerificationStatus.MISMATCH,
            v={"rejected_records": ["DOI is a different paper"]},
        ),
        ref("ref_u", "u", VerificationStatus.UNVERIFIED),
    ]
    issues = check("[@n] [@m] [@u]", refs, [paper(r.id) for r in refs])
    assert [code for code, _, _ in issues] == ["not_found", "metadata_mismatch", "unverified"]


def test_paper_state_and_evidence_level() -> None:
    refs = [ref("ref_a", "a"), ref("ref_b", "b")]
    papers = [
        paper(
            "ref_a",
            PaperState.NEEDS_ATTENTION,
            issues=[PaperIssue(kind="metadata", message="year disagrees")],
        ),
        paper("ref_b", level=EvidenceLevel.ABSTRACT_ONLY),
    ]
    assert check("[@a] [@b]", refs, papers) == [
        ("needs_attention", "a", ["0.0"]),
        ("limited_evidence", "b", ["0.2"]),
    ]


def test_same_work_cited_twice() -> None:
    a = ref("ref_a", "a", csl={"title": TITLE, "DOI": "10.1/x"})
    b = ref("ref_b", "b", csl={"title": "other", "DOI": "10.1/X"})
    assert check("[@a] and [@b]", [a, b], [paper("ref_a"), paper("ref_b")]) == [
        ("duplicate_cited", "a", ["0.0", "0.2"])
    ]
    a_distinct = a.model_copy(update={"distinct_from": ["ref_b"]})
    assert check("[@a] and [@b]", [a_distinct, b], [paper("ref_a"), paper("ref_b")]) == []


def test_stale_check_and_uncited() -> None:
    old = ref("ref_old", "old")
    old.verification.checked_at = utcnow() - timedelta(days=200)
    approved = ref("ref_ok", "ok")
    issues = check(
        "[@old]", [old, approved], [paper("ref_old"), paper("ref_ok", PaperState.APPROVED)]
    )
    assert issues == [("stale_check", "old", ["0.0"]), ("uncited", "ok", [])]


def test_issues_are_ordered_by_severity_then_position() -> None:
    refs = [ref(f"ref_{k}", k) for k in "abc"]
    refs[0] = ref("ref_a", "a", VerificationStatus.UNVERIFIED)
    refs[2] = ref("ref_c", "c", v={"retracted": True})
    text = (
        "\n\n".join(f"P{i} [@{k}]" for i, k in enumerate("abc"))
        + "\n\n"
        + "\n\n".join(["filler"] * 8)
        + "\n\n[@b] [@ghost]"
    )
    codes = [code for code, _, _ in check(text, refs, [paper(r.id) for r in refs])]
    assert codes == ["retracted", "missing_reference", "unverified"]


# --- API --------------------------------------------------------------------------------


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()

    def session() -> Iterator[Session]:
        with factory() as s, s.begin():
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_store] = lambda: LocalObjectStore(tmp_path)
    yield TestClient(app)


def test_api(client: TestClient, factory: sessionmaker[Session]) -> None:
    profile = ResearchProfile(problem="p", method="m", data="d", contribution="c")
    with transaction(factory) as s:
        project = repo.create_project(s, Project(name="T", profile=profile))
        good = repo.add_reference(
            s, ref("ref_good", "good").model_copy(update={"project_id": project.id})
        )
        bad = ref("ref_bad", "bad", v={"retracted": True}).model_copy(
            update={"project_id": project.id}
        )
        repo.add_reference(s, bad)
        for r in (good, bad):
            repo.add_paper(
                s,
                Paper(
                    project_id=project.id, reference_id=r.id, evidence_level=EvidenceLevel.FULL_TEXT
                ),
            )

    resp = client.post(
        f"/projects/{project.id}/documents",
        json={"text": "## Related\n\nGood [@good]. Bad \\cite{bad}. Missing [@nope]."},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert (body["citations"], body["unresolved_keys"]) == (3, ["nope"])
    doc_id = body["document"]["id"]

    check_body = client.get(f"/documents/{doc_id}/reference-check").json()
    assert (check_body["ok"], check_body["errors"]) == (False, 2)
    assert [i["code"] for i in check_body["report"]["issues"]] == ["retracted", "missing_reference"]

    revised = client.put(f"/documents/{doc_id}", json={"text": "Good [@good]."}).json()
    assert revised["document"]["version"] == 2
    assert client.get(f"/documents/{doc_id}/reference-check").json()["ok"] is True
    assert client.get("/documents/doc_nope/reference-check").status_code == 404
