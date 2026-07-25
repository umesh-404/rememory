"""Export the `memory` collection to plain JSON — the real backup.

Why JSON and not (only) a Qdrant snapshot: a snapshot restores into the same
Qdrant storage format and the same 1024-dim vector config. This JSON export is
payload-only -- titles, content, tags, history links -- with NO vectors, which
makes it the most durable form the knowledge can take: re-importable into any
future Qdrant version, any future embedding model (vectors are recomputed from
content on import), or greppable by hand in twenty years with no software at
all. The derived collections (code/docs) are deliberately not exported; they
are rebuilt from files with one command.

Run:    uv run --directory D:\\memory-system scripts/export_memory.py
Output: data/backups/memory-YYYYMMDD.json  (kept: most recent 30)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient

BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"
KEEP = 30


def main() -> None:
    client = QdrantClient(url=__import__("indexer.runtime", fromlist=["qdrant_url"]).qdrant_url(), timeout=60)

    points = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name="memory",
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,  # vectors are derivable from content; payload is not
        )
        points.extend({"id": str(p.id), "payload": p.payload} for p in batch)
        if offset is None:
            break

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    out = BACKUP_DIR / f"memory-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "exported_at": datetime.now(UTC).isoformat(),
                "collection": "memory",
                "count": len(points),
                "note": "payload-only export; re-embed content on import",
                "points": points,
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"exported {len(points)} memories -> {out}")

    # Retention: newest KEEP files stay. Same-day re-runs overwrite (same name).
    backups = sorted(BACKUP_DIR.glob("memory-*.json"), reverse=True)
    for old in backups[KEEP:]:
        old.unlink()
        print(f"pruned {old.name}")


if __name__ == "__main__":
    main()
