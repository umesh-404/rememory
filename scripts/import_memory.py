"""Restore memories from a JSON export -- the other half of backup.

A backup that has never been restored is not a backup; this script exists so
the export in data/backups is a tested, working recovery path rather than a
hope. It is also the MIGRATION path: exports are payload-only, so restoring
re-embeds every memory with whatever model is currently configured -- exactly
what you need after switching embedding models (when all stored vectors
become invalid by definition).

    uv run --directory D:\\memory-system scripts/import_memory.py                    # newest export
    uv run --directory D:\\memory-system scripts/import_memory.py data\\backups\\memory-20260725.json

Semantics:
* Upserts by original id -- restoring over a live collection is safe and
  idempotent; existing memories are overwritten with their backed-up state,
  memories created after the export are untouched.
* --dry-run prints what would happen without writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qdrant_client import models

from indexer.config import load_config
from indexer.embedder import Embedder
from indexer.sparse import build_sparse_vector

BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_file", nargs="?", help="path to a memory-*.json export")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.export_file:
        path = Path(args.export_file)
    else:
        candidates = sorted(BACKUP_DIR.glob("memory-*.json"), reverse=True)
        if not candidates:
            raise SystemExit(f"No exports found in {BACKUP_DIR}")
        path = candidates[0]

    data = json.loads(path.read_text(encoding="utf-8"))
    points = data.get("points", [])
    print(f"restoring from {path.name}: {len(points)} memories "
          f"(exported {data.get('exported_at', '?')})")
    if args.dry_run:
        for p in points:
            pay = p["payload"]
            print(f"  would restore [{pay.get('memory_type', '?'):14}] "
                  f"{pay.get('project', '?')}: {pay.get('title', '?')[:60]}")
        return 0

    cfg = load_config()
    from qdrant_client import QdrantClient

    client = QdrantClient(url=cfg.qdrant_url, timeout=60)

    restored = 0
    with Embedder(cfg.embedding) as embedder:
        embedder.health()
        for p in points:
            payload = p["payload"]
            # Re-embed from content: vectors were deliberately not exported,
            # which is what makes this file survive embedding-model changes.
            embed_text = f"{payload.get('title', '')}\n\n{payload.get('content', '')}"
            vector = embedder.embed_documents([embed_text])[0]
            indices, values = build_sparse_vector(embed_text, cfg.sparse)

            vectors: dict = {"dense": vector}
            if indices:
                vectors["lexical"] = models.SparseVector(indices=indices, values=values)

            # Stamp the CURRENT schema_version: these vectors were just made
            # by the currently configured model, whatever the export said.
            payload = {**payload, "schema_version": cfg.embedding.schema_version}

            client.upsert(
                collection_name="memory",
                points=[models.PointStruct(id=p["id"], vector=vectors, payload=payload)],
                wait=True,
            )
            restored += 1
            print(f"  restored [{payload.get('memory_type', '?'):14}] "
                  f"{payload.get('title', '?')[:60]}")

    print(f"\n{restored} memories restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
