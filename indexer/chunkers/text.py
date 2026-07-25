"""Fallback chunker: fixed line windows with overlap.

Used for formats we have no structural parser for (YAML, JSON, .env.example,
Terraform, Vue/Svelte single-file components). It is the least clever chunker
and the most important one to get right, because everything falls back to it
when parsing fails -- a syntax error in a file must degrade retrieval slightly,
never lose the file entirely.

Overlap exists so a construct sitting exactly on a boundary still appears whole
in one of the two chunks.
"""

from __future__ import annotations

from . import Chunk


def chunk_text(
    content: str,
    *,
    max_chars: int,
    min_chars: int,
    overlap_lines: int,
    symbol_type: str | None = None,
    symbol_name: str | None = None,
    start_line_offset: int = 0,
) -> list[Chunk]:
    lines = content.splitlines()
    if not lines:
        return []

    # Fits whole: one chunk, no splitting. The common case for config files.
    if len(content) <= max_chars:
        return [
            Chunk(
                content=content,
                start_line=start_line_offset + 1,
                end_line=start_line_offset + len(lines),
                symbol_type=symbol_type,
                symbol_name=symbol_name,
            )
        ]

    chunks: list[Chunk] = []
    i = 0
    while i < len(lines):
        window: list[str] = []
        size = 0
        j = i
        while j < len(lines) and size + len(lines[j]) + 1 <= max_chars:
            window.append(lines[j])
            size += len(lines[j]) + 1
            j += 1

        # A single line longer than max_chars (minified leftovers, a giant
        # string literal). Take it alone and hard-truncate, rather than looping
        # forever on a window that can never fit.
        if not window:
            window = [lines[i][:max_chars]]
            j = i + 1

        text = "\n".join(window)
        if len(text) >= min_chars or not chunks:
            chunks.append(
                Chunk(
                    content=text,
                    start_line=start_line_offset + i + 1,
                    end_line=start_line_offset + j,
                    symbol_type=symbol_type,
                    symbol_name=symbol_name,
                )
            )
        elif chunks:
            # Too small to stand alone: fold the remainder into the previous
            # chunk rather than storing a fragment with no signal.
            chunks[-1].content += "\n" + text
            chunks[-1].end_line = start_line_offset + j

        if j >= len(lines):
            break
        # Step back by the overlap, but always make forward progress.
        i = max(j - overlap_lines, i + 1)

    return chunks
