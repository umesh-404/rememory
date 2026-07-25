"""Benchmark local Ollama embedding models for THIS system's actual workload.

Why this exists
---------------
Public leaderboards (MTEB) rank models on general text. Our workload is
specific: source code, API docs, and short architecture notes, queried in the
way a developer actually phrases a question ("how do we refresh expired
tokens?"), which rarely shares vocabulary with the answer. This script measures
what we care about on the machine we will actually run on:

  1. Vector dimensionality  -> pins the Qdrant collection schema in Phase 3.
  2. Indexing throughput    -> how long a full re-index will take.
  3. Query latency          -> felt directly by Claude on every search.
  4. Retrieval accuracy     -> Recall@1 and MRR on a hand-built gold set
                               where the right answer is known.

Dependencies: none. Standard library only, on purpose — the project's real
Python environment is not set up until Phase 4, and a benchmark that needs an
environment to decide how to build the environment is a circular dependency.

Run:  python scripts\\benchmark_embeddings.py
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexer.runtime import ollama_url  # noqa: E402

OLLAMA = ollama_url()

# --------------------------------------------------------------------------
# Model definitions.
#
# The `prefix` fields matter enormously and are the single most common way to
# use these models wrong. Both models were trained with *asymmetric* prompts:
# the query and the document are embedded differently, so that a short question
# lands near a long answer. Omit the prefixes and retrieval quality collapses,
# silently -- you still get vectors, they are just worse. We therefore treat
# the prefix as part of the model definition, not as a caller's choice.
# --------------------------------------------------------------------------
MODELS = {
    "nomic-embed-text": {
        # Nomic was trained with these literal string prefixes.
        "query_prefix": "search_query: ",
        "doc_prefix": "search_document: ",
    },
    "qwen3-embedding:0.6b": {
        # Qwen3-Embedding is instruction-tuned: the query carries a natural
        # language task description, the document carries nothing.
        "query_prefix": (
            "Instruct: Given a developer's question, retrieve the source code "
            "or documentation passage that answers it\nQuery: "
        ),
        "doc_prefix": "",
    },
}

# --------------------------------------------------------------------------
# Gold set. Each query names the ONE passage that genuinely answers it.
#
# These are deliberately written so the query and its answer share little or no
# vocabulary. Anything a plain keyword search could solve tells us nothing about
# embedding quality.
# --------------------------------------------------------------------------
CORPUS: dict[str, str] = {
    "auth_refresh": (
        "async def rotate_credentials(session: Session) -> Token:\n"
        "    if session.expires_at < utcnow():\n"
        "        new = await idp.exchange(session.refresh_token)\n"
        "        session.refresh_token = new.refresh_token\n"
        "    return session.access_token"
    ),
    "rate_limit": (
        "class LeakyBucket:\n"
        "    \"\"\"Throttles outbound calls to third-party ATS boards.\"\"\"\n"
        "    def allow(self, key: str) -> bool:\n"
        "        self._drain(key)\n"
        "        return self._level[key] < self.capacity"
    ),
    "db_migration": (
        "# ADR-014: Schema changes are forward-only.\n"
        "We never write down-migrations. Rolling back a deploy restores the\n"
        "previous application image but leaves the schema in place; every\n"
        "change must therefore be additive and backward compatible for one\n"
        "release cycle."
    ),
    "caching": (
        "def memoize_ttl(seconds: int):\n"
        "    \"\"\"Keeps computed results in process memory for a fixed window,\n"
        "    avoiding repeated expensive recomputation.\"\"\""
    ),
    "pagination": (
        "Cursor-based listing: responses carry an opaque `next` token derived\n"
        "from the sort key of the final row. Offsets are not used because rows\n"
        "inserted mid-scroll would cause records to be skipped or repeated."
    ),
    "deploy_rollback": (
        "RUNBOOK: If the release is bad, re-point the container tag at the\n"
        "previous digest and restart. Do not revert the database."
    ),
    "logging": (
        "logger = structlog.get_logger()\n"
        "logger.bind(request_id=rid).info('handled', duration_ms=dt)"
    ),
    "testing": (
        "Fixtures spin up a throwaway Postgres in a container per test session\n"
        "and roll back a transaction after each individual case."
    ),
    "vector_search": (
        "Nearest-neighbour lookup uses cosine distance over an HNSW graph, with\n"
        "payload filters applied during traversal rather than afterwards."
    ),
    "config_loading": (
        "class Settings(BaseSettings):\n"
        "    database_url: PostgresDsn\n"
        "    model_config = SettingsConfigDict(env_file='.env')"
    ),
    "error_handling": (
        "@app.exception_handler(DomainError)\n"
        "async def to_problem_json(request, exc):\n"
        "    return JSONResponse(status_code=exc.status, content=exc.as_problem())"
    ),
    "background_jobs": (
        "The sync runs on a timer inside the API process. There is no broker;\n"
        "a single worker loop claims rows with SELECT ... FOR UPDATE SKIP LOCKED."
    ),
}

QUERIES: list[tuple[str, str]] = [
    ("how do we get a new access token when the old one expires?", "auth_refresh"),
    ("stop us hammering an external API too fast", "rate_limit"),
    ("can we undo a database change after a bad release?", "db_migration"),
    ("avoid recomputing the same expensive result over and over", "caching"),
    ("why don't we use page numbers for listing results?", "pagination"),
    ("what do I do when a deploy goes wrong?", "deploy_rollback"),
    ("how are tests isolated from each other's data?", "testing"),
    ("how does the similarity lookup combine filtering with the index?", "vector_search"),
    ("where do environment variables get read into the app?", "config_loading"),
    ("what happens to a business rule violation before it reaches the client?", "error_handling"),
    ("do we need Redis or Celery for the periodic scraping?", "background_jobs"),
]


def embed(model: str, text: str) -> list[float]:
    """Call Ollama's native embeddings endpoint for a single string."""
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["embedding"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def benchmark(model: str, spec: dict[str, str]) -> dict:
    print(f"\n{'=' * 68}\n{model}\n{'=' * 68}")

    # Warm-up: the first call pays model load + VRAM transfer. Including that
    # in the timings would measure disk speed, not embedding speed.
    embed(model, "warm up")

    # ---- Indexing throughput -------------------------------------------
    doc_vecs: dict[str, list[float]] = {}
    t0 = time.perf_counter()
    for key, text in CORPUS.items():
        doc_vecs[key] = embed(model, spec["doc_prefix"] + text)
    index_elapsed = time.perf_counter() - t0

    dims = len(next(iter(doc_vecs.values())))
    docs_per_sec = len(CORPUS) / index_elapsed

    # ---- Query latency + retrieval accuracy ----------------------------
    latencies: list[float] = []
    reciprocal_ranks: list[float] = []
    hits_at_1 = 0
    misses: list[str] = []

    for question, gold in QUERIES:
        q0 = time.perf_counter()
        qv = embed(model, spec["query_prefix"] + question)
        latencies.append((time.perf_counter() - q0) * 1000)

        ranked = sorted(
            ((cosine(qv, v), k) for k, v in doc_vecs.items()), reverse=True
        )
        rank = next(i for i, (_, k) in enumerate(ranked, 1) if k == gold)
        reciprocal_ranks.append(1 / rank)
        if rank == 1:
            hits_at_1 += 1
        else:
            misses.append(f"    rank {rank}: {question!r} -> got {ranked[0][1]!r}, want {gold!r}")

    recall_at_1 = hits_at_1 / len(QUERIES)
    mrr = statistics.mean(reciprocal_ranks)

    print(f"  dimensions        : {dims}")
    print(f"  index throughput  : {docs_per_sec:6.1f} chunks/sec")
    print(f"  query latency p50 : {statistics.median(latencies):6.1f} ms")
    print(f"  query latency max : {max(latencies):6.1f} ms")
    print(f"  Recall@1          : {recall_at_1:6.1%}  ({hits_at_1}/{len(QUERIES)})")
    print(f"  MRR               : {mrr:6.3f}")
    if misses:
        print("  misses:")
        for m in misses:
            print(m)

    return {
        "model": model,
        "dims": dims,
        "docs_per_sec": docs_per_sec,
        "p50_ms": statistics.median(latencies),
        "recall_at_1": recall_at_1,
        "mrr": mrr,
    }


def main() -> None:
    try:
        urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5)
    except urllib.error.URLError as exc:
        raise SystemExit(f"Ollama is not reachable at {OLLAMA}: {exc}")

    results = [benchmark(m, spec) for m, spec in MODELS.items()]

    print(f"\n{'=' * 68}\nSUMMARY\n{'=' * 68}")
    print(f"{'model':<24}{'dims':>6}{'chunks/s':>11}{'p50 ms':>9}{'R@1':>8}{'MRR':>8}")
    for r in results:
        print(
            f"{r['model']:<24}{r['dims']:>6}{r['docs_per_sec']:>11.1f}"
            f"{r['p50_ms']:>9.1f}{r['recall_at_1']:>8.1%}{r['mrr']:>8.3f}"
        )


if __name__ == "__main__":
    main()
