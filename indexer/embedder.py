"""Batched embedding via Ollama.

Batching is not an optimisation detail here -- it is the difference between a
usable system and an unusable one. Measured on this machine in Phase 2:

    batch=1  -> 11 chunks/s      batch=32 -> 52 chunks/s

The model is fully on the GPU either way; the cost is per-request round-trip.
Batch size comes from config/embedding.yaml (32, the measured knee).

Two correctness details this module owns:

* The DOCUMENT prefix is applied here, in one place. These models are
  asymmetric -- documents and queries are embedded with different prompts --
  and getting it wrong degrades retrieval SILENTLY. Callers cannot forget it
  because callers never see it.
* Over-long text is truncated before sending. Ollama silently drops anything
  past the context window, so an un-truncated 10,000-token chunk would embed
  only its beginning while appearing to have worked.
"""

from __future__ import annotations

import time

import httpx

from .config import EmbeddingConfig


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, cfg: EmbeddingConfig) -> None:
        self.cfg = cfg
        self._client = httpx.Client(timeout=cfg.timeout)
        # Conservative chars-per-token estimate. Code is denser than prose
        # (~3.5 chars/token); 3.0 keeps a safety margin, and the cost of being
        # wrong in this direction is merely a slightly shorter chunk.
        self._max_chars = int(cfg.max_context_tokens * 3.0)

    def __enter__(self) -> Embedder:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def health(self) -> str:
        """Verify Ollama is up and the configured model is present.

        Checked once before indexing starts, so a missing model fails in the
        first second rather than after chunking 4,000 files.
        """
        try:
            resp = self._client.get(f"{self.cfg.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Ollama unreachable at {self.cfg.base_url}: {exc}") from exc

        names = {m["name"] for m in resp.json().get("models", [])}
        # Ollama reports "name:latest" even when pulled as bare "name".
        if self.cfg.name not in names and f"{self.cfg.name}:latest" not in names:
            raise EmbeddingError(
                f"Model '{self.cfg.name}' not found in Ollama.\n"
                f"Available: {', '.join(sorted(names)) or '(none)'}\n"
                f"Fix with:  ollama pull {self.cfg.name}"
            )
        return self.cfg.name

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed chunk texts, in batches, with the document prefix applied."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.cfg.batch_size):
            batch = texts[i : i + self.cfg.batch_size]
            prepared = [self.cfg.document_prefix + self._truncate(t) for t in batch]
            out.extend(self._call(prepared))
        return out

    # Small process-lifetime cache for query embeddings. Claude frequently
    # re-issues the same or near-identical query within a session (retry with
    # a different filter, expand=true on the second call); each repeat would
    # otherwise cost a full Ollama round trip. Bounded FIFO -- 256 queries x
    # 1024 floats is ~2 MB, and correctness is unaffected because the same
    # (model, prefix, text) always produces the same vector.
    _QUERY_CACHE_MAX = 256

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query, with the (different) query prefix applied."""
        cache = getattr(self, "_query_cache", None)
        if cache is None:
            cache = self._query_cache = {}
        if text in cache:
            return cache[text]
        vector = self._call([self.cfg.query_prefix + self._truncate(text)])[0]
        if len(cache) >= self._QUERY_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[text] = vector
        return vector

    def _truncate(self, text: str) -> str:
        return text if len(text) <= self._max_chars else text[: self._max_chars]

    def _call(self, inputs: list[str], attempt: int = 0) -> list[list[float]]:
        try:
            resp = self._client.post(
                f"{self.cfg.base_url}/api/embed",
                json={
                    "model": self.cfg.name,
                    "input": inputs,
                    # Keep the model resident so the next batch does not pay a
                    # reload. Set per-request rather than globally, so we do not
                    # pin the large chat models and exhaust 8 GB of VRAM.
                    "keep_alive": self.cfg.keep_alive,
                },
            )
            resp.raise_for_status()
            vectors = resp.json()["embeddings"]
        except (httpx.HTTPError, KeyError) as exc:
            # One retry: Ollama occasionally drops a request while swapping a
            # model onto the GPU. Failing a 20-minute index over that would be
            # needlessly brittle.
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                return self._call(inputs, attempt + 1)
            raise EmbeddingError(f"Embedding failed after {attempt + 1} attempts: {exc}") from exc

        if len(vectors) != len(inputs):
            raise EmbeddingError(f"Expected {len(inputs)} vectors, got {len(vectors)}")
        if vectors and len(vectors[0]) != self.cfg.dimensions:
            raise EmbeddingError(
                f"Model returned {len(vectors[0])}-d vectors but config says "
                f"{self.cfg.dimensions}. The collections were built for "
                f"{self.cfg.dimensions}; indexing now would corrupt them."
            )
        return vectors
