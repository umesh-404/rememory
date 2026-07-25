"""Seed the memory collection with rememory's own design decisions.

These are real memories about this system -- why the embedding model was
chosen, why collections are partitioned by durability, what makes retrieval
good -- so a fresh install starts with a worked example of what a good memory
looks like, and so `search_memory` can answer questions about rememory itself.

Run from the repo root (uses the project environment):

    uv run scripts/seed_memories.py

Idempotent: refuses to run if any seeded title already exists.
"""

from indexer.config import load_config
from memory_mcp.memories import MemoryStore

SEEDS = [
    dict(
        project="rememory",
        memory_type="decision",
        title="Embedding model: qwen3-embedding:0.6b over nomic-embed-text, chosen by local benchmark",
        content=(
            "Benchmarked locally with a gold set of developer questions vs code/docs "
            "passages sharing no vocabulary: qwen3 hit Recall@1 72.7% / MRR 0.836 vs "
            "nomic's 54.5% / 0.685. Qwen is ~3x slower but batching at 32 recovers it "
            "(~52 chunks/s at the knee of the curve); query latency ~60ms is "
            "imperceptible. 1024 dims, cosine. Both models are ASYMMETRIC: qwen takes "
            "an instruction prefix on queries and none on documents -- omitting "
            "prefixes degrades retrieval silently, so they live in "
            "config/embedding.yaml as part of the model definition. Changing the "
            "model requires a full re-index (vectors from different models are "
            "incomparable and mixing them fails silently); "
            "scripts/benchmark_embeddings.py makes re-evaluating a future model a "
            "one-command job."
        ),
        tags=["embedding", "qwen", "benchmark", "ollama"],
    ),
    dict(
        project="rememory",
        memory_type="decision",
        title="Collections partitioned by durability (code/docs/memory), not by project",
        content=(
            "Three Qdrant collections -- code and docs (derived, rebuildable) and "
            "memory (authored, irreplaceable) -- with project as an indexed tenant "
            "payload field (is_tenant=true), instead of one collection per project. "
            "WHY: 're-index project X' is run constantly; if authored memory shared a "
            "collection with derived data, one careless wipe would destroy decisions "
            "that exist nowhere else. The split makes that structurally impossible "
            "(tooling refuses to bulk-delete memory). Bonus: cross-project search is "
            "one query, and Qdrant's own multitenancy guidance prefers few "
            "collections with tenant fields. Trade-off accepted: isolation relies on "
            "filters, so code/docs search tools take project as a MANDATORY "
            "argument, and tenant isolation is covered by tests/test_roundtrip.py."
        ),
        tags=["qdrant", "collections", "durability", "multitenancy"],
    ),
    dict(
        project="rememory",
        memory_type="implementation",
        title="Retrieval quality levers: symbol chunking, contextual headers, hybrid RRF, reranking",
        content=(
            "Four things make search good and all are easy to break. 1) Chunking is "
            "by SYMBOL (tree-sitter) not character windows -- one function/class/"
            "section per vector; tsx is a separate grammar from typescript and needs "
            "its own DEFINITION_NODES entry. 2) The EMBEDDED text is prefixed with a "
            "'path :: symbol' breadcrumb supplying vocabulary the bare code lacks, "
            "but the STORED payload is verbatim file content -- and because the path "
            "is baked into vectors, renames must re-embed, never copy vectors. "
            "3) Search is hybrid: dense + sparse CRC32 term-frequency vectors fused "
            "server-side with RRF (Qdrant modifier=idf does the IDF half); the "
            "sparse query builder must be the same imported function used at index "
            "time or lexical matching silently drifts. 4) The top candidates are "
            "reranked by a local cross-encoder (Qwen3-Reranker via Ollama raw-mode "
            "yes/no logprobs) -- measured on the live eval set this lifted Recall@1 "
            "from 50% to 92%."
        ),
        tags=["chunking", "tree-sitter", "hybrid-search", "reranking"],
    ),
]


def main() -> None:
    cfg = load_config()
    store = MemoryStore(cfg)

    existing_titles = {
        m["title"] for m in store.list_memories(limit=500, include_superseded=True)
    }
    clashes = [s["title"] for s in SEEDS if s["title"] in existing_titles]
    if clashes:
        raise SystemExit(f"Refusing to duplicate already-seeded memories: {clashes}")

    for seed in SEEDS:
        # One rejected seed (e.g. a future edit trips the near-duplicate
        # guard) must not traceback-crash setup -- report and continue.
        try:
            m = store.store(**seed)
        except Exception as exc:
            print(f"  SKIPPED  {seed['title'][:60]} -- {exc}")
            continue
        print(f"  stored [{m.memory_type:14}] {m.project}: {m.title[:70]}")

    print(f"\n{len(SEEDS)} memories seeded.")


if __name__ == "__main__":
    main()
