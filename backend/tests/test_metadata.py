"""Metadata resolution: source parsers, comparison, precedence, and conflicts (pure)."""

from typing import Any

import pytest

from cibud.metadata.compare import (
    authors_match,
    normalize_doi,
    title_similarity,
    year_of,
)
from cibud.metadata.resolver import (
    best_search_match,
    choose,
    find_conflicts,
    merge_record,
    same_paper,
    set_user_value,
    verify,
)
from cibud.metadata.sources import (
    SourceRecord,
    parse_arxiv,
    parse_crossref,
    parse_openalex,
    split_name,
)
from cibud.models import FieldCandidate, MetadataSource
from cibud.models.reference import VerificationStatus

S = MetadataSource
TITLE = "Attention-Based Sepsis Prediction from Electronic Health Records"

CROSSREF_MESSAGE: dict[str, Any] = {
    "type": "journal-article",
    "title": [TITLE],
    "author": [{"given": "Lin", "family": "Chen"}, {"given": "Kai", "family": "Ito"}],
    "issued": {"date-parts": [[2022, 1]]},
    "DOI": "10.1000/JCI.2022.42",
    "container-title": ["Journal of Clinical Informatics"],
    "volume": "118",
    "page": "103790",
    "publisher": "Example Press",
    "updated-by": [
        {"type": "correction", "DOI": "10.1000/jci.2022.99", "source": "retraction-watch"}
    ],
}

OPENALEX_WORK: dict[str, Any] = {
    "title": TITLE,
    "type": "article",
    "doi": "https://doi.org/10.1000/jci.2022.42",
    "publication_date": "2022-01-15",
    "authorships": [
        {"author": {"display_name": "Lin Chen"}},
        {"author": {"display_name": "Kai Ito"}},
    ],
    "primary_location": {"source": {"display_name": "Journal of Clinical Informatics"}},
    "biblio": {"volume": "118", "first_page": "103790", "last_page": None},
    "is_retracted": False,
}

ARXIV_ATOM = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2103.01234v2</id>
    <title>{TITLE}</title>
    <published>2021-03-04T00:00:00Z</published>
    <author><name>Lin Chen</name></author>
    <author><name>Kai Ito</name></author>
    <arxiv:doi>10.1000/jci.2022.42</arxiv:doi>
  </entry>
</feed>"""


def header_candidates(**csl: Any) -> dict[str, list[FieldCandidate]]:
    return {f: [FieldCandidate(value=v, source=S.PDF_HEADER)] for f, v in csl.items()}


class TestParsers:
    def test_crossref(self) -> None:
        r = parse_crossref(CROSSREF_MESSAGE)
        assert r.csl["type"] == "article-journal"
        assert r.csl["DOI"] == "10.1000/jci.2022.42"  # lower-cased
        assert r.csl["issued"] == {"date-parts": [[2022, 1]]}
        assert r.csl["author"][0] == {"family": "Chen", "given": "Lin"}
        assert r.corrected and not r.retracted
        assert r.notices == ["correction 10.1000/jci.2022.99"]

    def test_crossref_retraction(self) -> None:
        msg = {**CROSSREF_MESSAGE, "updated-by": [{"type": "retraction", "DOI": "10.1/x"}]}
        assert parse_crossref(msg).retracted

    def test_crossref_drops_authors_without_family_and_empty_fields(self) -> None:
        r = parse_crossref({"title": ["T"], "author": [{"name": "A Consortium"}]})
        assert "author" not in r.csl
        assert "DOI" not in r.csl

    def test_openalex(self) -> None:
        r = parse_openalex(OPENALEX_WORK)
        assert r.csl["DOI"] == "10.1000/jci.2022.42"
        assert r.csl["issued"] == {"date-parts": [[2022, 1, 15]]}
        assert r.csl["author"][1] == {"family": "Ito", "given": "Kai"}
        assert r.csl["container-title"] == "Journal of Clinical Informatics"
        assert r.csl["page"] == "103790"
        assert parse_openalex({**OPENALEX_WORK, "is_retracted": True}).retracted
        preprint = parse_openalex({**OPENALEX_WORK, "type": "preprint"})
        assert "container-title" not in preprint.csl

    def test_arxiv(self) -> None:
        r = parse_arxiv(ARXIV_ATOM)
        assert r is not None
        assert r.csl["arxiv"] == "2103.01234"
        assert r.csl["issued"] == {"date-parts": [[2021, 3, 4]]}
        assert r.csl["published-doi"] == "10.1000/jci.2022.42"
        assert "DOI" not in r.csl  # the preprint's DOI is not the published DOI

    def test_arxiv_unknown_id(self) -> None:
        empty = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        assert parse_arxiv(empty) is None

    def test_split_name(self) -> None:
        assert split_name("Ada M. Lovelace") == {"family": "Lovelace", "given": "Ada M."}
        assert split_name("Plato") == {"family": "Plato"}


class TestCompare:
    def test_title_similarity_ignores_case_punctuation_and_accents(self) -> None:
        assert title_similarity("Café: A Study.", "cafe - a study") == 1.0
        assert title_similarity("Sepsis prediction", "Protein folding") < 0.5

    def test_authors_match_on_family_names(self) -> None:
        a = [{"family": "Chén", "given": "L."}, {"family": "Ito"}]
        b = [{"family": "chen", "given": "Lin"}, {"family": "Ito", "given": "Kai"}]
        assert authors_match(a, b)
        assert not authors_match(a, b[:1])

    def test_year_and_doi(self) -> None:
        assert year_of({"date-parts": [[2020, 5]]}) == 2020
        assert year_of(None) is None
        assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
        assert normalize_doi("") is None


class TestPrecedence:
    def test_registry_beats_pdf_header_and_user_beats_all(self) -> None:
        prov = header_candidates(title="Header title", issued={"date-parts": [[2021]]})
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        csl = choose(prov, {"type": "article"})
        assert csl["title"] == TITLE
        assert year_of(csl["issued"]) == 2022

        prov = set_user_value(prov, "issued", {"date-parts": [[2023]]})
        assert year_of(choose(prov, {})["issued"]) == 2023

    def test_merge_replaces_same_source_candidates(self) -> None:
        prov = merge_record({}, parse_crossref(CROSSREF_MESSAGE))
        prov = merge_record(prov, parse_crossref({**CROSSREF_MESSAGE, "volume": "119"}))
        assert [c.value for c in prov["volume"]] == ["119"]

    def test_choose_keeps_base_fields_without_candidates(self) -> None:
        assert choose({}, {"title": "from filename"}) == {"title": "from filename"}


class TestConflicts:
    def test_agreeing_sources_have_no_conflicts(self) -> None:
        prov = header_candidates(
            title=TITLE.upper(), author=[{"family": "Chen"}], issued={"date-parts": [[2022]]}
        )
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        prov = merge_record(prov, parse_openalex(OPENALEX_WORK))
        assert find_conflicts(prov) == []

    def test_preprint_vs_published_year(self) -> None:
        """Design doc §7.3's example: the same paper with two different years."""
        prov = merge_record({}, parse_arxiv(ARXIV_ATOM))  # type: ignore[arg-type]
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        [conflict] = find_conflicts(prov)
        assert conflict.field == "issued"
        assert conflict.describe() == "issued disagrees: 2022 (crossref), 2021 (arxiv)"

    def test_online_first_vs_print_year_is_not_a_conflict(self) -> None:
        """Crossref dates the print issue, OpenAlex the online-first date (same DOI)."""
        online_first = {**OPENALEX_WORK, "publication_date": "2021-12-20"}
        prov = merge_record({}, parse_crossref(CROSSREF_MESSAGE))
        assert find_conflicts(merge_record(prov, parse_openalex(online_first))) == []
        years_apart = {**OPENALEX_WORK, "publication_date": "2019-01-01"}
        conflicts = find_conflicts(merge_record(prov, parse_openalex(years_apart)))
        assert [c.field for c in conflicts] == ["issued"]

    def test_pdf_header_authors_only_checked_on_first_author(self) -> None:
        crossref = parse_crossref(CROSSREF_MESSAGE)
        incomplete = header_candidates(author=[{"family": "Chen"}])
        assert find_conflicts(merge_record(incomplete, crossref)) == []
        wrong_first = header_candidates(author=[{"family": "Ito"}, {"family": "Chen"}])
        assert [c.field for c in find_conflicts(merge_record(wrong_first, crossref))] == ["author"]

    def test_registries_must_agree_on_full_author_list(self) -> None:
        work = {**OPENALEX_WORK, "authorships": OPENALEX_WORK["authorships"][:1]}
        prov = merge_record(
            merge_record({}, parse_crossref(CROSSREF_MESSAGE)), parse_openalex(work)
        )
        assert [c.field for c in find_conflicts(prov)] == ["author"]

    def test_substantially_different_title(self) -> None:
        prov = header_candidates(title="Sepsis prediction with attention")
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        assert [c.field for c in find_conflicts(prov)] == ["title"]

    def test_user_choice_resolves_conflict(self) -> None:
        prov = merge_record({}, parse_arxiv(ARXIV_ATOM))  # type: ignore[arg-type]
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        prov = set_user_value(prov, "issued", {"date-parts": [[2022]]})
        assert find_conflicts(prov) == []

    def test_venue_differences_are_not_conflicts(self) -> None:
        prov = header_candidates(**{"container-title": "J Clin Inform"})
        prov = merge_record(prov, parse_crossref(CROSSREF_MESSAGE))
        assert find_conflicts(prov) == []


class TestIdentity:
    def test_same_paper(self) -> None:
        record = parse_crossref(CROSSREF_MESSAGE)
        assert same_paper(TITLE, record)
        assert not same_paper("Deep learning for protein structure", record)
        assert same_paper(None, record)  # nothing to compare against

    def test_best_search_match_requires_close_title_and_first_author(self) -> None:
        good = parse_crossref(CROSSREF_MESSAGE)
        near_title = parse_crossref({**CROSSREF_MESSAGE, "title": [TITLE + ": A Survey"]})
        other_author = parse_crossref(
            {**CROSSREF_MESSAGE, "author": [{"family": "Smith"}], "DOI": "10.1/other"}
        )
        assert best_search_match(TITLE, "Chen", [near_title, other_author, good]) is good
        assert best_search_match(TITLE, "Chen", [other_author]) is None


@pytest.mark.parametrize(
    ("records", "rejected", "conflicted", "status"),
    [
        ([SourceRecord(S.CROSSREF, {})], [], False, VerificationStatus.VERIFIED),
        ([], [], False, VerificationStatus.NOT_FOUND),
        ([SourceRecord(S.CROSSREF, {})], ["wrong paper"], False, VerificationStatus.MISMATCH),
        ([SourceRecord(S.CROSSREF, {})], [], True, VerificationStatus.MISMATCH),
    ],
)
def test_verify_status(
    records: list[SourceRecord], rejected: list[str], conflicted: bool, status: VerificationStatus
) -> None:
    conflicts = find_conflicts(
        merge_record(
            merge_record({}, parse_arxiv(ARXIV_ATOM)),  # type: ignore[arg-type]
            parse_crossref(CROSSREF_MESSAGE),
        )
    )
    result = verify(records, rejected, conflicts if conflicted else [])
    assert result.status is status
    assert result.rejected_records == rejected
