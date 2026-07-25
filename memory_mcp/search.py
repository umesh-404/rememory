"""Hybrid retrieval: dense (semantic) + sparse (lexical) with server-side fusion.

Why hybrid, concretely
----------------------
Dense vectors answer "what is this about?" -- they find `rotate_credentials`
for "how do we refresh expired tokens". But they blur exact strings: ask for
`VoiceRouter` and a dense model returns things that are *about* call routing,
not necessarily the class itself. Sparse term-frequency vectors have exactly
the opposite profile. Code search needs both at once.

Qdrant fuses the two result lists server-side with RRF (Reciprocal Rank
Fusion). RRF scores each point by its RANK in each list, not its raw score --
which matters because cosine similarities and BM25-ish scores live on
incomparable scales; rank is the only thing they share.

The sparse query vector is built with the same tokenizer used at index time
(indexer/sparse.py) -- same CRC32 hashing, same identifier splitting. Any
drift between the two would silently break lexical matching, so it is the
same imported function, not a copy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from indexer.config import Config
from indexer.embedder import Embedder
from indexer.sparse import build_sparse_vector

# How many candidates each branch contributes before fusion. Fetching more than
# we return lets a hit that is mediocre in one branch but strong in the other
# surface after fusion.
PREFETCH_MULTIPLIER = 4


@dataclass
class SearchResult:
    id: str  # point id -- for memory results this is what update/delete take
    score: float
    project: str
    source_path: str
    start_line: int | None
    end_line: int | None
    content: str
    symbol: str | None  # symbol_name or heading_path, whichever exists
    extra: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "score": round(self.score, 4),
            "project": self.project,
            "location": (
                f"{self.source_path}:{self.start_line}-{self.end_line}"
                if self.start_line
                else self.source_path
            ),
            "content": self.content,
        }
        if self.symbol:
            out["symbol"] = self.symbol
        out.update(self.extra)
        return out


class Searcher:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.client = QdrantClient(url=config.qdrant_url, timeout=30)
        self.embedder = Embedder(config.embedding)
        # Imported here (not at module top) to keep a clean layering: rerank
        # depends on config only, search depends on rerank.
        from .rerank import Reranker

        self.reranker = Reranker(config)

    def search(
        self,
        collection: str,
        query: str,
        *,
        project: str | None = None,
        limit: int = 6,
        filters: dict[str, Any] | None = None,
        rerank: bool = True,
        max_per_file: int = 2,
        expand: bool = False,
    ) -> list[SearchResult]:
        dense = self.embedder.embed_query(query)

        # The query goes through the SAME sparse builder as documents did.
        indices, values = build_sparse_vector(query, self.cfg.sparse)

        conditions: list[models.FieldCondition] = []
        if project:
            conditions.append(
                models.FieldCondition(key="project", match=models.MatchValue(value=project))
            )
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if isinstance(value, list):
                conditions.append(
                    models.FieldCondition(key=key, match=models.MatchAny(any=value))
                )
            else:
                conditions.append(
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                )
        query_filter = models.Filter(must=conditions) if conditions else None

        prefetch = [
            models.Prefetch(
                query=dense,
                using="dense",
                filter=query_filter,
                limit=limit * PREFETCH_MULTIPLIER,
            )
        ]
        if indices:
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=indices, values=values),
                    using="lexical",
                    filter=query_filter,
                    limit=limit * PREFETCH_MULTIPLIER,
                )
            )

        # Fetch enough fused candidates to feed the reranker, not just `limit`:
        # the whole point of two-stage retrieval is that stage 1 over-fetches
        # for recall and stage 2 restores precision.
        fetch = max(limit, self.reranker.candidates if rerank else limit)

        try:
            points = self.client.query_points(
                collection_name=collection,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=fetch,
                with_payload=True,
            ).points
        except Exception as exc:
            # Sparse branch can fail on an empty-vocabulary query ("???").
            # Degrade to dense-only rather than erroring the tool call.
            print(f"hybrid search failed, retrying dense-only: {exc}", file=sys.stderr)
            points = self.client.query_points(
                collection_name=collection,
                query=dense,
                using="dense",
                query_filter=query_filter,
                limit=fetch,
                with_payload=True,
            ).points

        results = [self._to_result(p) for p in points]

        # Stage 2: cross-encoder reranking (falls back to RRF order on any
        # failure -- see rerank.py for the policy).
        if rerank:
            results = self.reranker.rerank(query, results)

        # Recency bias, memory collection only (the supermemory/Zep pattern):
        # when two memories are comparably relevant, the newer one should win
        # -- a 2026 deployment note beats a 2024 one about the same subject.
        # The factor is deliberately gentle (x0.85 at infinite age, x1.0
        # today, ~90-day half-life of the bonus): relevance still dominates,
        # recency only breaks near-ties. Code/docs skip this entirely --
        # old code that matches best IS the right answer.
        if collection == "memory":
            results = self._recency_bias(results)

        # Diversity: one file monopolizing the result list crowds out the
        # second-best *place* to look. Cap chunks per file, backfilling from
        # the remainder so the caller still gets `limit` results when possible.
        if max_per_file > 0:
            per_file: dict[str, int] = {}
            diverse: list[SearchResult] = []
            overflow: list[SearchResult] = []
            for r in results:
                key = f"{r.project}:{r.source_path}"
                if per_file.get(key, 0) < max_per_file:
                    per_file[key] = per_file.get(key, 0) + 1
                    diverse.append(r)
                else:
                    overflow.append(r)
            results = (diverse + overflow)[:limit]
        else:
            results = results[:limit]

        # Context expansion: merge each hit with its neighbouring chunks from
        # the same file, so the caller sees the surrounding code/prose without
        # a second round-trip. Possible cheaply because point ids are
        # deterministic (uuid5 of project:path:chunk_index).
        if expand and collection in ("code", "docs"):
            for r in results:
                self._expand(collection, r)

        return results

    def _recency_bias(self, results: list[SearchResult]) -> list[SearchResult]:
        import math
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        scored: list[tuple[float, int, SearchResult]] = []
        for pos, r in enumerate(results):
            # Base relevance: rerank score when present (0-1), else a rank
            # proxy so RRF-only results still order sensibly.
            base = r.extra.get("rerank", 1.0 / (pos + 1))
            factor = 0.85
            created = r.extra.get("updated_at") or r.extra.get("created_at")
            if created:
                try:
                    age_days = (now - datetime.fromisoformat(created)).days
                    factor = 0.85 + 0.15 * math.exp(-max(age_days, 0) / 90)
                except ValueError:
                    pass
            scored.append((base * factor, pos, r))
        # pos as tiebreak keeps the sort stable for identical scores.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [r for _, _, r in scored]

    def _expand(self, collection: str, result: SearchResult) -> None:
        from indexer.store import point_id

        idx = result.extra.get("chunk_index")
        total = result.extra.get("chunk_total")
        if idx is None or total is None:
            return
        neighbour_ids = [
            point_id(result.project, result.source_path, i)
            for i in (idx - 1, idx + 1)
            if 0 <= i < total
        ]
        if not neighbour_ids:
            return
        try:
            neighbours = self.client.retrieve(
                collection_name=collection, ids=neighbour_ids, with_payload=True
            )
        except Exception as exc:
            print(f"expand failed: {exc}", file=sys.stderr)
            return
        before, after = [], []
        for n in neighbours:
            p = n.payload or {}
            if p.get("chunk_index", -1) < idx:
                before.append(p)
            else:
                after.append(p)
        parts = []
        if before:
            parts.append(before[0].get("content", ""))
            result.start_line = before[0].get("start_line", result.start_line)
        parts.append(result.content)
        if after:
            parts.append(after[0].get("content", ""))
            result.end_line = after[0].get("end_line", result.end_line)
        result.content = "\n\n".join(parts)
        result.extra["expanded"] = True

    def _to_result(self, point) -> SearchResult:
        p = point.payload or {}
        extra: dict[str, Any] = {}
        for key in ("title", "language", "symbol_type", "doc_type", "memory_type", "tags",
                    "status", "created_at", "updated_at", "git_commit",
                    "chunk_index", "chunk_total"):
            if p.get(key) is not None:
                extra[key] = p[key]
        return SearchResult(
            id=str(point.id),
            score=point.score,
            project=p.get("project", "?"),
            source_path=p.get("source_path", "?"),
            start_line=p.get("start_line"),
            end_line=p.get("end_line"),
            content=p.get("content", ""),
            symbol=p.get("symbol_name") or p.get("heading_path"),
            extra=extra,
        )
