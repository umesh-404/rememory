"""Qdrant writes: deterministic ids, upserts, and scoped deletes.

The one idea that makes everything else work: POINT IDS ARE DERIVED, NOT RANDOM.

    id = uuid5(NAMESPACE, "project:relative/path.py:3")

Because the id is a pure function of (project, file, chunk index), re-indexing a
file overwrites its chunks in place instead of appending duplicates. Without
this, every re-index would double the corpus and searches would fill up with
stale copies of the same code. It is also what makes Phase 7's incremental
updates straightforward rather than a bookkeeping exercise.

The one thing that survives it: a file that SHRINKS (10 chunks -> 6) leaves
chunks 7-10 orphaned, since nothing overwrites them. So we always delete a
file's existing points before writing its new ones.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient, models

from .chunkers import Chunk
from .config import Config

# Fixed namespace: ids must be identical across runs and machines. Generated
# once and hardcoded on purpose -- never regenerate it, or every existing point
# becomes unreachable.
NAMESPACE = uuid.UUID("6f1b3a52-9c4d-5e8f-a7b2-1d0c4e9f3a86")


def point_id(project: str, rel_path: str, chunk_index: int) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project}:{rel_path}:{chunk_index}"))


class Store:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.client = QdrantClient(url=config.qdrant_url, timeout=120)

    # ------------------------------------------------------------- preflight
    def verify(self) -> None:
        """Fail before indexing if the collections are missing or mis-sized."""
        for name in ("code", "docs", "memory"):
            if not self.client.collection_exists(name):
                raise SystemExit(
                    f"Collection '{name}' does not exist.\n"
                    f"Run: uv run scripts\\create_collections.py"
                )
            info = self.client.get_collection(name)
            vectors = info.config.params.vectors
            size = (vectors["dense"] if isinstance(vectors, dict) else vectors).size
            if size != self.cfg.embedding.dimensions:
                raise SystemExit(
                    f"Collection '{name}' has {size}-d vectors but the configured "
                    f"model produces {self.cfg.embedding.dimensions}-d. "
                    f"Re-create the collection and re-index."
                )

    # ---------------------------------------------------------------- writes
    def delete_file(self, collection: str, project: str, rel_path: str) -> None:
        """Remove every chunk belonging to one file.

        Always called before writing that file's new chunks, so a file that
        shrank does not leave orphaned tail chunks behind.
        """
        self.client.delete(
            collection_name=collection,
            wait=True,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project", match=models.MatchValue(value=project)),  # noqa: E501
                        models.FieldCondition(
                            key="source_path", match=models.MatchValue(value=rel_path)
                        ),
                    ]
                )
            ),
        )

    def delete_project(self, collection: str, project: str) -> None:
        """Wipe one project from one collection. Never used on `memory`."""
        if collection == "memory":
            raise ValueError("refusing to bulk-delete from the `memory` collection")
        self.client.delete(
            collection_name=collection,
            wait=True,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project", match=models.MatchValue(value=project))
                    ]
                )
            ),
        )

    def upsert(
        self,
        collection: str,
        *,
        project: str,
        rel_path: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        sparse: list[tuple[list[int], list[float]]] | None,
        base_payload: dict[str, Any],
    ) -> int:
        if not chunks:
            return 0

        now = datetime.now(UTC).isoformat()
        points: list[models.PointStruct] = []

        for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            vector_payload: dict[str, Any] = {"dense": vector}
            if sparse is not None:
                indices, values = sparse[i]
                if indices:
                    vector_payload["lexical"] = models.SparseVector(
                        indices=indices, values=values
                    )

            payload = {
                **base_payload,
                "project": project,
                "source_path": rel_path,
                # Stored verbatim -- the contextual header used for embedding is
                # deliberately NOT included, so Claude reads back real file text.
                "content": chunk.content,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "chunk_index": i,
                "chunk_total": len(chunks),
                "indexed_at": now,
                "schema_version": self.cfg.embedding.schema_version,
            }
            if chunk.symbol_type:
                payload["symbol_type"] = chunk.symbol_type
            if chunk.symbol_name:
                payload["symbol_name"] = chunk.symbol_name
            if chunk.heading_path:
                payload["heading_path"] = chunk.heading_path

            points.append(
                models.PointStruct(
                    id=point_id(project, rel_path, i),
                    vector=vector_payload,
                    payload=payload,
                )
            )

        self.client.upsert(collection_name=collection, points=points, wait=False)
        return len(points)

    # ----------------------------------------------------------------- reads
    def project_stats(self, project: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for name in ("code", "docs", "memory"):
            if not self.client.collection_exists(name):
                continue
            if project:
                count = self.client.count(
                    collection_name=name,
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="project", match=models.MatchValue(value=project)
                            )
                        ]
                    ),
                    exact=True,
                ).count
            else:
                count = self.client.get_collection(name).points_count or 0
            out[name] = count
        return out

    def indexed_hashes(self, collection: str, project: str) -> dict[str, str]:
        """Map source_path -> content_hash for everything currently indexed.

        Phase 7 uses this to detect changed, deleted and renamed files. Built
        here in Phase 4 because the payload fields it relies on must be written
        correctly from the very first index, not retrofitted later.
        """
        hashes: dict[str, str] = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project", match=models.MatchValue(value=project))
                    ]
                ),
                limit=1000,
                offset=offset,
                with_payload=["source_path", "content_hash"],
                with_vectors=False,
            )
            for p in points:
                if p.payload:
                    hashes[p.payload["source_path"]] = p.payload.get("content_hash", "")
            if offset is None:
                break
        return hashes
