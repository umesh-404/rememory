"""Sparse (term-frequency) vectors for hybrid search.

Why we need these at all
------------------------
Dense embeddings capture meaning but blur exact strings. Ask for
`UserRepository` and a dense model returns things that are *about* user data
access -- often not the class itself. Code search needs both: semantics for
"how do we refresh tokens", and lexical precision for "find UserRepository".

Why we build them by hand rather than adding a BM25 library
-----------------------------------------------------------
Qdrant's `modifier: idf` (set on the `lexical` vector in collections.yaml) makes
the SERVER compute inverse-document-frequency across the collection at query
time. That is the hard half of BM25, and the half that needs corpus-wide
statistics we do not have while streaming files one at a time. All the client
must supply is term frequency per chunk. That is a dozen lines of standard
library, so pulling in fastembed (and its model downloads) would buy nothing.

The hashing trick
-----------------
Qdrant sparse vectors are (uint32 index -> float weight) pairs, so tokens must
become integers. We use CRC32 rather than Python's built-in `hash()`, because
`hash()` on strings is randomly salted per process: identical text would embed
to different sparse ids on every run, so nothing would ever match. Determinism
across runs and machines is a hard requirement here, not a nicety.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

from .config import SparseConfig

# Split on anything that is not alphanumeric or underscore.
TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9_]+")
# camelCase / PascalCase boundaries, including acronym runs like `HTTPServer`.
CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Words so common in code that they carry no discriminating signal.
STOPWORDS = frozenset(
    """
    the a an and or if is are was were be been being to of in on at for with by from as
    this that these those it its not no yes do does did done have has had will would can
    could should may might must return returns import from export default const let var
    function def class self this new public private static void true false null none
    """.split()  # noqa: SIM905 -- multiline string reads better than a 60-item literal
)


def build_sparse_vector(text: str, cfg: SparseConfig) -> tuple[list[int], list[float]]:
    """Return (indices, values) for one chunk, ready for Qdrant."""
    counts: Counter[str] = Counter()

    for raw in TOKEN_SPLIT.split(text):
        if not raw:
            continue
        lowered = raw.lower()
        if len(lowered) < cfg.min_token_len or lowered in STOPWORDS:
            continue

        # Keep the whole identifier: exact-symbol search depends on it.
        counts[lowered] += 1

        if cfg.split_identifiers:
            # ...and also its parts, so "user repository" finds UserRepository.
            parts = [p for chunk in raw.split("_") for p in CAMEL_SPLIT.split(chunk)]
            if len(parts) > 1:
                for part in parts:
                    lp = part.lower()
                    if len(lp) >= cfg.min_token_len and lp not in STOPWORDS:
                        # Sub-tokens weigh less than the full identifier: an
                        # exact match should always outrank a partial one.
                        counts[lp] += 0.5

    if not counts:
        return [], []

    # Cap the vocabulary per chunk. A long file otherwise produces a huge sparse
    # vector dominated by incidental words; the most frequent terms are the
    # ones carrying the topic.
    top = counts.most_common(cfg.max_tokens_per_chunk)

    indices: list[int] = []
    values: list[float] = []
    seen: set[int] = set()
    for token, count in top:
        # Mask to 31 bits: Qdrant wants uint32, and this avoids any sign
        # ambiguity across client versions.
        idx = zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF
        if idx in seen:  # hash collision -- merge rather than emit a duplicate
            values[indices.index(idx)] += float(count)
            continue
        seen.add(idx)
        indices.append(idx)
        values.append(float(count))

    return indices, values
