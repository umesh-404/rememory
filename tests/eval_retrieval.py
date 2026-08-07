"""End-to-end retrieval evaluation against the LIVE pipeline.

The Phase 2 benchmark measured the raw embedding model. This measures what
Claude actually experiences: discovery -> chunking -> hybrid RRF -> reranking,
over a real indexed corpus, scored against the golden set in
config/eval.yaml. Runs the same queries twice -- with and without the
reranker -- so the second stage has to prove its keep with numbers.

The corpus is this repo itself, indexed EPHEMERALLY under the reserved
project name "rememory-eval". rememory deliberately does not register or
index its own source (it would pollute user search results), and the MCP
tools validate `project` against the registry -- so the eval corpus is
invisible to every tool, and the eval no longer depends on any particular
machine's registered projects. Indexing is incremental, so re-runs while
tuning cost seconds, not minutes. Remove the corpus with --cleanup.

Metrics (industry-standard minimum):
  Recall@1 / Recall@3  -- is the right file the top hit / in the top 3?
  MRR                  -- how far down does the right file sit on average?

    uv run tests/eval_retrieval.py [--cleanup]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

from indexer.config import Project, load_config
from memory_mcp.search import Searcher

ROOT = Path(__file__).resolve().parent.parent
EVAL_PROJECT = "rememory-eval"


def ensure_eval_index(cfg) -> None:
    """Index this repo under the reserved eval project (incremental)."""
    from indexer.embedder import Embedder
    from indexer.lockfile import index_lock
    from indexer.pipeline import Pipeline
    from indexer.store import Store

    proj = Project(name=EVAL_PROJECT, root=ROOT,
                   description="ephemeral corpus for the retrieval scoreboard")
    store = Store(cfg)
    store.verify()
    with index_lock() as acquired:
        if not acquired:
            raise SystemExit("another index/sync is running -- retry in a minute")
        with Embedder(cfg.embedding) as embedder:
            embedder.health()
            stats = Pipeline(cfg).index_project(proj, store, embedder, only_changed=True)
    print(f"eval corpus: {stats.files_indexed} files indexed, "
          f"{stats.files_skipped_unchanged} unchanged "
          f"(project '{EVAL_PROJECT}', hidden from MCP tools)")


def cleanup_eval_index(cfg) -> None:
    from indexer.store import Store

    store = Store(cfg)
    for coll in ("code", "docs"):
        store.delete_project(coll, EVAL_PROJECT)
    print(f"removed the '{EVAL_PROJECT}' corpus from code and docs")


def evaluate(searcher: Searcher, cases: list[dict], *, rerank: bool) -> dict:
    hits1 = hits3 = 0
    rr_sum = 0.0
    misses: list[str] = []
    t0 = time.perf_counter()

    for case in cases:
        results = searcher.search(
            case["collection"],
            case["query"],
            project=case["project"],
            limit=5,
            rerank=rerank,
        )
        rank = next(
            (i for i, r in enumerate(results, 1) if case["expect"] in r.source_path),
            None,
        )
        if rank == 1:
            hits1 += 1
        if rank is not None and rank <= 3:
            hits3 += 1
        rr_sum += (1 / rank) if rank else 0.0
        if rank != 1:
            got = results[0].source_path if results else "(nothing)"
            misses.append(f"    rank {rank or '>5'}: {case['query'][:58]!r} -> {got}")

    return {
        "recall@1": hits1 / len(cases),
        "recall@3": hits3 / len(cases),
        "mrr": rr_sum / len(cases),
        "avg_latency_s": (time.perf_counter() - t0) / len(cases),
        "misses": misses,
    }


def main() -> int:
    cfg = load_config()
    if "--cleanup" in sys.argv[1:]:
        cleanup_eval_index(cfg)
        return 0
    golden = yaml.safe_load(
        (ROOT / "config" / "eval.yaml").read_text(encoding="utf-8")
    )["queries"]
    if any(case["project"] == EVAL_PROJECT for case in golden):
        ensure_eval_index(cfg)
    searcher = Searcher(cfg)

    print(f"{len(golden)} golden queries against the live index\n")
    rows = {}
    for label, rerank in (("RRF only", False), ("RRF + rerank", True)):
        rows[label] = evaluate(searcher, golden, rerank=rerank)

    print(f"{'pipeline':<14}{'R@1':>8}{'R@3':>8}{'MRR':>8}{'avg s':>8}")
    for label, m in rows.items():
        print(
            f"{label:<14}{m['recall@1']:>8.0%}{m['recall@3']:>8.0%}"
            f"{m['mrr']:>8.3f}{m['avg_latency_s']:>8.2f}"
        )
    for label, m in rows.items():
        if m["misses"]:
            print(f"\n  not-top-1 ({label}):")
            for line in m["misses"]:
                print(line)

    # The eval FAILS if reranked recall@3 drops below a floor -- so a future
    # change that quietly degrades retrieval breaks a test instead of a user.
    floor = 0.80
    ok = rows["RRF + rerank"]["recall@3"] >= floor
    print(f"\n{'PASS' if ok else 'FAIL'}: reranked recall@3 "
          f"{rows['RRF + rerank']['recall@3']:.0%} (floor {floor:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
