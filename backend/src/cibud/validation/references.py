"""Deterministic reference checks over any document (design doc §10.1).

Works on the citation nodes of a document tree, whoever wrote the text. Issues are grouped
per reference (listing every citation node involved), so one bad reference cited ten times
is one issue, not ten.
"""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from cibud.dedup.match import match
from cibud.models.common import EvidenceLevel, utcnow
from cibud.models.document import citation_nodes_with_paths
from cibud.models.paper import Paper, PaperState
from cibud.models.reference import Reference, VerificationStatus

# Retraction status can change after import; older checks are reported as possibly outdated.
STALE_AFTER = timedelta(days=90)


class Severity(StrEnum):
    ERROR = "error"  # blocks a verified export
    WARNING = "warning"  # needs a human look
    INFO = "info"  # worth knowing


class CheckCode(StrEnum):
    MISSING_REFERENCE = "missing_reference"
    RETRACTED = "retracted"
    CORRECTED = "corrected"
    NOT_FOUND = "not_found"
    METADATA_MISMATCH = "metadata_mismatch"
    UNVERIFIED = "unverified"
    NEEDS_ATTENTION = "needs_attention"
    DUPLICATE_CITED = "duplicate_cited"
    LIMITED_EVIDENCE = "limited_evidence"
    STALE_CHECK = "stale_check"
    UNCITED = "uncited"


SEVERITY = {
    CheckCode.MISSING_REFERENCE: Severity.ERROR,
    CheckCode.RETRACTED: Severity.ERROR,
    CheckCode.CORRECTED: Severity.WARNING,
    CheckCode.NOT_FOUND: Severity.WARNING,
    CheckCode.METADATA_MISMATCH: Severity.WARNING,
    CheckCode.NEEDS_ATTENTION: Severity.WARNING,
    CheckCode.DUPLICATE_CITED: Severity.WARNING,
    CheckCode.UNVERIFIED: Severity.INFO,
    CheckCode.LIMITED_EVIDENCE: Severity.INFO,
    CheckCode.STALE_CHECK: Severity.INFO,
    CheckCode.UNCITED: Severity.INFO,
}


class ReferenceIssue(BaseModel):
    code: CheckCode
    severity: Severity
    message: str
    reference_id: str | None = None
    citation_key: str | None = None
    node_paths: list[str] = Field(default_factory=list)  # citation nodes involved


class ReferenceReport(BaseModel):
    citation_nodes: int
    cited_references: int
    issues: list[ReferenceIssue]

    def count(self, severity: Severity) -> int:
        return sum(i.severity is severity for i in self.issues)

    @property
    def ok(self) -> bool:
        """No errors. Warnings and info do not block, but are shown."""
        return self.count(Severity.ERROR) == 0


def _issue(code: CheckCode, message: str, **kwargs: Any) -> ReferenceIssue:
    return ReferenceIssue(code=code, severity=SEVERITY[code], message=message, **kwargs)


def _reference_issues(
    ref: Reference, paper: Paper | None, paths: list[str], now: datetime
) -> list[ReferenceIssue]:
    v = ref.verification
    key = ref.citation_key
    found: list[tuple[CheckCode, str]] = []
    if v.retracted:
        notice = next((n for n in v.issues if n.startswith("retraction")), "retraction notice")
        found.append((CheckCode.RETRACTED, f"{key} has been retracted ({notice})"))
    if v.has_correction:
        found.append(
            (CheckCode.CORRECTED, f"{key} has a published correction; check the claim still holds")
        )
    if v.status is VerificationStatus.NOT_FOUND:
        found.append((CheckCode.NOT_FOUND, f"{key} was not found in Crossref, OpenAlex, or arXiv"))
    elif v.status is VerificationStatus.MISMATCH:
        detail = "; ".join(v.rejected_records) or "sources disagree on its metadata"
        found.append((CheckCode.METADATA_MISMATCH, f"{key}: {detail}"))
    elif v.status is VerificationStatus.UNVERIFIED:
        found.append((CheckCode.UNVERIFIED, f"{key} has not been checked against a registry yet"))
    if v.checked_at is not None and now - v.checked_at > STALE_AFTER:
        found.append(
            (
                CheckCode.STALE_CHECK,
                f"{key} was last checked {v.checked_at:%Y-%m-%d}; "
                "its retraction status may be outdated",
            )
        )
    if paper is not None and paper.state is PaperState.NEEDS_ATTENTION:
        open_issues = "; ".join(i.message for i in paper.issues)
        found.append((CheckCode.NEEDS_ATTENTION, f"{key} has unresolved issues: {open_issues}"))
    if paper is not None and paper.evidence_level is not EvidenceLevel.FULL_TEXT:
        level = paper.evidence_level.value.replace("_", " ")
        found.append(
            (
                CheckCode.LIMITED_EVIDENCE,
                f"{key} is {level}; claims citing it can't be fully verified",
            )
        )
    return [
        _issue(code, message, reference_id=ref.id, citation_key=key, node_paths=paths)
        for code, message in found
    ]


def _position(paths: list[str]) -> tuple[int, ...]:
    """Document order of an issue's first citation node; issues without nodes sort last."""
    if not paths:
        return (1_000_000,)
    return tuple(int(p) for p in paths[0].split("."))


def check_references(
    content: dict[str, Any],
    references: list[Reference],
    papers: Mapping[str, Paper],
    now: datetime | None = None,
) -> ReferenceReport:
    """``papers`` maps reference ID -> paper. ``references`` is the project's full library."""
    now = now or utcnow()
    by_id = {r.id: r for r in references}
    by_key = {r.citation_key: r for r in references}
    nodes = citation_nodes_with_paths(content)

    cited_paths: dict[str, list[str]] = defaultdict(list)
    removed_paths: dict[str, list[str]] = defaultdict(list)  # node points at a deleted ref
    unknown_paths: dict[str, list[str]] = defaultdict(list)  # key matches nothing
    for path, attrs in nodes:
        for item in attrs.items:
            if item.ref is not None:
                if item.ref in by_id:
                    cited_paths[item.ref].append(path)
                else:
                    removed_paths[item.ref].append(path)
            elif (ref := by_key.get(item.key or "")) is not None:
                cited_paths[ref.id].append(path)
            else:
                unknown_paths[item.key or "?"].append(path)

    issues = [
        _issue(
            CheckCode.MISSING_REFERENCE,
            "citation points to a reference that was removed from the library",
            reference_id=ref_id,
            node_paths=paths,
        )
        for ref_id, paths in removed_paths.items()
    ] + [
        _issue(
            CheckCode.MISSING_REFERENCE,
            f"citation key {key!r} does not match any reference in the library",
            citation_key=key,
            node_paths=paths,
        )
        for key, paths in unknown_paths.items()
    ]
    cited_paths = {r: list(dict.fromkeys(p)) for r, p in cited_paths.items()}
    for ref_id, paths in cited_paths.items():
        issues += _reference_issues(by_id[ref_id], papers.get(ref_id), paths, now)

    cited = [by_id[r] for r in cited_paths]
    for a, b in combinations(cited, 2):
        if b.id in a.distinct_from or a.id in b.distinct_from:
            continue
        if kind := match(a.csl, b.csl):
            issues.append(
                _issue(
                    CheckCode.DUPLICATE_CITED,
                    f"{a.citation_key} and {b.citation_key} look like the same work "
                    f"({kind.value.replace('_', ' ')}); cite one of them",
                    reference_id=a.id,
                    citation_key=a.citation_key,
                    node_paths=cited_paths[a.id] + cited_paths[b.id],
                )
            )

    for ref in references:
        paper = papers.get(ref.id)
        if ref.id not in cited_paths and paper is not None and paper.state is PaperState.APPROVED:
            issues.append(
                _issue(
                    CheckCode.UNCITED,
                    f"{ref.citation_key} is approved for this project but not cited",
                    reference_id=ref.id,
                    citation_key=ref.citation_key,
                )
            )

    order = list(Severity)
    issues.sort(key=lambda i: (order.index(i.severity), _position(i.node_paths)))
    return ReferenceReport(
        citation_nodes=len(nodes), cited_references=len(cited_paths), issues=issues
    )
