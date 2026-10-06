"""Load an evaluation corpus from disk and check it for consistency."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cibud.eval.schemas import CaseCategory, Corpus
from cibud.models.common import EvidenceLevel

FILES = ("profile", "papers", "relevance", "findings", "cases")
REQUIRED = {"profile", "papers"}

# Targets from the design doc (§16) and roadmap (Milestone 0).
MIN_PAPERS = 20
MIN_LIMITED_ACCESS = 3  # abstract-only / metadata-only papers to exercise "unable to verify"


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_yaml(path: Path) -> Any:
    with path.open() as f:
        return yaml.safe_load(f)


def load_corpus(directory: Path) -> Corpus:
    """Parse and schema-validate a corpus. Raises on missing files or schema errors."""
    data: dict[str, Any] = {"name": directory.name}
    for name in FILES:
        path = directory / f"{name}.yaml"
        if not path.exists():
            if name in REQUIRED:
                raise FileNotFoundError(path)
            continue
        data[name] = _read_yaml(path) or []
    return Corpus.model_validate(data)


def check_corpus(corpus: Corpus) -> Report:
    """Cross-file consistency checks and coverage warnings."""
    report = Report()
    keys = [p.key for p in corpus.papers]
    known = set(keys)

    for key, n in Counter(keys).items():
        if n > 1:
            report.errors.append(f"papers: duplicate key {key!r}")
    for label, ids in [
        ("findings", [f.id for f in corpus.findings]),
        ("cases", [c.id for c in corpus.cases]),
    ]:
        for item_id, n in Counter(ids).items():
            if n > 1:
                report.errors.append(f"{label}: duplicate id {item_id!r}")

    def unknown(where: str, key: str | None) -> None:
        if key is not None and key not in known:
            report.errors.append(f"{where}: unknown paper {key!r}")

    labelled = Counter(r.paper for r in corpus.relevance)
    for r in corpus.relevance:
        unknown("relevance", r.paper)
    for key, n in labelled.items():
        if n > 1:
            report.errors.append(f"relevance: paper {key!r} labelled {n} times")
    for f in corpus.findings:
        unknown(f"findings[{f.id}]", f.paper)
    for c in corpus.cases:
        unknown(f"cases[{c.id}]", c.paper)
        # Integrity cases may cite keys that are deliberately missing from the library.
        if c.kind != "integrity":
            for key in c.cites:
                unknown(f"cases[{c.id}]", key)

    n_papers = len(corpus.papers)
    limited = sum(p.access is not EvidenceLevel.FULL_TEXT for p in corpus.papers)
    verified = sum(p.metadata_verified for p in corpus.papers)
    defects = sum(c.is_defect for c in corpus.cases)
    controls = len(corpus.cases) - defects

    if n_papers < MIN_PAPERS:
        report.warnings.append(f"{n_papers} papers; target is {MIN_PAPERS}-30")
    if limited < MIN_LIMITED_ACCESS:
        report.warnings.append(
            f"{limited} limited-access papers; include at least {MIN_LIMITED_ACCESS} "
            "abstract-only/metadata-only papers"
        )
    if missing := known - set(labelled):
        report.warnings.append(f"{len(missing)} papers have no relevance label")
    if verified < n_papers:
        report.warnings.append(f"{n_papers - verified} papers have unverified metadata")
    if defects and controls < defects:
        report.warnings.append(
            f"{defects} defects vs {controls} valid controls; aim for at least as many controls "
            "so false alarms are measurable"
        )
    if corpus.cases:
        uncovered = set(CaseCategory) - {c.category for c in corpus.cases}
        if uncovered:
            report.warnings.append(f"no cases for: {', '.join(sorted(uncovered))}")

    report.stats = {
        "papers": n_papers,
        "limited_access": limited,
        "metadata_verified": verified,
        "relevance_labels": len(corpus.relevance),
        "findings": len(corpus.findings),
        "defect_cases": defects,
        "valid_controls": controls,
    }
    return report
