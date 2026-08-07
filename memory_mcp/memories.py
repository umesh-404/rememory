"""The write path into the `memory` collection -- the irreplaceable one.

Policy, enforced here rather than hoped for:

* Memories are distilled knowledge (a decision, a summary, a finding), never
  transcripts. Size limits nudge that: too-short is noise, too-long is a dump.
* Updates SUPERSEDE rather than overwrite. The old memory stays, marked
  status=superseded and linked from its replacement. What you believed in
  March is part of the project's history; silently rewriting it destroys the
  audit trail that makes an ADR worth keeping.
* Deletes require the exact point id of a single memory. There is no
  delete-by-filter, no bulk path, by construction.
* Every memory belongs to a project and a type, so it is findable by browse
  as well as by search.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient, models

from indexer.config import Config
from indexer.embedder import Embedder
from indexer.sparse import build_sparse_vector

COLLECTION = "memory"

MEMORY_TYPES = {
    "decision",        # architecture / design decisions and their why
    "feature",         # what a feature does and how it is wired
    "api",             # endpoint/contract summaries
    "bug",             # investigation findings: root cause, fix, prevention
    "deployment",      # deploy/ops notes and runbooks
    "implementation",  # non-obvious how-it-works notes
    "design",          # UX/product design rationale
    "session",         # where-we-left-off handoff between work sessions;
                       # exactly one active per project (saves supersede)
}

MIN_CONTENT_CHARS = 40      # below this it is a note-to-self, not knowledge
MAX_CONTENT_CHARS = 8000    # above this it is a transcript dump; distill it


class MemoryError(ValueError):
    """Raised for invalid memory operations; the message is shown to Claude."""


@dataclass
class StoredMemory:
    id: str
    title: str
    memory_type: str
    project: str
    status: str
    created_at: str


class MemoryStore:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.client = QdrantClient(url=config.qdrant_url, timeout=30)
        self.embedder = Embedder(config.embedding)

    # Cosine similarity above which a new memory is considered a duplicate of
    # an existing ACTIVE one. Calibrated against measured distributions on
    # this corpus (not guessed): a full paraphrase of the same fact scored
    # 0.909 against its original, while the nearest genuinely-distinct memory
    # scored 0.535 -- a wide gap, and 0.85 sits safely inside it. The two
    # error costs are asymmetric: a false positive costs one retry with
    # allow_duplicate=true; a false negative silts the collection permanently.
    DUPLICATE_THRESHOLD = 0.85

    # ------------------------------------------------------------------ store
    def store(
        self,
        *,
        project: str,
        memory_type: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        supersedes: str | None = None,
        allow_duplicate: bool = False,
    ) -> StoredMemory:
        self._validate(project, memory_type, title, content)

        now = datetime.now(UTC).isoformat()
        point_id = str(uuid.uuid4())  # random on purpose: memories are not
        # derived from files, so there is nothing to be deterministic against.

        embed_text = f"{title}\n\n{content}"
        vector = self.embedder.embed_documents([embed_text])[0]

        # Near-duplicate guard: without it the collection silts up with
        # variants of the same fact and every search returns a chorus of
        # almost-identical memories. Skipped when superseding (the new version
        # is SUPPOSED to resemble the old) or when explicitly overridden.
        if not allow_duplicate and not supersedes:
            dup = self._find_duplicate(vector, project)
            if dup is not None:
                raise MemoryError(
                    f"Near-duplicate of existing active memory {dup['id']} "
                    f"({dup['title']!r}, similarity {dup['score']:.2f}). "
                    f"Either update_memory that id if this replaces it, or pass "
                    f"allow_duplicate=true if they are genuinely distinct."
                )

        indices, values = build_sparse_vector(embed_text, self.cfg.sparse)

        vectors: dict[str, Any] = {"dense": vector}
        if indices:
            vectors["lexical"] = models.SparseVector(indices=indices, values=values)

        payload: dict[str, Any] = {
            "project": project,
            "memory_type": memory_type,
            "title": title,
            "content": content,
            "tags": sorted(set(tags or [])),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "schema_version": self.cfg.embedding.schema_version,
            # source_path keeps the payload shape compatible with code/docs
            # results so one renderer handles all three collections.
            "source_path": f"memory/{memory_type}/{point_id[:8]}",
        }

        if supersedes:
            old = self._get(supersedes)
            payload["supersedes"] = supersedes
            # Mark the old one superseded, but never touch its content.
            self.client.set_payload(
                collection_name=COLLECTION,
                payload={"status": "superseded", "updated_at": now},
                points=[supersedes],
                wait=True,
            )
            # Carry the old tags forward unless the caller replaced them.
            if not tags and old.payload.get("tags"):
                payload["tags"] = old.payload["tags"]

        self.client.upsert(
            collection_name=COLLECTION,
            points=[models.PointStruct(id=point_id, vector=vectors, payload=payload)],
            wait=True,
        )
        return StoredMemory(point_id, title, memory_type, project, "active", now)

    # ----------------------------------------------------------------- update
    def update(self, memory_id: str, *, title: str | None, content: str | None,
               tags: list[str] | None) -> StoredMemory:
        """Supersede an existing memory with a corrected version.

        Implemented as store(supersedes=old) so history is preserved. Fields
        not provided are carried over from the original.
        """
        old = self._get(memory_id)
        p = old.payload
        if p.get("status") == "superseded":
            raise MemoryError(
                f"Memory {memory_id} is already superseded"
                + (f" by {p['superseded_by']}" if p.get("superseded_by") else "")
                + ". Update the active version instead (find it with list_memories)."
            )
        replacement = self.store(
            project=p["project"],
            memory_type=p["memory_type"],
            # `is not None`, not `or`: an explicitly-passed empty string should
            # reach validation (and be rejected with a clear message), not
            # silently keep the old value.
            title=title if title is not None else p["title"],
            content=content if content is not None else p["content"],
            tags=tags if tags is not None else p.get("tags"),
            supersedes=memory_id,
        )
        # Back-link so a stale id in Claude's context can be followed forward.
        self.client.set_payload(
            collection_name=COLLECTION,
            payload={"superseded_by": replacement.id},
            points=[memory_id],
            wait=True,
        )
        return replacement

    # ----------------------------------------------------------------- delete
    def delete(self, memory_id: str) -> dict[str, str]:
        """Hard-delete ONE memory by exact id. The only destructive operation,
        and deliberately the narrowest: no filters, no lists, no wildcards."""
        old = self._get(memory_id)  # raises if it does not exist
        title = old.payload.get("title", "?")
        self.client.delete(
            collection_name=COLLECTION,
            points_selector=models.PointIdsList(points=[memory_id]),
            wait=True,
        )
        return {"deleted": memory_id, "title": title}

    # ------------------------------------------------------------------- list
    def list_memories(
        self,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        include_superseded: bool = False,
        limit: int = 30,
        offset: int = 0,
        with_page: bool = False,
        full_content: bool = False,
    ) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
        conditions: list[models.FieldCondition] = []
        if project:
            conditions.append(
                models.FieldCondition(key="project", match=models.MatchValue(value=project))
            )
        if memory_type:
            conditions.append(
                models.FieldCondition(key="memory_type", match=models.MatchValue(value=memory_type))
            )
        if not include_superseded:
            conditions.append(
                models.FieldCondition(key="status", match=models.MatchValue(value="active"))
            )

        # order_by in the SCROLL, not a post-sort: an unordered scroll returns
        # points in id order, so with more memories than `limit` the newest
        # ones could be missing from a "newest first" listing entirely.
        # Over-fetch by offset+1 to derive has_more without a second query;
        # collections are hundreds of rows at most, so this stays cheap.
        query_filter = models.Filter(must=conditions) if conditions else None
        points, _ = self.client.scroll(
            collection_name=COLLECTION,
            scroll_filter=query_filter,
            limit=offset + limit + 1,
            order_by=models.OrderBy(key="created_at", direction=models.Direction.DESC),
            with_payload=True,
            with_vectors=False,
        )
        # Returned alongside the items (with_page=True) rather than stashed on
        # self: a mutable side channel on a shared store instance meant two
        # overlapping list calls could hand one caller the other's pagination.
        page = {
            "total": self.client.count(
                collection_name=COLLECTION, count_filter=query_filter, exact=True
            ).count,
            "offset": offset,
            "has_more": len(points) > offset + limit,
        }
        points = points[offset : offset + limit]
        out = []
        for pt in points:
            p = pt.payload or {}
            item = {
                "id": str(pt.id),
                "title": p.get("title"),
                "memory_type": p.get("memory_type"),
                "project": p.get("project"),
                "tags": p.get("tags", []),
                "status": p.get("status"),
                "created_at": p.get("created_at"),
                "preview": (p.get("content") or "")[:160],
            }
            if full_content:
                # The payload is already in hand -- callers that need whole
                # memories (the desktop app) get them without a _get() per row.
                item["content"] = p.get("content") or ""
            out.append(item)
        if with_page:
            return out, page
        return out

    # ---------------------------------------------------------------- helpers
    def _find_duplicate(self, vector: list[float], project: str) -> dict | None:
        """Nearest ACTIVE memory in this project, if above the threshold."""
        hits = self.client.query_points(
            collection_name=COLLECTION,
            query=vector,
            using="dense",
            limit=1,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="project", match=models.MatchValue(value=project)),
                    models.FieldCondition(key="status", match=models.MatchValue(value="active")),
                ]
            ),
            with_payload=["title"],
        ).points
        if hits and hits[0].score >= self.DUPLICATE_THRESHOLD:
            return {
                "id": str(hits[0].id),
                "title": (hits[0].payload or {}).get("title", "?"),
                "score": hits[0].score,
            }
        return None

    def _get(self, memory_id: str):
        found = self.client.retrieve(
            collection_name=COLLECTION, ids=[memory_id], with_payload=True
        )
        if not found:
            raise MemoryError(
                f"No memory with id {memory_id!r}. Ids come from store/list/search "
                f"results -- use list_memories to find the right one."
            )
        return found[0]

    def _validate(self, project: str, memory_type: str, title: str, content: str) -> None:
        if project not in self.cfg.projects:
            raise MemoryError(
                f"Unknown project {project!r}. Registered: {', '.join(self.cfg.projects)}. "
                f"Register new projects in config/projects.yaml first."
            )
        if memory_type not in MEMORY_TYPES:
            raise MemoryError(
                f"Invalid memory_type {memory_type!r}. One of: {', '.join(sorted(MEMORY_TYPES))}"
            )
        if not title or not title.strip():
            raise MemoryError("A memory needs a title.")
        if len(content) < MIN_CONTENT_CHARS:
            raise MemoryError(
                f"Content is {len(content)} chars; minimum is {MIN_CONTENT_CHARS}. "
                f"A memory should be a distilled, self-contained piece of knowledge."
            )
        if len(content) > MAX_CONTENT_CHARS:
            raise MemoryError(
                f"Content is {len(content)} chars; maximum is {MAX_CONTENT_CHARS}. "
                f"Distill the knowledge rather than storing a transcript."
            )
