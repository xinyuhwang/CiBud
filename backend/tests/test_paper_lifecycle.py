from itertools import pairwise

import pytest

from cibud.models.paper import TRANSITIONS, InvalidTransition, PaperState, check_transition

S = PaperState


def walk(*states: PaperState) -> None:
    for current, target in pairwise(states):
        check_transition(current, target)


def test_every_state_has_rules() -> None:
    assert set(TRANSITIONS) == set(PaperState)


def test_full_text_happy_path() -> None:
    walk(S.IMPORTED, S.EXTRACTING, S.METADATA_REVIEW, S.ANALYZING, S.RELEVANCE_REVIEW, S.APPROVED)


def test_metadata_only_paper_can_still_be_approved() -> None:
    walk(S.IMPORTED, S.METADATA_ONLY, S.RELEVANCE_REVIEW, S.APPROVED)


def test_uploading_pdf_later_upgrades_metadata_only_paper() -> None:
    walk(S.METADATA_ONLY, S.EXTRACTING, S.METADATA_REVIEW)


def test_needs_attention_is_resolvable() -> None:
    walk(S.EXTRACTING, S.NEEDS_ATTENTION, S.EXTRACTING)
    walk(S.METADATA_REVIEW, S.NEEDS_ATTENTION, S.METADATA_REVIEW)


def test_profile_change_sends_decisions_back_to_review() -> None:
    walk(S.APPROVED, S.RELEVANCE_REVIEW)
    walk(S.EXCLUDED, S.RELEVANCE_REVIEW)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.IMPORTED, S.APPROVED),  # skipping review is never allowed
        (S.METADATA_ONLY, S.ANALYZING),  # no full text to analyze
        (S.RELEVANCE_REVIEW, S.EXTRACTING),
        (S.APPROVED, S.APPROVED),
    ],
)
def test_illegal_transitions(current: PaperState, target: PaperState) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(current, target)
