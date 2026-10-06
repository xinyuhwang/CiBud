"""``cibud-eval``: tools for building evaluation corpora.

cibud-eval validate eval/corpus/<name>
cibud-eval prefill eval/corpus/<name>/papers.yaml [-o OUT]
"""

import argparse
import sys
from pathlib import Path

import httpx
import yaml
from pydantic import TypeAdapter, ValidationError

from cibud.eval.corpus import check_corpus, load_corpus
from cibud.eval.prefill import prefill
from cibud.eval.schemas import EvalPaper
from cibud.settings import get_settings


def _validate(args: argparse.Namespace) -> int:
    try:
        corpus = load_corpus(Path(args.corpus))
    except (FileNotFoundError, ValidationError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report = check_corpus(corpus)
    for key, value in report.stats.items():
        print(f"{key:>18}: {value}")
    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0 if report.ok else 1


def _prefill(args: argparse.Namespace) -> int:
    source = Path(args.papers)
    papers = TypeAdapter(list[EvalPaper]).validate_python(yaml.safe_load(source.read_text()))
    headers = {"User-Agent": "cibud-eval/0.1"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        filled, problems = prefill(papers, client, get_settings().contact_email)
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    out = yaml.safe_dump(
        [p.model_dump(mode="json", exclude_none=True) for p in filled],
        sort_keys=False,
        allow_unicode=True,
    )
    if args.output:
        Path(args.output).write_text(out)
    else:
        print(out, end="")
    print(
        "note: prefilled metadata is unverified; check each entry against the paper "
        "and set metadata_verified: true",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cibud-eval")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="schema and consistency check of a corpus")
    p_validate.add_argument("corpus", help="corpus directory")
    p_validate.set_defaults(func=_validate)

    p_prefill = sub.add_parser("prefill", help="fill missing metadata from Crossref/arXiv")
    p_prefill.add_argument("papers", help="path to papers.yaml")
    p_prefill.add_argument("-o", "--output", help="write here instead of stdout")
    p_prefill.set_defaults(func=_prefill)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
