"""Chunking: turning a file into pieces small enough to embed and coherent
enough to be worth retrieving.

Why chunking decides retrieval quality
--------------------------------------
A vector is a fixed-size summary. Embed an entire 800-line file and you get one
blurry average of everything it does -- it matches every query weakly and none
strongly. Chunk too finely and each piece loses the context that made it
meaningful. The unit we want is "one idea": a function, a class, a doc section.

That is why we split on SYNTAX rather than on character counts. A 500-character
window will happily cut a function in half, leaving a body with no signature
(so nobody can find it) and a signature with no body (so finding it tells you
nothing).
"""

from dataclasses import dataclass, field


@dataclass
class Chunk:
    """One embeddable unit, plus everything we know about where it came from."""

    content: str  # verbatim source text -- exactly what is on disk
    start_line: int  # 1-based, inclusive
    end_line: int
    symbol_type: str | None = None  # function | class | method | interface | ...
    symbol_name: str | None = None  # qualified where possible: "Class.method"
    heading_path: str | None = None  # docs: "Setup > Docker > Volumes"
    extra: dict = field(default_factory=dict)

    def embed_text(self, header: str | None) -> str:
        """The text actually sent to the embedding model.

        Optionally prefixed with a breadcrumb (file path, symbol name, heading
        trail). This is 'contextual retrieval': a bare code chunk often lacks
        the words a developer would search for -- `def allow(self, key)` says
        nothing about rate limiting, but the path
        `services/rate_limit.py :: LeakyBucket.allow` does.

        The header is deliberately NOT stored in the payload, so what Claude
        reads back is the real file content, unaltered.
        """
        return f"{header}\n\n{self.content}" if header else self.content


__all__ = ["Chunk"]
