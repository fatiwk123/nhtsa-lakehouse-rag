"""Two chunking strategies, compared in the evaluation (phase 5).

- fixed:     sliding window of N words with overlap, ignoring structure.
- structure: packs paragraphs (then sentences when a paragraph is too long) up to a
             word budget, never crossing a section (recall field / PDF page) boundary.

Every chunk keeps its page range, so answers can cite "document, page X".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from rag.documents import Document, Section

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    strategy: str
    index: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading: str | None = None

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}#{self.strategy}#{self.index}"


def fixed_window(doc: Document, window: int, overlap: int) -> list[Chunk]:
    if overlap >= window:
        raise ValueError("overlap must be smaller than window")
    words: list[tuple[str, int | None]] = []
    for section in doc.sections:
        prefix = [(f"{section.heading}:", section.page)] if section.heading else []
        words.extend(prefix + [(w, section.page) for w in section.text.split()])
    chunks, start, step = [], 0, window - overlap
    while start < len(words):
        piece = words[start : start + window]
        pages = [p for _, p in piece if p is not None]
        chunks.append(
            Chunk(
                doc_id=doc.doc_id,
                strategy="fixed",
                index=len(chunks),
                text=" ".join(w for w, _ in piece),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
            )
        )
        if start + window >= len(words):
            break
        start += step
    return chunks


def _units(section: Section, max_words: int) -> list[str]:
    """Paragraphs, with over-long paragraphs split into sentences (and hard-split if needed)."""
    units = []
    for paragraph in re.split(r"\n\s*\n", section.text):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph.split()) <= max_words:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            words = sentence.split()
            for i in range(0, len(words), max_words):
                units.append(" ".join(words[i : i + max_words]))
    return units


def structure_aware(doc: Document, max_words: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in doc.sections:
        buffer: list[str] = []
        size = 0

        def flush() -> None:
            nonlocal buffer, size
            if buffer:
                body = "\n".join(buffer)
                text = f"{section.heading}: {body}" if section.heading else body
                chunks.append(
                    Chunk(
                        doc_id=doc.doc_id,
                        strategy="structure",
                        index=len(chunks),
                        text=text,
                        page_start=section.page,
                        page_end=section.page,
                        heading=section.heading,
                    )
                )
            buffer, size = [], 0

        for unit in _units(section, max_words):
            n = len(unit.split())
            if size + n > max_words:
                flush()
            buffer.append(unit)
            size += n
        flush()
    return chunks


def strategies(fixed_words: int, fixed_overlap: int, structure_words: int) -> dict[str, Callable[[Document], list[Chunk]]]:
    return {
        "fixed": lambda d: fixed_window(d, fixed_words, fixed_overlap),
        "structure": lambda d: structure_aware(d, structure_words),
    }
