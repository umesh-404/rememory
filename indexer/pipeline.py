"""Orchestration: discover -> chunk -> embed -> store.

Kept deliberately thin. Every hard decision lives in the module that owns it,
so this file reads as a description of the process rather than an implementation
of it.

Files are processed one at a time and their chunks embedded in batches. We do
not accumulate the whole project in memory first: a large repo would mean
hundreds of megabytes of text and vectors resident for no benefit, and
per-file processing means an interrupted run leaves a partially-indexed but
entirely consistent collection (each file is all-or-nothing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .chunkers import Chunk
from .chunkers.code import CodeChunker
from .chunkers.docs import DocsChunker
from .chunkers.text import chunk_text
from .config import Config, Project
from .discovery import DiscoveredFile, Discovery, git_commit
from .embedder import Embedder
from .sparse import build_sparse_vector
from .store import Store


@dataclass
class IndexStats:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped_unchanged: int = 0
    files_failed: int = 0
    chunks: int = 0
    by_collection: dict[str, int] = field(default_factory=dict)
    fallback_parses: int = 0
    files_deleted: int = 0
    files_renamed: int = 0
    secrets_redacted: int = 0
    errors: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.code_chunker = CodeChunker(
            max_chars=config.chunking.max_chunk_chars,
            min_chars=config.chunking.min_chunk_chars,
            overlap_lines=config.chunking.overlap_lines,
        )
        self.docs_chunker = DocsChunker(
            max_chars=config.chunking.max_chunk_chars,
            min_chars=config.chunking.min_chunk_chars,
            overlap_lines=config.chunking.overlap_lines,
        )

    # ------------------------------------------------------------- chunking
    def chunk_file(self, disc: DiscoveredFile, source: str) -> tuple[list[Chunk], bool]:
        """Return (chunks, used_fallback)."""
        if disc.chunker == "code" and disc.ts_language:
            chunks = self.code_chunker.chunk(source, disc.ts_language)
            if chunks:
                return chunks, False
            # Parsing failed or found nothing. Degrade to line windows rather
            # than dropping the file -- half-written code is still searchable.
            return self._fallback(source), True

        if disc.chunker == "docs":
            return self.docs_chunker.chunk(source), False

        # A `code` file with no tree-sitter grammar configured also line-window
        # chunks -- count it as a fallback parse so the stat stays honest if
        # such a mapping is ever added. Plain text/config files are not
        # fallbacks; line windows are their intended chunking.
        return self._fallback(source), disc.chunker == "code"

    def _fallback(self, source: str) -> list[Chunk]:
        return chunk_text(
            source,
            max_chars=self.cfg.chunking.max_chunk_chars,
            min_chars=self.cfg.chunking.min_chunk_chars,
            overlap_lines=self.cfg.chunking.overlap_lines,
        )

    def header_for(self, disc: DiscoveredFile, chunk: Chunk) -> str | None:
        """Breadcrumb prepended to the embedded text (never to stored content).

        Supplies the vocabulary a bare chunk lacks: `def allow(self, key)` does
        not mention rate limiting, but `services/rate_limit.py :: LeakyBucket.allow`
        does.
        """
        if not self.cfg.chunking.contextual_header:
            return None
        parts = [disc.rel_path]
        if chunk.heading_path:
            parts.append(chunk.heading_path)
        elif chunk.symbol_name:
            parts.append(f"{chunk.symbol_type or 'symbol'} {chunk.symbol_name}")
        return " :: ".join(parts)

    # -------------------------------------------------------------- indexing
    def index_project(
        self,
        project: Project,
        store: Store,
        embedder: Embedder,
        *,
        only_changed: bool = False,
        progress=None,
        limit: int | None = None,
    ) -> IndexStats:
        stats = IndexStats()
        discovery = Discovery(self.cfg, project)
        files = discovery.walk()
        if limit:
            files = files[:limit]
        stats.files_seen = len(files)

        commit = git_commit(project.root)

        # What is already indexed, and with which hash. Needed for two things:
        # skipping unchanged files (only_changed) and detecting deletions --
        # so it is loaded on EVERY run, not just incremental ones. A full
        # re-index that ignored deletions would still leave ghosts.
        existing: dict[str, dict[str, str]] = {}
        for coll in ("code", "docs"):
            existing[coll] = store.indexed_hashes(coll, project.name)

        # ---- deletions & renames -------------------------------------------
        # Anything indexed but no longer on disk gets its chunks removed.
        # `limit` runs skip this: a partial walk would misread absent files as
        # deleted and purge most of the index.
        #
        # Membership is checked PER COLLECTION, not just "is the path on
        # disk". If a file's classification changes (say a config edit moves
        # .txt from the docs chunker to the text chunker), its path is still
        # on disk but its chunks now belong in the other collection -- a
        # plain on-disk check would leave the old copies stranded, and
        # searches would return both versions forever.
        if limit is None:
            on_disk_by_coll: dict[str, set[str]] = {"code": set(), "docs": set()}
            for f in files:
                on_disk_by_coll["docs" if f.chunker == "docs" else "code"].add(f.rel_path)
            hash_on_disk = {f.content_hash for f in files}
            for coll in ("code", "docs"):
                for stale_path, stale_hash in existing[coll].items():
                    if stale_path in on_disk_by_coll[coll]:
                        continue
                    store.delete_file(coll, project.name, stale_path)
                    stats.files_deleted += 1
                    # Rename = same content, new path. Counted separately only
                    # for honest reporting; the new path is (re)indexed in the
                    # main loop below regardless, because the embedded text
                    # includes the file-path breadcrumb -- vectors from the old
                    # path would be subtly wrong, so they cannot be reused.
                    if stale_hash and stale_hash in hash_on_disk:
                        stats.files_renamed += 1

        task = progress.add_task(f"[cyan]{project.name}", total=len(files)) if progress else None

        from .lockfile import heartbeat

        for disc in files:
            # Keep the writer lock visibly alive: its staleness window is
            # short (holder-death detection), so a long-running index must
            # refresh it or a concurrent starter would steal it mid-run.
            heartbeat()
            if progress:
                progress.update(task, advance=1, description=f"[cyan]{project.name}[/] {disc.rel_path[:52]}")  # noqa: E501

            collection = "docs" if disc.chunker == "docs" else "code"

            if only_changed and existing.get(collection, {}).get(disc.rel_path) == disc.content_hash:  # noqa: E501
                stats.files_skipped_unchanged += 1
                continue

            try:
                count = self._index_one(disc, project, collection, store, embedder, commit, stats)
            except Exception as exc:  # one bad file must not abort the run
                stats.files_failed += 1
                stats.errors.append(f"{disc.rel_path}: {type(exc).__name__}: {exc}")
                continue

            if count:
                stats.files_indexed += 1
                stats.chunks += count
                stats.by_collection[collection] = stats.by_collection.get(collection, 0) + count

        return stats

    def _index_one(
        self,
        disc: DiscoveredFile,
        project: Project,
        collection: str,
        store: Store,
        embedder: Embedder,
        commit: str | None,
        stats: IndexStats,
    ) -> int:
        source = disc.path.read_text(encoding="utf-8", errors="replace")

        # Secret redaction BEFORE chunking/embedding/storage: a credential
        # that never enters the store can never be retrieved into Claude's
        # context (and from there into transcripts that leave the machine).
        # Line counts are preserved, so citations stay correct.
        from .redact import redact

        source, redactions = redact(source)
        if redactions:
            stats.secrets_redacted += redactions

        chunks, fallback = self.chunk_file(disc, source)
        if fallback:
            stats.fallback_parses += 1
        if not chunks:
            return 0

        texts = [c.embed_text(self.header_for(disc, c)) for c in chunks]
        vectors = embedder.embed_documents(texts)

        sparse = None
        if self.cfg.sparse.enabled:
            # Built from the SAME text that was embedded, header included, so
            # a search for the file path also matches lexically.
            sparse = [build_sparse_vector(t, self.cfg.sparse) for t in texts]

        base_payload: dict[str, object] = {
            "language": disc.language,
            "content_hash": disc.content_hash,
            "last_modified": _iso(disc.mtime),
        }
        if commit:
            base_payload["git_commit"] = commit
        if collection == "docs":
            base_payload["doc_type"] = disc.doc_type or "guide"
            base_payload["title"] = Path(disc.rel_path).stem

        # Delete first: a file that shrank would otherwise leave stale tail
        # chunks that nothing overwrites.
        store.delete_file(collection, project.name, disc.rel_path)

        return store.upsert(
            collection,
            project=project.name,
            rel_path=disc.rel_path,
            chunks=chunks,
            vectors=vectors,
            sparse=sparse,
            base_payload=base_payload,
        )


def _iso(mtime: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
