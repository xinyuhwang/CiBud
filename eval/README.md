# Evaluation corpora

The benchmark CiBud is measured against (design doc §16). It should be built **before** the
pipeline: every Milestone 1A–1C exit criterion is scored against it.

## Layout

```text
eval/
├── templates/          # copy these to start a corpus
└── corpus/<name>/
    ├── profile.yaml    # research profile that relevance is judged against
    ├── papers.yaml     # identifiers + hand-verified metadata
    ├── relevance.yaml  # relationship types and level per paper
    ├── findings.yaml   # key findings with verbatim supporting passages
    ├── cases.yaml      # seeded defects + valid controls
    └── pdfs/           # local copies of the papers (git-ignored, never committed)
```

## Building a corpus

1. Choose one research topic you know well. You will be judging whether passages support
   claims, so the benchmark is only as good as your domain knowledge.
2. Copy the templates:
   ```bash
   cp -r eval/templates eval/corpus/<name>
   ```
3. Write `profile.yaml`.
4. List 20–30 papers in `papers.yaml`. Use mostly open-access papers (arXiv, PMC), plus at
   least 3 paywalled or DOI-only papers to exercise the "unable to verify" path.
5. Optionally prefill metadata, then **check every entry against the paper itself** and set
   `metadata_verified: true`:
   ```bash
   cd backend
   uv run cibud-eval prefill ../eval/corpus/<name>/papers.yaml -o ../eval/corpus/<name>/papers.yaml
   ```
6. Label relevance, record findings with verbatim quotes, and write cases. Include at least
   as many `valid` controls as defects: an auditor that flags everything is as useless as one
   that flags nothing.
7. Validate:
   ```bash
   uv run cibud-eval validate ../eval/corpus/<name>
   ```
   This checks the schemas and cross-file references (unknown paper keys, duplicate IDs).
   It also warns about coverage gaps: too few papers, too few limited-access papers,
   unverified metadata, missing defect categories, and too few controls.

## Copyright

Commit identifiers and annotations only. Paper PDFs go in `pdfs/`, which is git-ignored.
Short verbatim quotes in `findings.yaml` are used as evidence anchors.
