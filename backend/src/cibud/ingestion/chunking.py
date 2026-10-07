"""Turn a parsed paper into a canonical plain text and evidence passages (design doc §5).

Passages follow paragraph boundaries and are split at sentence boundaries when a paragraph is
long, so a passage never starts or ends mid-sentence. Each passage keeps the section, page,
character span into the canonical text, and the PDF boxes of its sentences.
"""

from dataclasses import dataclass, field

from cibud.ingestion.tei import ParsedPaper, Sentence
from cibud.models.evidence import BoundingBox

MAX_PASSAGE_CHARS = 1200
SENTENCE_SEP = " "
PARAGRAPH_SEP = "\n\n"


@dataclass(frozen=True)
class PassageDraft:
    text: str
    section: str | None
    page: int | None
    char_span: tuple[int, int]
    bboxes: list[BoundingBox] = field(default_factory=list)


@dataclass
class ChunkedPaper:
    text: str
    passages: list[PassageDraft]


def _groups(sentences: list[Sentence], max_chars: int) -> list[list[Sentence]]:
    groups: list[list[Sentence]] = []
    current: list[Sentence] = []
    length = 0
    for sentence in sentences:
        added = len(sentence.text) + (len(SENTENCE_SEP) if current else 0)
        if current and length + added > max_chars:
            groups.append(current)
            current, length = [], 0
            added = len(sentence.text)
        current.append(sentence)
        length += added
    if current:
        groups.append(current)
    return groups


def chunk(paper: ParsedPaper, max_chars: int = MAX_PASSAGE_CHARS) -> ChunkedPaper:
    """Build the canonical text and its passages. Deterministic for a given TEI."""
    blocks: list[str] = []
    passages: list[PassageDraft] = []
    offset = 0

    def append(block: str) -> int:
        nonlocal offset
        if blocks:
            offset += len(PARAGRAPH_SEP)
        start = offset
        blocks.append(block)
        offset += len(block)
        return start

    for section in paper.sections:
        if section.label:
            append(section.label)
        for paragraph in section.paragraphs:
            for group in _groups(paragraph.sentences, max_chars):
                text = SENTENCE_SEP.join(s.text for s in group)
                start = append(text)
                passages.append(
                    PassageDraft(
                        text=text,
                        section=section.label,
                        page=next((s.page for s in group if s.page is not None), None),
                        char_span=(start, start + len(text)),
                        bboxes=[box for s in group for box in s.boxes],
                    )
                )

    return ChunkedPaper(text=PARAGRAPH_SEP.join(blocks), passages=passages)
