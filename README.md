# CiBud — Research Companion

**Evidence-grounded related-work drafting and citation validation.**

CiBud takes a set of papers (PDFs, DOIs/URLs, or a BibTeX/RIS bibliography) and a short description of your own research. It:

- works out how each paper relates to your work, with reasons and evidence rather than a bare score
- drafts a related-work section organized by theme
- manages in-text citations and the bibliography in any CSL style, switchable at any time
- keeps every factual sentence linked to the source passage that supports it, and flags sentences that aren't supported

> **Status:** Milestone 0 (foundations) in progress. The backend skeleton, core data schemas, and evaluation tooling exist; no pipeline features yet. The full system design is in [`docs/research-companion-design-doc.pdf`](docs/research-companion-design-doc.pdf) (Draft v0.2), and the build plan is in [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Why

General-purpose LLMs write fluent related-work text but invent or stretch claims. Reference managers format citations but don't help with synthesis. CiBud does the synthesis while making every claim checkable.

## Design principles

**The LLM proposes; the evidence system verifies.**

- **Deterministic where there is a right answer.** Metadata resolution, citation formatting, deduplication, and export are ordinary code. No LLM ever formats a reference.
- **Citations are document nodes, not text.** The draft is a structured tree in which each citation points to a stable reference ID. Text like "(Chen et al., 2021)" or "[3]" is rendered only for display or export, so switching style is just a re-render.
- **Validation is a function over any text.** It checks generated prose and your own writing the same way, and keeps working after edits.
- **Honest uncertainty.** "Unable to verify" is a normal outcome. A claim that rests on an abstract-only or paywalled source is never shown as verified.
- **Provenance on every fact.** Every extracted field, evidence passage, and verdict records its source, location, model, and prompt version.
- **Human approval gates.** Only papers you approve are used for drafting, and a draft is only marked verified after your review.

## How it works

1. **Create a project** with a research profile: problem, method, data, contribution, and how your work differs from prior work.
2. **Import sources.** Each paper is processed independently in the background.
3. **Review the library.** Check extracted metadata, evidence level (full text, abstract only, metadata only), and any issues.
4. **Review relevance.** Papers are grouped as directly related, methodological, data/domain, or contrasting. Approve, reject, or recategorize them.
5. **Approve an outline**, then draft from approved papers only.
6. **Validate and revise.** Each sentence shows a status (verified, partially supported, unsupported, conflicting, unable to verify, stale). The supporting passage is one click away, and edits trigger revalidation automatically.
7. **Choose a style and export** to Markdown (LaTeX and DOCX are planned).

## Architecture

```text
Application          Next.js UI · Workflow API · Tiptap editor · Pandoc export
Research processing  GROBID · Metadata resolver (Crossref/OpenAlex) · Dedup · Hybrid retrieval
AI orchestration     Paper analysis · Relevance & synthesis · Constrained writer · Claim auditor
Integrity & storage  citeproc/CSL · Reference validator · PostgreSQL + pgvector · Object store + audit log
```

The system is a deterministic workflow with specialized LLM steps, not a set of autonomous agents. Every LLM step returns a schema-validated (Pydantic) object. The writer's output schema requires every sentence to list the reference and evidence IDs it relies on, so a hallucinated citation fails validation.

## Planned tech stack

| Component | Choice |
| --- | --- |
| Frontend | Next.js, TypeScript, React, Tiptap, PDF.js |
| Backend | Python, FastAPI, Pydantic |
| Database & retrieval | PostgreSQL, pgvector, Postgres full-text search |
| PDF extraction | GROBID (OCR fallback for scanned PDFs) |
| Metadata | Crossref, OpenAlex (Semantic Scholar optional) |
| Citations & export | citeproc + CSL styles, Pandoc `--citeproc` |
| Jobs | Durable queue (Postgres-backed, or Celery/RQ) |
| Testing | pytest, fixtures, seeded-defect benchmark |

## Roadmap

| Step | Outcome |
| --- | --- |
| **1A — Library + validator** | Check citations in an existing draft against the actual papers |
| **1B — Editor + rendering** | Write and cite in-app; switch citation styles freely |
| **1C — Relevance + drafting** | End-to-end related-work drafting |
| **Phase 2** | Paper discovery, LaTeX/DOCX export, version history |
| **Phase 3** | Cross-project library, reference-manager import/export, collaboration, local models |

Details, exit criteria, and open decisions are in [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Repository layout

```text
backend/   Python package: FastAPI API, data schemas, database + migrations,
           job queue, LLM interface, eval tools
eval/      evaluation corpora: annotation templates and guide
infra/     docker-compose for Postgres + pgvector and GROBID
docs/      design doc and roadmap
```

The Next.js frontend (`web/`) will be added when the first UI work starts in Milestone 1A.

## Development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
# Services. GROBID uses the CRF-only image (~0.5 GB, native on Apple Silicon);
# see infra/docker-compose.yml to switch to the larger deep-learning image.
# Postgres listens on host port 5433 (override with CIBUD_PG_PORT) and has two
# databases: cibud (development) and cibud_test (used by the test suite).
docker compose -f infra/docker-compose.yml up -d

# Backend
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head                   # apply database migrations
uv run uvicorn cibud.api.main:app --reload   # API docs: http://localhost:8000/docs
uv run cibud-worker                           # background jobs (PDF extraction), in a second terminal

# Checks (CI runs the same commands)
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest          # database tests are skipped if Postgres isn't running
uv run alembic check   # models and migrations are in sync
```

After changing a table in `src/cibud/db/tables.py`, generate a migration with
`uv run alembic revision --autogenerate -m "<what changed>"` and review it before committing.

Try ingestion with a local PDF (PDFs in the repo root are git-ignored):

```bash
PROJECT=$(curl -s -X POST localhost:8000/projects -H 'content-type: application/json' \
  -d '{"name":"Demo","profile":{"problem":"...","method":"...","data":"...","contribution":"..."}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/projects/$PROJECT/papers -F file=@../paper.pdf
curl -s localhost:8000/projects/$PROJECT/papers   # state moves imported → extracting → metadata_review
```

To build an evaluation corpus, see [`eval/README.md`](eval/README.md).

## Documentation

- [`docs/research-companion-design-doc.pdf`](docs/research-companion-design-doc.pdf): architecture, data model, validation pipeline, UI wireframes, evaluation plan, risks
- [`docs/ROADMAP.md`](docs/ROADMAP.md): milestones, deliverables, and exit criteria
- [`eval/README.md`](eval/README.md): how to build and validate an evaluation corpus

## License

Licensed under the [Apache License 2.0](LICENSE).
