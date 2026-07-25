# /// script
# requires-python = ">=3.12"
# dependencies = ["qdrant-client>=1.12", "pyyaml>=6.0", "httpx>=0.27"]
# ///
"""End-to-end smoke test for Phases 1-3.

Proves the whole chain actually works together, rather than each piece working
in isolation:

    Ollama embeds text -> vector has the size the collection expects
                       -> point upserts into Qdrant
                       -> semantic search finds it by meaning, not keywords
                       -> the `project` filter genuinely isolates tenants
                       -> cleanup leaves no residue

The tenant-isolation check matters most. The whole multi-project design rests
on payload filtering rather than physical separation (see config/collections.yaml),
so "does the filter actually exclude other projects?" is the assumption the
architecture is betting on. It gets tested, not assumed.

    uv run tests\\test_roundtrip.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import yaml
from qdrant_client import QdrantClient, models

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
COLLECTION = "code"  # derived + disposable, so a test can safely write here

# Two projects, so we can prove the filter separates them.
FIXTURES = [
    ("proj-alpha", "def rotate_credentials(session):\n    return idp.exchange(session.refresh_token)"),
    ("proj-alpha", "class LeakyBucket:\n    def allow(self, key): return self._level[key] < self.capacity"),
    ("proj-beta", "SELECT id, email FROM users WHERE deleted_at IS NULL ORDER BY created_at DESC"),
]
QUERY = "how do we obtain a fresh access token once the current one has expired?"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def embed(cfg: dict, text: str, *, is_query: bool) -> list[float]:
    m = cfg["model"]
    prefix = m["query_prefix"] if is_query else m["document_prefix"]
    resp = httpx.post(
        f"{cfg['provider']['base_url']}/api/embed",
        json={
            "model": m["name"],
            "input": prefix + text,
            "keep_alive": cfg["provider"]["keep_alive"],
        },
        timeout=cfg["provider"]["request_timeout_seconds"],
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def main() -> int:
    cfg = yaml.safe_load((CONFIG_DIR / "embedding.yaml").read_text(encoding="utf-8"))
    client = QdrantClient(url="http://127.0.0.1:6333", timeout=60)
    run_id = uuid.uuid4().hex[:8]  # tags this run's points so cleanup is exact

    print(f"\nPhase 1-3 round trip (run {run_id})\n")

    # --- 1. embedding dimensionality matches the collection ------------------
    vectors = [embed(cfg, text, is_query=False) for _, text in FIXTURES]
    declared = cfg["model"]["dimensions"]
    actual = len(vectors[0])
    check("embedding size matches config", actual == declared, f"{actual} == {declared}")

    info = client.get_collection(COLLECTION)
    coll_dim = info.config.params.vectors["dense"].size
    check("collection size matches embedding", coll_dim == actual, f"{coll_dim} == {actual}")

    # --- 2. upsert -----------------------------------------------------------
    ids = [str(uuid.uuid4()) for _ in FIXTURES]
    client.upsert(
        collection_name=COLLECTION,
        wait=True,
        points=[
            models.PointStruct(
                id=pid,
                vector={"dense": vec},
                payload={
                    "project": project,
                    "source_path": f"_test/{run_id}/{i}.py",
                    "content": text,
                    "content_hash": f"test-{run_id}",
                    "schema_version": cfg["schema_version"],
                    "language": "python",
                    "symbol_type": "function",
                    "_test_run": run_id,
                },
            )
            for i, (pid, vec, (project, text)) in enumerate(zip(ids, vectors, FIXTURES, strict=True))
        ],
    )
    check("upserted 3 points", client.get_collection(COLLECTION).points_count >= 3)

    # --- 3. semantic search finds the right chunk ----------------------------
    qvec = embed(cfg, QUERY, is_query=True)
    hits = client.query_points(
        collection_name=COLLECTION,
        query=qvec,
        using="dense",
        limit=1,
        query_filter=models.Filter(
            must=[models.FieldCondition(key="_test_run", match=models.MatchValue(value=run_id))]
        ),
    ).points
    top = hits[0].payload["content"] if hits else ""
    check(
        "semantic search returns the token-refresh code",
        "rotate_credentials" in top,
        f"score={hits[0].score:.3f}" if hits else "no hits",
    )

    # --- 4. tenant isolation -- the architecture's core assumption -----------
    scoped = client.query_points(
        collection_name=COLLECTION,
        query=qvec,
        using="dense",
        limit=10,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(key="_test_run", match=models.MatchValue(value=run_id)),
                models.FieldCondition(key="project", match=models.MatchValue(value="proj-beta")),
            ]
        ),
    ).points
    check(
        "project filter isolates tenants",
        len(scoped) == 1 and scoped[0].payload["project"] == "proj-beta",
        f"{len(scoped)} hit(s), all from proj-beta",
    )

    # --- 5. cleanup ----------------------------------------------------------
    client.delete(
        collection_name=COLLECTION,
        wait=True,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="_test_run", match=models.MatchValue(value=run_id))]
            )
        ),
    )
    # Count only THIS run's points: the collection legitimately holds real
    # indexed content now, so a global points_count==0 assertion (valid when
    # this test predated the indexer) would fail forever.
    residue = client.count(
        collection_name=COLLECTION,
        count_filter=models.Filter(
            must=[models.FieldCondition(key="_test_run", match=models.MatchValue(value=run_id))]
        ),
        exact=True,
    ).count
    check("cleanup left no residue", residue == 0, f"{residue} test points remain")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
