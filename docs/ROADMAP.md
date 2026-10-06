# Roadmap

This roadmap turns the phases in the [design doc](research-companion-design-doc.pdf) (§15) into milestones with deliverables and exit criteria. Section references (§) point to the design doc.

**Sequencing rule:** the system should never generate text it cannot check. Validation is built before drafting, and each step leaves a tool that is usable on its own.

| Milestone | Theme | Usable outcome |
| --- | --- | --- |
| [0 — Foundations](#milestone-0--foundations) | Evaluation set, repo scaffolding, core schemas | A benchmark to measure everything against |
| [1A — Library + validator](#milestone-1a--library--validator) | Ingestion, metadata, evidence, validation | Check citations in an existing draft against the actual papers |
| [1B — Editor + rendering](#milestone-1b--editor--rendering) | Structured editor, citation nodes, CSL | Write and cite in-app; switch styles freely |
| [1C — Relevance + drafting](#milestone-1c--relevance--drafting) | Research profile, relevance, outline, writer | End-to-end related-work drafting |
| [Phase 2](#phase-2--smarter-research-workflow) | Discovery, LaTeX/DOCX, history | Smarter research workflow |
| [Phase 3](#phase-3--full-research-workspace) | Cross-project, collaboration, local models | Full research workspace |

---

## Milestone 0 — Foundations

The design doc says to build the evaluation set *before* investing heavily in the pipeline (§16).

### Deliverables
- [ ] **Evaluation corpus:** 20–30 papers from one research topic, with hand-annotated:
  - relevance labels and relationship types (direct, methodological, data/domain, contrasting)
  - key findings and the passages that support them (page, section, span)
  - correct reference metadata (title, authors, year, venue, DOI)
- [ ] **Seeded-defect set** (§16):
  - metadata: wrong year, DOI pointing to a different paper, near-identical titles
  - claims: unsupported numeric claim, citation that supports only half a sentence, correlation stated as causation
  - comparisons: conflicting results, comparison across incompatible datasets or metrics
  - integrity: missing bibliography entry, orphaned reference after deletion, a paper that is topically similar but methodologically irrelevant
  - the same number of **valid** citations, to measure false alarms
- [x] **Annotation format and tooling:** YAML schemas, templates, `cibud-eval validate` (schema, cross-references, coverage warnings) and `cibud-eval prefill` (Crossref/arXiv). See [`eval/README.md`](../eval/README.md).
- [x] **Backend scaffolding:** Python + FastAPI package managed with uv, pytest, ruff, mypy (strict).
- [x] **Local services:** docker-compose with PostgreSQL + pgvector and GROBID.
- [x] **CI:** GitHub Actions running lint, format check, type check, tests, and template validation.
- [x] **Core schemas** (Pydantic, §6): `Project`, `ResearchProfile`, `Paper`, `Reference`, `EvidencePassage`, `RelevanceAssessment`, `Document`/citation nodes, `Claim`, `DraftParagraph`, `Job`.
- [x] **LLM provider interface:** versioned prompts, schema-validated output, provenance on every result, and a fake provider for tests.
- [ ] **Scoring script** that scores a pipeline run against a corpus. This needs pipeline output, so it is written alongside the first 1A checks.
- Deferred to Milestone 1A, when first needed: database tables and migrations, the durable job queue, the first concrete LLM provider, and the Next.js frontend.

### Exit criteria
- The evaluation corpus and defect set are checked in, with a script that scores a pipeline run against them.
- `docker compose up` (or similar) starts the database and GROBID locally.
- CI runs lint and tests.

### Decisions
- [x] **Claim hash** covers the sentence text *plus* the sorted set of cited reference IDs (§10.4). Implemented as `claim_hash()`.
- [x] **Sentence status** is computed by `sentence_verdict()`: the most severe claim verdict, ordered stale > unsupported > conflicting > partial > unable to verify > supported.
- [ ] Claim decomposition: stored at write time or derived at validation time? (§18 Q1) The current `Claim` schema stores claims; revisit in 1A.

---

## Milestone 1A — Library + validator

**Outcome:** a researcher pastes an existing draft with citations and finds out which citations are wrong and which claims their sources don't support.

### Deliverables
**Ingestion (§7)**
- [ ] PDF upload → GROBID → TEI parsing: header metadata, sections, reference list, passage coordinates
- [ ] OCR fallback for scanned PDFs, flagged as lower confidence
- [ ] DOI/URL import → Crossref/OpenAlex metadata; fetch full text only where legally accessible (open-access PDF, arXiv, PMC)
- [ ] BibTeX/RIS import → CSL-JSON, then filled in from Crossref
- [ ] Evidence levels (`full_text`, `abstract_only`, `metadata_only`); uploading a PDF later upgrades the level

**Metadata and deduplication (§5, §7.3)**
- [ ] Metadata resolver that records which source supplied each field
- [ ] Precedence rules (the publisher's DOI record beats the extracted header); unresolved conflicts go to `NeedsAttention`
- [ ] Deduplication by DOI, arXiv ID, title/author similarity, and preprint vs. published version

**Evidence retrieval (§5)**
- [ ] Chunk text by section and page, keeping page, section, char span, and bbox
- [ ] Hybrid search (pgvector + full-text) limited to a given set of papers

**Validation (§10)**
- [ ] Deterministic checks: reference existence, metadata consistency, retraction/correction notices, orphan and missing references
- [ ] Claim pipeline: split into sentences → decompose into atomic claims → retrieve passages from the cited papers only → auditor verdict → store the verdict with its supporting span, rationale, model, and prompt version
- [ ] Auditor independence: fresh context, neutral framing (§10.3)
- [ ] Uncited factual claims are flagged
- [ ] All six statuses: verified, partial, unsupported, conflicting, unable to verify, stale

**Workflow (§11)**
- [ ] Per-paper state machine: Imported → Extracting → MetadataReview → Analyzing → RelevanceReview → Approved/Excluded, plus `MetadataOnly` and `NeedsAttention`
- [ ] Idempotent jobs keyed by inputs hash; concurrent, retryable, resumable after a crash

**Minimal UI**
- [ ] Library view with metadata, evidence level, provenance, and issues
- [ ] Validate pasted text: per-sentence status, evidence passage, auditor rationale

### Exit criteria (measured against Milestone 0)
- Metadata accuracy, evidence-retrieval recall@k, and auditor precision/recall/false-alarm rate are all reported.
- No claim that rests on an abstract-only source is ever marked "verified".
- One failed paper never blocks the others.

---

## Milestone 1B — Editor + rendering

**Outcome:** the researcher writes and cites inside the app, and switching citation style never touches the document content.

### Deliverables
- [ ] Tiptap editor with an atomic inline `citation` node: `items[{ref, locator, label}]`, `mode` (§6.1)
- [ ] Citation picker (§13.5): searches the project library, multi-select, mode (parenthetical, narrative, suppress-author), locator, live preview, evidence level shown before inserting
- [ ] Picker as the single way to add, change, or remove citations (select a node and press Enter)
- [ ] CSL rendering with citeproc: APA 7 and IEEE first; numeric styles numbered by order of first appearance
- [ ] Removing a reference lists the sentences that cite it first, then turns those nodes into broken "reference removed" nodes; text is never deleted (§13.4)
- [ ] Incremental revalidation: changed sentences go stale and are re-queued; editing reference metadata re-runs only the deterministic checks (§10.4)
- [ ] Workspace (§13.3): library pane, editor with status gutter, underlines and tinted citations, evidence panel that follows the cursor, issue counts with `]`/`[` navigation
- [ ] Source inspector (§13.4): PDF.js viewer opened at the passage highlighted from GROBID coordinates; field provenance; every citing sentence
- [ ] Markdown export: document tree → Pandoc Markdown with `[@key, p. 4]` → `pandoc --citeproc`
- [ ] Stable citation keys

### Exit criteria
- Scripted edit sequences produce no missing, broken, or mismatched citations (§16).
- Switching APA 7 ↔ IEEE re-renders correctly with no change to the document JSON.
- In-editor rendering and Pandoc export produce the same citation text for the same style.

### Decisions to make here
- Choose citeproc-js (in-browser preview) vs. citeproc-py, and how to keep it consistent with Pandoc's citeproc at export.
- Revalidation granularity: sentence, paragraph, or section? (§18 Q3)
- How "Mark as opinion/synthesis" is stored and shown (§13.3, §18 Q2).

---

## Milestone 1C — Relevance + drafting

**Outcome:** end-to-end related-work drafting, where every generated sentence goes through the 1A validator.

### Deliverables
- [ ] Research profile (problem, method, data/population, contribution, how the work differs), versioned; relevance results cached per profile version
- [ ] Paper Analysis step: question, method, data, findings, limitations, evidence passages with locations
- [ ] Relevance assessment (§8): several relationship types per paper, level, reasons, evidence IDs, cautions, potential use
- [ ] Library and relevance review screen (§13.6): grouped by relationship, approve/exclude/recategorize, filter tabs, metadata-conflict banners
- [ ] Comparison table across approved papers (task, method, data, metrics, findings, limitations)
- [ ] Clustering into themes and an outline proposal; outline review screen (§13.7) with drag-to-reassign and coverage indicators
- [ ] Constrained writer: `DraftParagraph` / `DraftSentence` output; reference and evidence IDs checked in code against the approved set
- [ ] Draft lifecycle: Outline → Drafting → Validating → ResearcherReview → Approved → Exported (§11.2)
- [ ] Paragraph action bar: accept, rewrite with an instruction, reject, history
- [ ] Suggested fixes shown as diffs, never silent edits
- [ ] Draft one section only, or all sections
- [ ] Style and export screen (§13.8) with a pre-export check; verified export is blocked while issues are open, and a labeled draft export is always available

### Exit criteria
- Claim grounding: share of generated factual claims supported by their cited sources.
- Relevance precision@5 and agreement with the researcher's own judgments.
- Human ratings of synthesis quality: accuracy, comparison quality, coverage.
- Cost and time per paper and per section are reported.

### Decisions to make here
- How much of the comparison table the user can edit before drafting (§18 Q5).
- Auditor model choice, and whether an auditor from a different model family does measurably better (§18 Q4).

---

## Phase 2 — Smarter research workflow
- Paper discovery beyond uploaded papers
- Clustering and conflict detection across the library
- LaTeX (`.tex` + `.bib`) and DOCX export
- Version history for drafts, including validation state per version
- Validation that is aware of revisions

## Phase 3 — Full research workspace
- Library shared across projects
- Reference-manager import/export (Zotero and others; no live sync)
- Journal templates (LaTeX class or reference DOCX) alongside CSL styles
- Collaboration
- Local models for sensitive work
- Coverage reports

---

## Out of scope for Phase 1
From §2: autonomous web-wide literature discovery, fine-tuning or custom embeddings, multi-agent loops, live two-way sync with reference managers, and claims about the definitive research gap of an entire field.

## Risks to track
See §17. The ones most likely to affect this schedule:
- **Auditor accuracy:** gate 1C on the auditor meeting its precision/recall targets on the seeded-defect set.
- **PDF extraction quality:** tables, equations, and two-column layouts. Keep click-through to the original page for every passage.
- **Model cost:** cache analyses per paper and profile version, revalidate incrementally, and use smaller models for decomposition.
