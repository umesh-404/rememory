"""Cross-encoder reranking -- the second retrieval stage.

Why this exists (the industry consensus, verified against Anthropic's
contextual-retrieval work and 2026 production-RAG guidance): bi-encoder
retrieval compares a query vector with document vectors computed in isolation,
so it can only measure similarity, not *answerhood*. A cross-encoder reads the
query and document together and judges directly whether this document answers
this query. It is dramatically more accurate and dramatically slower -- which
is why the pipeline is staged: hybrid RRF casts a wide cheap net (recall),
reranking orders the top few candidates precisely (precision).

How scoring works here, since Ollama has no rerank endpoint:
Qwen3-Reranker is a causal LM fine-tuned to answer a binary question with a
single token. We send the official prompt template in RAW mode with the
assistant primed by an empty <think> block, generate exactly ONE token, and
read Ollama's logprobs for that position. score = softmax over the 'yes' and
'no' logits. Measured discrimination on this machine: relevant 0.88, related-
but-not-answering 0.02, irrelevant 0.00.

Failure policy: reranking is an ENHANCEMENT. Any failure -- model missing,
timeout, budget exceeded -- logs to stderr and returns the first-stage order.
A search must never fail because its second stage did.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time

import httpx

from indexer.config import Config


class Reranker:
    def __init__(self, config: Config) -> None:
        raw = config.reranker
        self.enabled: bool = bool(raw.get("enabled"))
        self.model: str = raw.get("model", "")
        self.candidates: int = int(raw.get("candidates", 10))
        self.concurrency: int = int(raw.get("concurrency", 4))
        self.keep_alive: str = raw.get("keep_alive", "30m")
        self.timeout: float = float(raw.get("timeout_seconds", 60))
        self.max_batch_seconds: float = float(raw.get("max_batch_seconds", 12))
        self.instruct: str = raw.get("instruct", "").strip()
        self.base_url: str = config.embedding.base_url
        # After a hard failure, disable for this process lifetime: a missing
        # model would otherwise add a timeout to EVERY search.
        self._dead = False

    # ------------------------------------------------------------------ score
    def _prompt(self, query: str, document: str) -> str:
        return (
            "<|im_start|>system\nJudge whether the Document meets the requirements "
            'based on the Query and the Instruct provided. Note that the answer can '
            'only be "yes" or "no".<|im_end|>\n'
            f"<|im_start|>user\n<Instruct>: {self.instruct}\n"
            f"<Query>: {query}\n<Document>: {document}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )

    async def _score_one(self, client: httpx.AsyncClient, query: str, doc: str) -> float | None:
        resp = await client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": self._prompt(query, doc),
                "raw": True,
                "stream": False,
                "options": {"temperature": 0, "num_predict": 1, "num_ctx": 8192},
                "logprobs": True,
                "top_logprobs": 20,
                "keep_alive": self.keep_alive,
            },
        )
        resp.raise_for_status()
        logprobs = resp.json().get("logprobs") or []
        if not logprobs:
            return None
        yes = no = None
        for cand in logprobs[0].get("top_logprobs", []):
            token = cand["token"].strip().lower()
            if token == "yes" and yes is None:
                yes = cand["logprob"]
            elif token == "no" and no is None:
                no = cand["logprob"]
        if yes is None and no is None:
            return None
        e_yes = math.exp(yes) if yes is not None else 0.0
        e_no = math.exp(no) if no is not None else 0.0
        return e_yes / (e_yes + e_no) if (e_yes + e_no) else 0.0

    async def _score_batch(self, query: str, docs: list[str]) -> list[float | None]:
        sem = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(timeout=self.timeout) as client:

            async def bounded(doc: str) -> float | None:
                async with sem:
                    return await self._score_one(client, query, doc)

            return list(
                await asyncio.gather(*(bounded(d) for d in docs), return_exceptions=False)
            )

    # ------------------------------------------------------------------ rerank
    def rerank(self, query: str, results: list, pool_size: int | None = None) -> list:
        """Reorder SearchResults by cross-encoder score; annotate each result.

        Takes and returns the search layer's SearchResult objects. On any
        failure the input order (RRF) is returned untouched.

        pool_size widens the scored window beyond `candidates` when the caller
        will return more than `candidates` results -- otherwise the head of
        the response would mix scored and unscored entries, and the tool
        docstrings tell Claude to trust the scores.
        """
        if not self.enabled or self._dead or len(results) < 2:
            return results

        window = max(self.candidates, pool_size or 0)
        pool = results[:window]
        rest = results[window:]
        started = time.perf_counter()

        def run_batch() -> list[float | None]:
            return asyncio.run(
                asyncio.wait_for(
                    self._score_batch(query, [r.content for r in pool]),
                    timeout=self.max_batch_seconds,
                )
            )

        try:
            # asyncio.run() explodes if this thread already has a running
            # loop (an async caller). FastMCP's worker threads don't, but any
            # async embedding of this code would silently lose reranking --
            # so detect the case and run the batch in its own thread+loop.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                scores = run_batch()
            else:
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool_ex:
                    scores = pool_ex.submit(run_batch).result(
                        timeout=self.max_batch_seconds + 5
                    )
        except Exception as exc:
            print(f"rerank failed ({type(exc).__name__}: {exc}); using RRF order",
                  file=sys.stderr)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                self._dead = True  # model not pulled -- stop trying this process
                print(f"reranker disabled for this session. Fix: ollama pull {self.model}",
                      file=sys.stderr)
            return results

        elapsed = time.perf_counter() - started
        scored = []
        for r, s in zip(pool, scores, strict=True):
            if s is not None:
                r.extra["rerank"] = round(s, 4)
            # A None score (no yes/no token surfaced) keeps its RRF position
            # value low but present, so it sorts below anything scored.
            scored.append((s if s is not None else -1.0, r))
        scored.sort(key=lambda t: t[0], reverse=True)

        print(f"reranked {len(pool)} candidates in {elapsed:.1f}s", file=sys.stderr)
        return [r for _, r in scored] + rest
