"""Claims and verdicts (design doc §6.2, §10)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from cibud.models.common import LLMProvenance, new_id, stable_hash


class Verdict(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"
    UNABLE_TO_VERIFY = "unable_to_verify"
    STALE = "stale"


# Most severe first. A sentence shows the most severe verdict among its claims (§10.2).
# STALE outranks everything: an outdated verdict must never be displayed as current.
VERDICT_SEVERITY: tuple[Verdict, ...] = (
    Verdict.STALE,
    Verdict.UNSUPPORTED,
    Verdict.CONFLICTING,
    Verdict.PARTIAL,
    Verdict.UNABLE_TO_VERIFY,
    Verdict.SUPPORTED,
)


def claim_hash(text: str, reference_ids: list[str]) -> str:
    """Staleness key: covers the sentence text *and* its cited references (§10.4).

    Order of references does not matter; adding or removing one does.
    """
    return stable_hash({"text": text, "refs": sorted(set(reference_ids))})


def sentence_verdict(verdicts: list[Verdict]) -> Verdict | None:
    """Aggregate atomic-claim verdicts into the status shown for a sentence."""
    if not verdicts:
        return None
    return min(verdicts, key=VERDICT_SEVERITY.index)


class Claim(BaseModel):
    id: str = Field(default_factory=lambda: new_id("claim"))
    document_id: str
    node_path: str  # location of the source sentence in the document tree
    sentence_hash: str  # claim_hash() of the source sentence when last validated
    text: str  # atomic claim decomposed from the sentence
    reference_ids: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    verdict: Verdict = Verdict.STALE
    supporting_span: str | None = None
    rationale: str = ""
    validated_at: datetime | None = None
    provenance: LLMProvenance | None = None

    def is_stale_for(self, sentence_text: str, reference_ids: list[str]) -> bool:
        return self.sentence_hash != claim_hash(sentence_text, reference_ids)
