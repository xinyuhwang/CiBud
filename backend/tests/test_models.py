from typing import ClassVar

import pytest
from pydantic import ValidationError

from cibud.models import (
    Claim,
    ClaimType,
    DraftParagraph,
    DraftSentence,
    EvidencePassage,
    Job,
    Verdict,
    check_paragraph,
    claim_hash,
    sentence_verdict,
)
from cibud.models.document import iter_citation_nodes


class TestClaimHash:
    def test_ignores_reference_order_and_duplicates(self) -> None:
        assert claim_hash("x", ["a", "b"]) == claim_hash("x", ["b", "a", "a"])

    def test_changes_with_text(self) -> None:
        assert claim_hash("x", ["a"]) != claim_hash("y", ["a"])

    def test_changes_with_references(self) -> None:
        assert claim_hash("x", ["a"]) != claim_hash("x", ["a", "b"])

    def test_claim_staleness(self) -> None:
        claim = Claim(
            document_id="doc_1",
            node_path="0.1",
            sentence_hash=claim_hash("Models improved AUROC.", ["ref_a"]),
            text="Models improved AUROC.",
            reference_ids=["ref_a"],
        )
        assert not claim.is_stale_for("Models improved AUROC.", ["ref_a"])
        assert claim.is_stale_for("Models improved AUROC.", [])
        assert claim.is_stale_for("Models greatly improved AUROC.", ["ref_a"])


class TestSentenceVerdict:
    def test_empty(self) -> None:
        assert sentence_verdict([]) is None

    def test_weakest_wins(self) -> None:
        assert sentence_verdict([Verdict.SUPPORTED, Verdict.UNSUPPORTED]) is Verdict.UNSUPPORTED
        assert sentence_verdict([Verdict.SUPPORTED, Verdict.PARTIAL]) is Verdict.PARTIAL

    def test_unverifiable_is_never_shown_as_supported(self) -> None:
        verdicts = [Verdict.SUPPORTED, Verdict.UNABLE_TO_VERIFY]
        assert sentence_verdict(verdicts) is Verdict.UNABLE_TO_VERIFY

    def test_stale_outranks_everything(self) -> None:
        assert sentence_verdict([Verdict.UNSUPPORTED, Verdict.STALE]) is Verdict.STALE


class TestCheckParagraph:
    approved = frozenset({"ref_a", "ref_b"})
    evidence: ClassVar[dict[str, str]] = {"ev_1": "ref_a", "ev_2": "ref_b"}

    def para(self, *sentences: DraftSentence) -> DraftParagraph:
        return DraftParagraph(theme_id="t1", sentences=list(sentences))

    def test_valid(self) -> None:
        p = self.para(
            DraftSentence(
                text="A did X.",
                reference_ids=["ref_a"],
                evidence_ids=["ev_1"],
                claim_type=ClaimType.FACTUAL,
            ),
            DraftSentence(text="In contrast,", claim_type=ClaimType.TRANSITION),
        )
        assert check_paragraph(p, self.approved, self.evidence) == []

    def test_hallucinated_reference(self) -> None:
        p = self.para(
            DraftSentence(text="Z did X.", reference_ids=["ref_z"], claim_type=ClaimType.FACTUAL)
        )
        assert any("not in the approved set" in e for e in check_paragraph(p, self.approved, {}))

    def test_uncited_factual_claim(self) -> None:
        p = self.para(DraftSentence(text="Everyone does X.", claim_type=ClaimType.FACTUAL))
        assert any("no citation" in e for e in check_paragraph(p, self.approved, {}))

    def test_evidence_from_uncited_reference(self) -> None:
        p = self.para(
            DraftSentence(
                text="A did X.",
                reference_ids=["ref_a"],
                evidence_ids=["ev_2"],
                claim_type=ClaimType.FACTUAL,
            )
        )
        errors = check_paragraph(p, self.approved, self.evidence)
        assert any("which is not cited" in e for e in errors)

    def test_unknown_evidence(self) -> None:
        p = self.para(
            DraftSentence(
                text="A did X.",
                reference_ids=["ref_a"],
                evidence_ids=["ev_9"],
                claim_type=ClaimType.FACTUAL,
            )
        )
        assert any("unknown evidence" in e for e in check_paragraph(p, self.approved, {}))


def test_iter_citation_nodes_matches_design_doc_example() -> None:
    paragraph = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Attention-based models have been applied to clinical data "},
            {
                "type": "citation",
                "attrs": {
                    "items": [
                        {"ref": "ref_8f2a", "locator": "4", "label": "page"},
                        {"ref": "ref_c41d"},
                    ],
                    "mode": "parenthetical",
                },
            },
            {"type": "text", "text": "."},
        ],
    }
    nodes = iter_citation_nodes(paragraph)
    assert [item.ref for item in nodes[0].items] == ["ref_8f2a", "ref_c41d"]
    assert nodes[0].items[0].locator == "4"


def test_evidence_span_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        EvidencePassage(paper_id="p", text="t", char_span=(10, 5))


def test_job_inputs_hash_is_deterministic() -> None:
    a = Job.create("analyze_paper", {"paper_id": "p1", "profile_version": 2})
    b = Job.create("analyze_paper", {"profile_version": 2, "paper_id": "p1"})
    c = Job.create("analyze_paper", {"paper_id": "p1", "profile_version": 3})
    assert a.inputs_hash == b.inputs_hash != c.inputs_hash
    assert a.id != b.id
