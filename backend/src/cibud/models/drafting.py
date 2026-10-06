"""Constrained writer output (design doc §9).

The writer may only cite approved references and evidence belonging to them. These rules are
enforced in code by ``check_paragraph``, so a hallucinated citation is a validation error rather
than something the auditor has to catch later.
"""

from collections.abc import Mapping, Set
from enum import StrEnum

from pydantic import BaseModel, Field


class ClaimType(StrEnum):
    FACTUAL = "factual"
    COMPARATIVE = "comparative"
    SYNTHESIS = "synthesis"
    TRANSITION = "transition"


class DraftSentence(BaseModel):
    text: str = Field(min_length=1)
    reference_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_type: ClaimType


class DraftParagraph(BaseModel):
    theme_id: str
    sentences: list[DraftSentence] = Field(min_length=1)


# Claim types that assert something about the literature and therefore need a citation.
CITATION_REQUIRED = {ClaimType.FACTUAL, ClaimType.COMPARATIVE}


def check_paragraph(
    paragraph: DraftParagraph,
    approved_reference_ids: Set[str],
    evidence_to_reference: Mapping[str, str],
) -> list[str]:
    """Return a list of violations; empty means the paragraph is acceptable."""
    errors: list[str] = []
    for i, sentence in enumerate(paragraph.sentences):
        where = f"sentence {i}"
        if sentence.claim_type in CITATION_REQUIRED and not sentence.reference_ids:
            errors.append(f"{where}: {sentence.claim_type} sentence has no citation")
        for ref_id in sentence.reference_ids:
            if ref_id not in approved_reference_ids:
                errors.append(f"{where}: reference {ref_id!r} is not in the approved set")
        cited = set(sentence.reference_ids)
        for ev_id in sentence.evidence_ids:
            owner = evidence_to_reference.get(ev_id)
            if owner is None:
                errors.append(f"{where}: unknown evidence {ev_id!r}")
            elif owner not in cited:
                errors.append(
                    f"{where}: evidence {ev_id!r} belongs to {owner!r}, which is not cited"
                )
    return errors
