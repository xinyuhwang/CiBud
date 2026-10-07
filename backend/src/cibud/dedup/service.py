"""Find, merge, and dismiss duplicate papers within a project (design doc §5, §7.1).

Possible duplicates are never merged silently: the later-resolved paper is flagged with a
``duplicate`` issue, and the user either merges the two or marks them as different works.
"""

from sqlalchemy.orm import Session

from cibud.db import repo
from cibud.dedup.match import DuplicateMatch, match
from cibud.metadata.resolver import choose
from cibud.models.common import EvidenceLevel
from cibud.models.paper import Paper, PaperIssue, PaperState
from cibud.models.reference import Reference

# Papers past metadata review were already vetted; a new duplicate flags the newcomer instead.
_FLAGGABLE = {PaperState.METADATA_REVIEW, PaperState.METADATA_ONLY, PaperState.NEEDS_ATTENTION}


class MergeError(ValueError):
    pass


def find_duplicates(session: Session, reference: Reference) -> list[DuplicateMatch]:
    matches = []
    for other in repo.list_references(session, reference.project_id):
        if other.id == reference.id:
            continue
        if other.id in reference.distinct_from or reference.id in other.distinct_from:
            continue
        if kind := match(reference.csl, other.csl):
            matches.append(DuplicateMatch(other.id, other.citation_key, kind))
    return matches


def duplicate_issues(session: Session, reference: Reference) -> list[PaperIssue]:
    return [
        PaperIssue(kind="duplicate", message=m.describe())
        for m in find_duplicates(session, reference)
    ]


def refresh_duplicate_flags(session: Session, paper: Paper) -> None:
    if paper.state not in _FLAGGABLE:
        return
    reference = repo.get_reference(session, paper.reference_id)
    repo.update_issues(session, paper.id, {"duplicate"}, duplicate_issues(session, reference))


def _refresh_project_flags(session: Session, project_id: str) -> None:
    """Re-check every paper currently flagged as a duplicate (after a merge or dismissal)."""
    for paper in repo.list_papers(session, project_id, PaperState.NEEDS_ATTENTION):
        if any(i.kind == "duplicate" for i in paper.issues):
            refresh_duplicate_flags(session, paper)


def mark_distinct(session: Session, paper_id: str, other_paper_id: str) -> None:
    """The user says these are different works; never flag the pair again."""
    a, b = repo.get_paper(session, paper_id), repo.get_paper(session, other_paper_id)
    if a.project_id != b.project_id or a.id == b.id:
        raise MergeError("papers must be two different papers in the same project")
    ref_a = repo.get_reference(session, a.reference_id)
    ref_b = repo.get_reference(session, b.reference_id)
    for ref, other in ((ref_a, ref_b), (ref_b, ref_a)):
        if other.id not in ref.distinct_from:
            repo.save_reference(
                session, ref.model_copy(update={"distinct_from": [*ref.distinct_from, other.id]})
            )
    _refresh_project_flags(session, a.project_id)


def merge_papers(session: Session, source_id: str, target_id: str) -> Paper:
    """Fold ``source`` into ``target`` and delete ``source``.

    - The target's bibliographic data wins; the source only fills fields the target lacks
      (e.g. the arXiv ID when merging a preprint into its published version).
    - If only the source has full text, its passages and extracted text move to the target.
    - Source files are kept on the target, so nothing the user uploaded is lost.
    """
    source, target = repo.get_paper(session, source_id), repo.get_paper(session, target_id)
    if source.project_id != target.project_id or source.id == target.id:
        raise MergeError("papers must be two different papers in the same project")
    if repo.is_reference_cited(session, source.reference_id):
        raise MergeError(
            "the paper being merged away is cited in a draft; replace those citations first"
        )

    src_ref = repo.get_reference(session, source.reference_id)
    dst_ref = repo.get_reference(session, target.reference_id)
    provenance = dict(dst_ref.field_provenance)
    for field, candidates in src_ref.field_provenance.items():
        provenance.setdefault(field, candidates)
    merged = dst_ref.model_copy(
        update={
            "field_provenance": provenance,
            "csl": choose(provenance, {**src_ref.csl, **dst_ref.csl}),
            "distinct_from": sorted(set(dst_ref.distinct_from) | set(src_ref.distinct_from)),
        }
    )
    repo.save_reference(session, merged)

    gains_full_text = (
        target.evidence_level is not EvidenceLevel.FULL_TEXT
        and source.evidence_level is EvidenceLevel.FULL_TEXT
        and source.extracted_text_ref is not None
    )
    if gains_full_text:
        assert source.extracted_text_ref is not None
        repo.move_passages(session, source.id, target.id)
        repo.record_extraction(
            session,
            target.id,
            extracted_text_ref=source.extracted_text_ref,
            evidence_level=EvidenceLevel.FULL_TEXT,
        )
    repo.add_source_files(session, target.id, source.source_files)
    repo.delete_paper(session, source.id)

    if gains_full_text and target.state is PaperState.METADATA_ONLY:
        repo.set_paper_state(session, target.id, PaperState.METADATA_REVIEW)
    _refresh_project_flags(session, target.project_id)
    refresh_duplicate_flags(session, repo.get_paper(session, target.id))
    return repo.get_paper(session, target.id)
