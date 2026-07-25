"""Heading-aware chunking for Markdown and prose.

Documentation has structure that maps almost perfectly onto retrieval: a
section under a heading is, by construction, one topic. So we split on headings
rather than on length, and carry the full heading trail
("Deployment > Docker > Volumes") on every chunk.

That trail matters more than it looks. A section titled "Rollback" reads as
meaningless out of context; "Deployment > Incidents > Rollback" is searchable.
The trail is prepended to the EMBEDDED text and stored in the `heading_path`
payload field, but is not baked into the stored content.

Fenced code blocks are tracked so that a `#` inside a shell snippet is never
mistaken for a heading -- a classic and very silent corruption of markdown
parsing.
"""

from __future__ import annotations

import re

from . import Chunk
from .text import chunk_text

ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
# Setext: a line underlined with === or --- is an H1/H2.
SETEXT = re.compile(r"^\s*(=+|-{3,})\s*$")


class DocsChunker:
    def __init__(self, *, max_chars: int, min_chars: int, overlap_lines: int) -> None:
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.overlap_lines = overlap_lines

    def chunk(self, source: str) -> list[Chunk]:
        lines = source.splitlines()
        if not lines:
            return []

        sections = self._split_sections(lines)
        chunks: list[Chunk] = []

        for start, end, trail in sections:
            text = "\n".join(lines[start:end]).strip("\n")
            if not text.strip():
                continue
            heading_path = " > ".join(trail) if trail else None

            if len(text) <= self.max_chars:
                if len(text.strip()) < self.min_chars and chunks:
                    # A stub section ("## TODO"). Fold it into the previous
                    # chunk rather than storing an near-empty vector.
                    chunks[-1].content += "\n\n" + text
                    chunks[-1].end_line = end
                    continue
                chunks.append(
                    Chunk(
                        content=text,
                        start_line=start + 1,
                        end_line=end,
                        symbol_type="section",
                        symbol_name=trail[-1] if trail else None,
                        heading_path=heading_path,
                    )
                )
            else:
                # A long section: split it, but every piece keeps the heading
                # trail so no fragment loses its topic.
                for part in chunk_text(
                    text,
                    max_chars=self.max_chars,
                    min_chars=self.min_chars,
                    overlap_lines=self.overlap_lines,
                    symbol_type="section",
                    symbol_name=trail[-1] if trail else None,
                    start_line_offset=start,
                ):
                    part.heading_path = heading_path
                    chunks.append(part)

        return chunks

    def _split_sections(self, lines: list[str]) -> list[tuple[int, int, list[str]]]:
        """Yield (start, end, heading_trail) spans, respecting code fences."""
        boundaries: list[tuple[int, int, str]] = []  # (line_index, level, title)
        in_fence = False
        fence_marker = ""

        for i, line in enumerate(lines):
            fence = FENCE.match(line)
            if fence:
                marker = fence.group(1)
                if not in_fence:
                    in_fence, fence_marker = True, marker
                elif marker == fence_marker:
                    in_fence = False
                continue
            if in_fence:
                continue  # a '#' in a bash block is a comment, not a heading

            m = ATX_HEADING.match(line)
            if m:
                boundaries.append((i, len(m.group(1)), m.group(2).strip()))
                continue

            # Setext heading: the underline belongs to the line above.
            if i > 0 and SETEXT.match(line) and lines[i - 1].strip() and not ATX_HEADING.match(lines[i - 1]):  # noqa: E501
                level = 1 if line.strip().startswith("=") else 2
                boundaries.append((i - 1, level, lines[i - 1].strip()))

        if not boundaries:
            return [(0, len(lines), [])]

        sections: list[tuple[int, int, list[str]]] = []

        # Preamble: content before the first heading (often the real summary).
        if boundaries[0][0] > 0:
            sections.append((0, boundaries[0][0], []))

        trail: list[tuple[int, str]] = []  # (level, title)
        for idx, (line_no, level, title) in enumerate(boundaries):
            # Pop headings at the same or deeper level to rebuild the trail.
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, title))
            end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
            sections.append((line_no, end, [t for _, t in trail]))

        return sections
