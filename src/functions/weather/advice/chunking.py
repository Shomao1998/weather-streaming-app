"""Splitting a guidance document into retrievable passages.

Not a fixed character window. These documents are lists of actions under
headings, and a blind cut severs "Do not drive into flooded roadways" from the
sentence that says why — leaving a chunk that is retrievable but useless to
cite. Sections are the unit; only an over-long section is split further, and
then on paragraph boundaries with overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Wide enough to hold a heading plus its bullet list, small enough that four of
# them fit in a prompt without crowding out the weather facts.
TARGET_CHARS = 700
MAX_CHARS = 1000
OVERLAP_CHARS = 120
MIN_CHARS = 80

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Section:
    heading: str
    text: str


def split_sections(markdown: str) -> list[Section]:
    """Split on headings, keeping each heading with the text beneath it."""
    sections: list[Section] = []
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(Section(heading=heading, text=body))

    for line in markdown.splitlines():
        match = HEADING_RE.match(line.strip())
        if match:
            flush()
            buffer = []
            heading = match.group(2).strip()
            continue
        buffer.append(line)
    flush()
    return sections


def _split_long(text: str) -> list[str]:
    """Break an over-long section on paragraph boundaries, with overlap.

    Overlap exists so a passage that begins mid-list still carries the line
    before it; without it the first bullet of every continuation chunk loses
    its context.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    parts: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= TARGET_CHARS or not current:
            current = candidate
            continue
        parts.append(current)
        tail = current[-OVERLAP_CHARS:]
        # Resume at a line boundary so the overlap is a whole bullet, not a
        # fragment of one.
        if "\n" in tail:
            tail = tail[tail.index("\n") + 1 :]
        current = f"{tail}\n\n{paragraph}".strip()

    if current:
        parts.append(current)

    # A paragraph longer than the hard cap is rare in this corpus, but must
    # still be bounded rather than silently oversized.
    bounded: list[str] = []
    for part in parts:
        while len(part) > MAX_CHARS:
            cut = part.rfind("\n", 0, MAX_CHARS)
            cut = cut if cut > MIN_CHARS else MAX_CHARS
            bounded.append(part[:cut].strip())
            part = part[max(cut - OVERLAP_CHARS, 0) :].strip()
        if part:
            bounded.append(part)
    return bounded


def chunk_document(markdown: str) -> list[Section]:
    """Sections, further split only where a section is too long to be useful."""
    chunks: list[Section] = []
    for section in split_sections(markdown):
        body = section.text.strip()
        if not body:
            continue
        if len(body) <= MAX_CHARS:
            if len(body) >= MIN_CHARS or not chunks:
                chunks.append(section)
            else:
                # A stub section (a one-line preamble) is merged into the
                # previous chunk rather than indexed as its own passage.
                previous = chunks[-1]
                chunks[-1] = Section(previous.heading, f"{previous.text}\n\n{body}")
            continue
        chunks.extend(Section(section.heading, part) for part in _split_long(body))
    return chunks
