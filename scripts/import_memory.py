"""Restore memories from a JSON export -- the other half of backup.

A backup that has never been restored is not a backup; this script exists so
the export in data/backups is a tested, working recovery path rather than a
hope. It is also the MIGRATION path: exports are payload-only, so restoring
re-embeds every memory with whatever model is currently configured -- exactly
what you need after switching embedding models (when all stored vectors
become invalid by definition).

    uv run scripts/import_memory.py                                # newest export
    uv run scripts/import_memory.py data\\backups\\memory-20260725.json

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

    if data.get("collection") not in (None, "memory"):
        raise SystemExit(f"REFUSED: {path.name} is an export of "
                         f"'{data.get('collection')}', not of 'memory'.")

    cfg = load_config()
    from indexer.store import Store

    # Same invariant check the indexer runs: the collection must exist and its
    # dense vector size must match the configured model. Without it, a restore
    # after an embedding-model switch paid for a full re-embed and then died
    # on the first upsert with a raw dimension-mismatch traceback -- the
    # scenario this script is documented as THE migration path for. verify()
    # points at create_collections.py, which rebuilds the collection at the
    # new dimensions without touching other data.
    store = Store(cfg)
    store.verify()
    client = store.client

    # Memories from projects no longer in the registry restore fine but are
    # half-usable: project-filtered search rejects the name and update_memory
    # refuses it. Say so up front instead of leaving them silently crippled.
    orphans = sorted({
        p["payload"].get("project", "?") for p in points
        if p["payload"].get("project") not in cfg.projects
    })
    if orphans:
        print(f"  note: {len(orphans)} project(s) in this backup are not in "
              f"config/projects.yaml: {', '.join(orphans)}. Their memories "
              f"will restore and appear in unfiltered/cross-project search, "
              f"but project-filtered tools reject them until you re-register "
              f"the project(s).")

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
