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

import re
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
    "fix",             # error text -> cause -> fix; looked up by lookup_error
    "recipe",          # an exact command sequence verified to work
    "vendor",          # a third-party price/rate/API behaviour: needs a
                       # source URL and expires (recheck_after)
}

# How a memory's claim is known. A stale "reported" fact and a stale "tested"
# one deserve different trust, so search shows the class with every result.
EVIDENCE = {
    "tested",          # ran it and saw it work
    "read-at-source",  # read the code / spec / vendor page itself
    "reported",        # second-hand: search summaries, someone said, a ticket
    "inferred",        # reasoned from other facts, not checked directly
}
VENDOR_RECHECK_DAYS = 30
_SUPERSEDE_RE = None  # compiled lazily

_IDENT = r"[A-Z][A-Z0-9]{0,7}-\d+(?![\w-])"
_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,7}-\d+(?![\w-])")
# Review only (never retires anything): a wider verb list than auto-supersede.
_REPLACES_RE = re.compile(
    r"\b(?i:supersedes|superseded|superseding|replaces|replaced|replacing|reverses|"
    r"reversed|reverting|overrides|overrode|override\s+of|overturns|overturned|"
    r"retires|retired|obsoletes|undoes)"
    rf"\s+((?:{_IDENT})(?:\s*(?:,|and|&|/)\s*{_IDENT})*)")
_SELF_RETIRED_RE = re.compile(
    r"^\W*(?:superseded|obsolete|retracted|deprecated|outdated|replaced|void)\b", re.I)
_PART_RE = re.compile(
    r"\b(?:part|pt\.?|section|chapter|episode|step)\s*(?:\d+|[ivx]+)\b"
    r"|\(\s*\d+\s*/\s*\d+\s*\)|\b\d+\s+of\s+\d+\b", re.I)


_GONE_RE = re.compile(r"\b(?:delet\w*|remov\w*|retir\w*|dropp\w*|prun\w*|gone|"
                      r"no longer exists?|renamed|moved)\b", re.I)


def _said_gone(text: str, ref: str) -> bool:
    """True if the text near a mention of `ref` says it was deleted/moved."""
    start = 0
    while (k := text.find(ref, start)) != -1:
        if _GONE_RE.search(text[max(0, k - 90):k + len(ref) + 90]):
            return True
        start = k + len(ref)
    return False


def _tracked_files(root) -> frozenset[str] | None:
    """Every git-tracked path, so a path written relative to a sub-app
    (tests/setup.ts for apps/web/tests/setup.ts) is not reported missing."""
    import subprocess
    import sys

    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=15,
            check=False, stdin=subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return frozenset(out.stdout.decode("utf-8", "replace").split("\0")) - {""}


def _tracked_suffix(tracked: frozenset[str] | None, ref: str) -> bool:
    if not tracked:
        return False
    tail = "/" + ref
    return any(t.endswith(tail) for t in tracked)


def _same_series(a: str, b: str) -> bool:
    """Numbered parts of one write-up ("X part 3", "X (4/8)") complement each
    other; a later part never makes an earlier one stale."""
    if not (_PART_RE.search(a) and _PART_RE.search(b)):
        return False

    def stem(t: str) -> str:
        # The series name is what comes before the part marker.
        m = _PART_RE.search(t)
        return " ".join(re.sub(r"\W+", " ", t[:m.start()] if m else t).lower().split())
    sa, sb = stem(a), stem(b)
    return bool(sa) and sa == sb

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
    # Active memories on the same topic that this one may contradict or
    # replace (similarity below the duplicate threshold, above RELATED_FLOOR).
    related: list[dict[str, Any]] | None = None
    auto_superseded: dict[str, list] | None = None


class MemoryStore:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.client = QdrantClient(url=config.qdrant_url, timeout=30)
        self.embedder = Embedder(config.embedding)
        # review() is O(n^2) over a project's memories and runs on every
        # briefing. Cache per project; any write through this store clears it,
        # and a TTL bounds staleness from edits made by other processes.
        self._review_cache: dict[tuple, tuple[float, list]] = {}

    # Cosine similarity above which a new memory is considered a duplicate of
    # an existing ACTIVE one. Calibrated against measured distributions on
    # this corpus (not guessed): a full paraphrase of the same fact scored
    # 0.909 against its original, while the nearest genuinely-distinct memory
    # scored 0.535 -- a wide gap, and 0.85 sits safely inside it. The two
    # error costs are asymmetric: a false positive costs one retry with
    # allow_duplicate=true; a false negative silts the collection permanently.
    DUPLICATE_THRESHOLD = 0.85
    # Below the duplicate line but clearly the same subject: the band where a
    # NEW memory most often silently contradicts an OLD one ("engine uses its
    # own Pipecat loop" vs a later "Pipecat removed"). Surfaced on store and
    # flagged by review(), never acted on automatically.
    RELATED_FLOOR = 0.62
    # A newer memory this close restates the same facts (paraphrases of one
    # fact score ~0.91; distinct same-topic notes 0.70-0.88 on this corpus).
    RESTATED_THRESHOLD = 0.92
    # Operational notes rot faster than decisions.
    PERISHABLE_TYPES = ("deployment", "bug", "api", "implementation")
    PERISHABLE_DAYS = 90

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
        depends_on: list[str] | None = None,
        evidence: str | None = None,
        source_url: str | None = None,
        recheck_after: str | None = None,
        error_signature: str | None = None,
        apply_defaults: bool = True,
    ) -> StoredMemory:
        self._validate(project, memory_type, title, content)
        extra = self._validate_extra(project, memory_type, content, depends_on, evidence,
                                     source_url, recheck_after, error_signature,
                                     apply_defaults=apply_defaults)

        now = datetime.now(UTC).isoformat()
        point_id = str(uuid.uuid4())  # random on purpose: memories are not
        # derived from files, so there is nothing to be deterministic against.

        embed_text = f"{title}\n\n{content}"
        if error_signature:
            # The error text is what a later lookup will paste in; embed it.
            embed_text = f"{title}\n\nERROR: {error_signature}\n\n{content}"
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

        related = [] if supersedes else self._find_related(vector, project)

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
        payload.update(extra)

        if supersedes:
            supersedes = self.resolve_id(supersedes)
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
        auto = ([] if memory_type == "session" else
                self._auto_supersede(project, point_id, title, content, skip=supersedes))
        self._invalidate()
        return StoredMemory(point_id, title, memory_type, project, "active", now, related,
                            auto)

    # ----------------------------------------------------------------- update
    def update(self, memory_id: str, *, title: str | None, content: str | None,
               tags: list[str] | None, **fields: Any) -> StoredMemory:
        """Supersede an existing memory with a corrected version.

        Implemented as store(supersedes=old) so history is preserved. Fields
        not provided are carried over from the original.
        """
        memory_id = self.resolve_id(memory_id)
        old = self._get(memory_id)
        p = old.payload
        if p.get("status") == "superseded":
            raise MemoryError(
                f"Memory {memory_id} is already superseded"
                + (f" by {p['superseded_by']}" if p.get("superseded_by") else "")
                + ". Update the active version instead (find it with list_memories)."
            )
        overrides = {k: v for k, v in fields.items() if v is not None}
        content_changed = content is not None and content != p.get("content")
        if not content_changed and not overrides:
            # Only the title and/or tags change: the knowledge itself is the
            # same, so keep the id -- briefings, handoffs and other memories
            # refer to it. (A content change still makes a new version.)
            return self._relabel(memory_id, p, title, tags)
        # "tested" describes the OLD text; a rewrite has not been tested.
        carried_evidence = p.get("evidence")
        if content_changed and carried_evidence == "tested":
            carried_evidence = None
        # Carried dependencies that no longer exist are dropped (the memory
        # was probably flagged precisely because one vanished) -- refusing the
        # update would block the very fix the flag asked for.
        root = self._root(p.get("project"))
        carried_deps = [d.get("ref") for d in p.get("depends_on") or []
                        if d.get("ref") and (root is None or _inside(root, d.get("path", "")))
                        and (root is None or (root / d.get("path", "")).exists())]
        replacement = self._store_with(overrides,
            project=p["project"],
            memory_type=p["memory_type"],
            # `is not None`, not `or`: an explicitly-passed empty string should
            # reach validation (and be rejected with a clear message), not
            # silently keep the old value.
            title=title if title is not None else p["title"],
            content=content if content is not None else p["content"],
            tags=tags if tags is not None else p.get("tags"),
            supersedes=memory_id,
            depends_on=carried_deps or None,
            evidence=carried_evidence,
            apply_defaults=False,
            source_url=p.get("source_url"),
            recheck_after=(p.get("recheck_after") if p.get("memory_type") != "vendor"
                           else None),  # a rewritten vendor fact restarts its clock
            error_signature=p.get("error_signature"),
        )
        # Back-link so a stale id in Claude's context can be followed forward.
        self.client.set_payload(
            collection_name=COLLECTION,
            payload={"superseded_by": replacement.id},
            points=[memory_id],
            wait=True,
        )
        return replacement

    def _relabel(self, memory_id: str, p: dict, title: str | None,
                 tags: list[str] | None) -> StoredMemory:
        now = datetime.now(UTC).isoformat()
        payload = dict(p)
        if tags is not None:
            payload["tags"] = sorted(set(tags))
        if title is not None and title != p.get("title"):
            if not title.strip():
                raise MemoryError("A memory needs a title.")
            payload["title"] = title
            payload["previous_titles"] = [*(p.get("previous_titles") or []), p.get("title")]
        if payload == p:
            raise MemoryError("Nothing to change: give a new title, content, tags or "
                              "another field.")
        payload["updated_at"] = now
        if payload.get("title") != p.get("title"):
            # The title is part of the embedded text: re-embed under the same id.
            embed_text = f"{payload['title']}\n\n{payload.get('content') or ''}"
            if payload.get("error_signature"):
                embed_text = (f"{payload['title']}\n\nERROR: {payload['error_signature']}"
                              f"\n\n{payload.get('content') or ''}")
            vectors: dict[str, Any] = {"dense": self.embedder.embed_documents([embed_text])[0]}
            indices, values = build_sparse_vector(embed_text, self.cfg.sparse)
            if indices:
                vectors["lexical"] = models.SparseVector(indices=indices, values=values)
            self.client.upsert(
                collection_name=COLLECTION,
                points=[models.PointStruct(id=memory_id, vector=vectors, payload=payload)],
                wait=True)
        else:
            self.client.set_payload(collection_name=COLLECTION,
                                    payload={"tags": payload["tags"], "updated_at": now},
                                    points=[memory_id], wait=True)
        self._invalidate()
        return StoredMemory(memory_id, payload["title"], payload["memory_type"],
                            payload["project"], payload.get("status", "active"),
                            payload.get("created_at", now))

    # ----------------------------------------------------------------- delete
    def delete(self, memory_id: str) -> dict[str, str]:
        """Hard-delete ONE memory by exact id. The only destructive operation,
        and deliberately the narrowest: no filters, no lists, no wildcards."""
        memory_id = self.resolve_id(memory_id)
        old = self._get(memory_id)  # raises if it does not exist
        title = old.payload.get("title", "?")
        self.client.delete(
            collection_name=COLLECTION,
            points_selector=models.PointIdsList(points=[memory_id]),
            wait=True,
        )
        self._invalidate()
        return {"deleted": memory_id, "title": title}

    # ----------------------------------------------------------------- retire
    def retire(self, memory_id: str, *, reason: str, replaced_by: str | None = None) -> dict:
        """Mark an active memory superseded WITHOUT writing a new version.

        For knowledge that is simply no longer true (or is now covered by a
        different memory). Non-destructive: content stays, status flips, the
        reason is recorded, so the history still explains what changed.
        """
        memory_id = self.resolve_id(memory_id)
        old = self._get(memory_id)
        if (old.payload or {}).get("status") != "active":
            raise MemoryError(f"Memory {memory_id} is not active; nothing to retire.")
        if not reason or len(reason.strip()) < 10:
            raise MemoryError("Give a short reason (what made it stale), at least 10 chars.")
        payload: dict[str, Any] = {
            "status": "superseded",
            "retired_reason": reason.strip(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if replaced_by:
            replaced_by = self.resolve_id(replaced_by)  # must exist; short ids ok
            if replaced_by == memory_id:
                raise MemoryError("A memory cannot be replaced by itself.")
            payload["superseded_by"] = replaced_by
        self.client.set_payload(collection_name=COLLECTION, payload=payload,
                                points=[memory_id], wait=True)
        self._invalidate()
        return {"retired": memory_id, "title": old.payload.get("title"),
                "replaced_by": replaced_by}

    def confirm(self, memory_id: str) -> dict:
        """Record that a memory was checked and still holds; review() stops
        flagging it against memories written before this confirmation."""
        memory_id = self.resolve_id(memory_id)
        old = self._get(memory_id)
        if (old.payload or {}).get("status") != "active":
            raise MemoryError(f"Memory {memory_id} is not active.")
        now = datetime.now(UTC).isoformat()
        payload: dict[str, Any] = {"verified_at": now, "suspect": None}
        deps = old.payload.get("depends_on") or []
        if deps:
            root = self._root(old.payload.get("project"))
            payload["depends_on"] = [dict(d, hash=dependency_hash(root, d)) for d in deps]
        if old.payload.get("memory_type") == "vendor":
            payload["recheck_after"] = _days_from_now(VENDOR_RECHECK_DAYS)
        self.client.set_payload(collection_name=COLLECTION, payload=payload,
                                points=[memory_id], wait=True)
        self._invalidate()
        return {"confirmed": memory_id, "title": old.payload.get("title"), "verified_at": now}

    # -------------------------------------------------------------- dependents
    def flag_dependents(self, project: str, changed_paths: list[str]) -> list[dict]:
        """After a sync: memories depending on a changed file/symbol whose
        content hash no longer matches become `suspect` (labelled in search,
        listed in briefings). Never retired automatically -- a change to a
        file does not prove the memory wrong, only that it needs a look."""
        changed = sorted({c.replace("\\", "/") for c in changed_paths if c})
        if not changed:
            return []
        root = self._root(project)
        flagged = []
        for start in range(0, len(changed), 200):
            batch = changed[start:start + 200]
            points, offset = [], None
            while True:
                page, offset = self.client.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=models.Filter(must=[
                        models.FieldCondition(key="project",
                                              match=models.MatchValue(value=project)),
                        models.FieldCondition(key="status",
                                              match=models.MatchValue(value="active")),
                        models.FieldCondition(key="depends_paths",
                                              match=models.MatchAny(any=batch)),
                    ]),
                    limit=256, offset=offset, with_payload=True, with_vectors=False,
                )
                points.extend(page)
                if offset is None:
                    break
            for pt in points:
                reason = drift_reason(root, (pt.payload or {}).get("depends_on") or [])
                if reason and (pt.payload or {}).get("suspect") != reason:
                    self.client.set_payload(
                        collection_name=COLLECTION,
                        payload={"suspect": reason,
                                 "suspect_since": datetime.now(UTC).isoformat()},
                        points=[pt.id], wait=True)
                    flagged.append({"id": str(pt.id), "title": pt.payload.get("title"),
                                    "reason": reason})
        if flagged:
            self._invalidate()
        return flagged

    # ----------------------------------------------------------------- review
    REVIEW_TTL = 600.0

    def review(self, project: str, root=None, limit: int = 20) -> list[dict[str, Any]]:
        import time

        key = (project, str(root), limit)
        hit = self._review_cache.get(key)
        if hit and time.monotonic() - hit[0] < self.REVIEW_TTL:
            return hit[1]
        result = self._review(project, root, limit)
        self._review_cache[key] = (time.monotonic(), result)
        return result

    def _invalidate(self) -> None:
        self._review_cache.clear()

    def _review(self, project: str, root=None, limit: int = 20) -> list[dict[str, Any]]:
        """Active memories that are probably stale, with the reason.

        Signals, none of them acted on automatically:
        * evidence a NEWER memory replaces it (see the comment below -- topic
          similarity alone is deliberately not enough);
        * suspect dependencies or a passed recheck date;
        * the content names repo files that no longer exist;
        * an operational note older than PERISHABLE_DAYS, never re-confirmed.
        A memory confirmed (confirm_memory) after the newer one was written is
        not re-flagged for that pair.
        """
        import re
        from pathlib import Path

        import numpy as np

        points, _ = self.client.scroll(
            collection_name=COLLECTION,
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="project", match=models.MatchValue(value=project)),
                models.FieldCondition(key="status", match=models.MatchValue(value="active")),
            ]),
            limit=2000,
            with_payload=True,
            with_vectors=["dense"],
        )
        rows = [p for p in points if (p.payload or {}).get("memory_type") != "session"]
        if not rows:
            return []
        now = datetime.now(UTC)

        def stamp(value: str | None) -> datetime | None:
            try:
                d = datetime.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                return None
            return d.replace(tzinfo=UTC) if d and d.tzinfo is None else d

        created = [stamp((p.payload or {}).get("created_at")) or now for p in rows]
        verified = [stamp((p.payload or {}).get("verified_at")) for p in rows]

        def vec(p) -> list[float]:
            v = p.vector.get("dense") if isinstance(p.vector, dict) else p.vector
            return list(v or [])

        flags: dict[int, list[str]] = {}
        newer_of: dict[int, tuple[int, str]] = {}  # i -> (j, why): likely replaced
        n = len(rows)
        titles = [(p.payload or {}).get("title") or "" for p in rows]

        # Topical similarity alone is NOT staleness: parts 1-8 of one teardown,
        # or a feature and its follow-up, are all "about the same thing" without
        # either being out of date. Only evidence that the newer memory REPLACES
        # the older one counts:
        #   * the older one's own title says SUPERSEDED/OBSOLETE/... (still active);
        #   * a newer memory says it supersedes/replaces/reverses/overrides an id
        #     (D-36, ADR-4) that the older one's title carries;
        #   * a newer memory is a near-copy (similarity >= RESTATED) and the two
        #     are not numbered parts of one series.
        for i, t in enumerate(titles):
            if _SELF_RETIRED_RE.match(t):
                flags.setdefault(i, []).append(
                    "its title says it is superseded/obsolete, but it is still active "
                    "-- retire_memory it (or remove the marker if it is current)")
        title_ids = [set(_ID_RE.findall(t)) for t in titles]
        replaced_ids: list[set[str]] = []
        for j, p in enumerate(rows):
            pj = p.payload or {}
            named: set[str] = set()
            for m in _REPLACES_RE.finditer(f"{titles[j]}\n{pj.get('content') or ''}"):
                named |= set(_ID_RE.findall(m.group(1)))
            replaced_ids.append(named - title_ids[j])
        sims = None
        vectors = [vec(p) for p in rows]
        dim = max((len(v) for v in vectors), default=0)
        if n > 1 and dim and all(len(v) == dim for v in vectors):
            mat = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            unit = mat / norms
            sims = unit @ unit.T
        for i in range(n):
            for j in range(n):
                if i == j or created[j] <= created[i]:
                    continue  # only a NEWER memory can replace i
                if verified[i] and verified[i] >= created[j]:
                    continue  # confirmed still true after j was written
                hit = title_ids[i] & replaced_ids[j]
                if hit:
                    why = f"says it replaces {', '.join(sorted(hit))}"
                elif (sims is not None and sims[i, j] >= self.RESTATED_THRESHOLD
                      and not _same_series(titles[i], titles[j])):
                    why = f"restates the same facts (similarity {sims[i, j]:.2f})"
                else:
                    continue
                if i not in newer_of or hit:
                    newer_of[i] = (j, why)
        for i, (j, why) in newer_of.items():
            pj = rows[j].payload or {}
            flags.setdefault(i, []).append(
                f"newer memory {str(rows[j].id)[:8]} '{pj.get('title')}' "
                f"({(pj.get('created_at') or '')[:10]}) {why} -- check whether this is "
                f"still true")

        # Repo-relative paths with an extension, e.g. apps/api/src/main.py.
        # Multi-dot names (dashboard.test.tsx) are captured whole.
        path_re = re.compile(
            r"(?<![\w/.:-])((?:[\w.-]+/)+[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{1,5})(?![\w/.])")
        root_path = Path(root) if root else None
        tracked = _tracked_files(root_path) if root_path is not None else None
        today = now.date().isoformat()
        for i, p in enumerate(rows):
            pl = p.payload or {}
            if pl.get("suspect"):
                flags.setdefault(i, []).append(f"suspect: {pl['suspect']}")
            elif pl.get("depends_on") and root_path is not None:
                reason = drift_reason(root_path, pl["depends_on"])
                if reason:
                    flags.setdefault(i, []).append(f"suspect: {reason}")
            if pl.get("recheck_after") and str(pl["recheck_after"])[:10] < today:
                flags.setdefault(i, []).append(
                    f"recheck date {str(pl['recheck_after'])[:10]} has passed -- re-read "
                    f"{pl.get('source_url') or 'the source'} and confirm_memory or update it")
            if root_path is not None and root_path.exists():
                text = pl.get("content") or ""
                gone_record = _GONE_RE.search(pl.get("title") or "")
                missing = [
                    ref for ref in dict.fromkeys(path_re.findall(text))
                    if _inside(root_path, ref) and (root_path / ref.split("/")[0]).is_dir()
                    and not (root_path / ref).exists() and not _tracked_suffix(tracked, ref)
                    # A memory RECORDING the deletion is accurate, not stale.
                    and not gone_record and not _said_gone(text, ref)
                ]
                if missing:
                    flags.setdefault(i, []).append(
                        "mentions files that no longer exist: " + ", ".join(missing[:4]))
            age = (now - created[i]).days
            if (pl.get("memory_type") in self.PERISHABLE_TYPES and age > self.PERISHABLE_DAYS
                    and not verified[i]):
                flags.setdefault(i, []).append(f"{pl.get('memory_type')} note is {age} days old")

        out = []
        for i, reasons in flags.items():
            pl = rows[i].payload or {}
            out.append({
                "id": str(rows[i].id),
                "title": pl.get("title"),
                "memory_type": pl.get("memory_type"),
                "created_at": pl.get("created_at"),
                "reason": "; ".join(reasons),
                "preview": (pl.get("content") or "")[:200],
                "_contradiction": i in newer_of or bool(_SELF_RETIRED_RE.match(
                    pl.get("title") or "")),
            })
        # Contradiction candidates first (most dangerous), then oldest first.
        out.sort(key=lambda r: (not r["_contradiction"], r["created_at"] or ""))
        for r in out:
            r.pop("_contradiction")
        return out[:limit]

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
                "updated_at": p.get("updated_at"),
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

    def _store_with(self, overrides: dict[str, Any], **kwargs: Any) -> StoredMemory:
        kwargs.update(overrides)
        return self.store(**kwargs)

    def _root(self, project: str | None):
        proj = self.cfg.projects.get(project or "")
        return proj.root if proj else None

    def _validate_extra(self, project: str, memory_type: str, content: str,
                        depends_on: list[str] | None, evidence: str | None,
                        source_url: str | None, recheck_after: str | None,
                        error_signature: str | None,
                        apply_defaults: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if evidence is not None:
            if evidence not in EVIDENCE:
                raise MemoryError(f"evidence must be one of: {', '.join(sorted(EVIDENCE))}")
            out["evidence"] = evidence
        if source_url:
            if not source_url.startswith(("http://", "https://")):
                raise MemoryError("source_url must be an http(s) URL.")
            out["source_url"] = source_url
        if recheck_after:
            out["recheck_after"] = _parse_recheck(recheck_after)
        if memory_type == "vendor":
            if not source_url:
                raise MemoryError("A vendor fact needs source_url (where it was read).")
            if evidence is None:
                raise MemoryError("A vendor fact needs evidence (read-at-source if you read "
                                  "the page yourself, reported if from search summaries).")
            out["read_at"] = datetime.now(UTC).date().isoformat()
            out.setdefault("recheck_after", _days_from_now(VENDOR_RECHECK_DAYS))
        if memory_type == "fix" and (not error_signature or len(error_signature.strip()) < 3):
            raise MemoryError("A fix memory needs error_signature: the distinctive error "
                              "text, exactly as it appears (e.g. 'REFUSED TO SCORE').")
        if error_signature:
            out["error_signature"] = error_signature.strip()[:500]
        if memory_type == "recipe" and apply_defaults:
            out.setdefault("evidence", evidence or "tested")
        if out.get("evidence") == "tested":
            out["verified_at"] = datetime.now(UTC).isoformat()
        if depends_on:
            root = self._root(project)
            deps = []
            for ref in depends_on[:20]:
                ref = ref.strip().replace("\\", "/").removeprefix("./")
                path, _, symbol = ref.partition("::")
                if root is not None:
                    if not _inside(root, path):
                        raise MemoryError(f"depends_on: {path!r} is outside {project}'s root.")
                    if not (root / path).exists():
                        raise MemoryError(f"depends_on: {path!r} does not exist in {project} "
                                          f"(use repo-relative paths, optionally path::Symbol).")
                    path = _canonical(root, path)
                    ref = path + (f"::{symbol}" if symbol else "")
                dep = {"ref": ref, "path": path, "symbol": symbol or None}
                dep["hash"] = dependency_hash(root, dep)
                if symbol and dep["hash"] is None:
                    raise MemoryError(f"depends_on: symbol {symbol!r} not found in {path} "
                                      f"(a dependency that cannot be located would never "
                                      f"be flagged).")
                deps.append(dep)
            out["depends_on"] = deps
            out["depends_paths"] = sorted({d["path"] for d in deps})
        return out

    def _auto_supersede(self, project: str, new_id: str, title: str, content: str,
                        skip: str | None) -> dict[str, list]:
        """`supersedes D-127` / `replaces ADR-4` in a new memory retires the
        active memory whose TITLE STARTS WITH that id -- the decision log
        already says it; the memory store should not need telling twice.

        Deliberately narrow, because a false positive silently retires real
        knowledge: the id must be upper-case PREFIX-NUMBER written exactly so
        (so "replaces utf-8" or "retires gpt-4" never match), the target's
        title must begin with it, and exactly ONE active memory may match --
        several matches are returned as candidates for a human/agent to pick.
        """
        import re

        global _SUPERSEDE_RE
        if _SUPERSEDE_RE is None:
            ident = r"[A-Z][A-Z0-9]{0,7}-\d+(?![\w-])"
            _SUPERSEDE_RE = re.compile(
                r"\b(?i:supersedes|superseding|replaces|replacing|reverses|overturns|retires)"
                rf"\s+((?:{ident})(?:\s*(?:,|and|&|/)\s*{ident})*)")
        targets: list[str] = []
        for m in _SUPERSEDE_RE.finditer(f"{title}\n{content}"):
            targets += re.findall(r"[A-Z][A-Z0-9]{0,7}-\d+", m.group(1))
        own = set(re.findall(r"[A-Z][A-Z0-9]{0,7}-\d+", title))
        targets = [t for t in dict.fromkeys(targets) if t not in own]
        result: dict[str, list] = {"retired": [], "candidates": []}
        if not targets:
            return result
        points, _ = self.client.scroll(
            collection_name=COLLECTION,
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="project", match=models.MatchValue(value=project)),
                models.FieldCondition(key="status", match=models.MatchValue(value="active")),
            ]),
            limit=5000, with_payload=["title", "memory_type"], with_vectors=False,
        )
        now = datetime.now(UTC).isoformat()
        for target in targets:
            starts = re.compile(rf"^\W*{re.escape(target)}(?![\d])")
            matches = [
                pt for pt in points
                if str(pt.id) not in (new_id, skip)
                and (pt.payload or {}).get("memory_type") != "session"
                and starts.match((pt.payload or {}).get("title") or "")
            ]
            if len(matches) == 1:
                pt = matches[0]
                self.client.set_payload(
                    collection_name=COLLECTION,
                    payload={"status": "superseded", "superseded_by": new_id,
                             "updated_at": now,
                             "retired_reason": f"superseded: {new_id[:8]} says it replaces "
                                               f"{target}"},
                    points=[pt.id], wait=True)
                result["retired"].append({"id": str(pt.id), "via": target,
                                          "title": (pt.payload or {}).get("title")})
            elif len(matches) > 1:
                result["candidates"] += [{"id": str(pt.id), "via": target,
                                          "title": (pt.payload or {}).get("title")}
                                         for pt in matches[:5]]
        return result

    def _find_related(self, vector: list[float], project: str) -> list[dict]:
        """Active memories on the same subject, below the duplicate line."""
        try:
            hits = self.client.query_points(
                collection_name=COLLECTION,
                query=vector,
                using="dense",
                limit=4,
                score_threshold=self.RELATED_FLOOR,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project",
                                              match=models.MatchValue(value=project)),
                        models.FieldCondition(key="status",
                                              match=models.MatchValue(value="active")),
                    ],
                    must_not=[
                        models.FieldCondition(key="memory_type",
                                              match=models.MatchValue(value="session")),
                    ],
                ),
                with_payload=["title", "memory_type", "created_at"],
            ).points
        except Exception:
            return []  # advisory only -- never block a store on it
        return [
            {"id": str(h.id), "title": (h.payload or {}).get("title"),
             "memory_type": (h.payload or {}).get("memory_type"),
             "created_at": ((h.payload or {}).get("created_at") or "")[:10],
             "similarity": round(h.score, 2)}
            for h in hits if h.score < self.DUPLICATE_THRESHOLD
        ]

    def resolve_id(self, memory_id: str) -> str:
        """The full id for a full id OR a unique prefix of one (briefings and
        review lists show 8 characters). Never passes a raw Qdrant
        "not a valid point ID" error through to the agent."""
        raw = (memory_id or "").strip().lower()
        compact = raw.replace("-", "")
        if not compact or any(c not in "0123456789abcdef" for c in compact):
            raise MemoryError(
                f"{memory_id!r} is not a memory id. Ids look like '2ef554fe' (short) or "
                f"'2ef554fe-....' (full); get them from search/list/briefing results.")
        if len(compact) == 32:
            return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"
        if len(compact) < 6:
            raise MemoryError(f"Id prefix {memory_id!r} is too short; give at least 6 "
                              f"characters (briefings show 8).")
        matches: list[str] = []
        offset = None
        while True:
            page, offset = self.client.scroll(collection_name=COLLECTION, limit=1000,
                                              offset=offset, with_payload=False,
                                              with_vectors=False)
            matches += [str(pt.id) for pt in page
                        if str(pt.id).replace("-", "").startswith(compact)]
            if offset is None:
                break
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MemoryError(
                f"No memory with id {memory_id!r}. Ids come from store/list/search "
                f"results -- use list_memories to find the right one.")
        raise MemoryError(f"Id prefix {memory_id!r} matches {len(matches)} memories "
                          f"({', '.join(matches[:5])}); give more characters.")

    def _get(self, memory_id: str):
        memory_id = self.resolve_id(memory_id)
        try:
            found = self.client.retrieve(
                collection_name=COLLECTION, ids=[memory_id], with_payload=True
            )
        except Exception as exc:  # never hand a raw database error to the agent
            raise MemoryError(f"Could not read memory {memory_id!r}: "
                              f"{type(exc).__name__}") from exc
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


# ------------------------------------------------------------ module helpers
def _days_from_now(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(days=days)).date().isoformat()


def _parse_recheck(value: str) -> str:
    """Accept an ISO date or a number of days ("30" / "30d")."""
    v = str(value).strip().lower().rstrip("d")
    if v.isdigit():
        return _days_from_now(int(v))
    try:
        return datetime.fromisoformat(str(value).strip()).date().isoformat()
    except ValueError as exc:
        raise MemoryError("recheck_after must be an ISO date (2026-12-01) or days ('30').") \
            from exc


def _inside(root, rel: str) -> bool:
    """Is root/rel inside root? Rejects '..' escapes and absolute paths."""
    from pathlib import Path

    try:
        return (Path(root) / rel).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _canonical(root, rel: str) -> str:
    """The path as spelled on disk (case-correct on Windows), so it matches
    the indexer's changed_paths."""
    from pathlib import Path

    try:
        return (Path(root) / rel).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return rel


def dependency_hash(root, dep: dict) -> str | None:
    """sha256 of the file, or of just the symbol's lines when a symbol is
    named (so unrelated edits elsewhere in the file do not raise alarms).
    None = the file (or symbol) does not exist."""
    import hashlib
    from pathlib import Path

    if root is None or not _inside(root, dep["path"]):
        return None
    path = Path(root) / dep["path"]
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not dep.get("symbol"):
        return hashlib.sha256(raw).hexdigest()
    from .symbols import locate

    span = locate(Path(root), dep["path"], dep["symbol"], None)
    if span is None:
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()[span[0] - 1: span[1]]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def drift_reason(root, deps: list[dict]) -> str | None:
    changed = []
    for d in deps:
        current = dependency_hash(root, d)
        if current == d.get("hash"):
            continue
        what = d.get("ref") or d.get("path")
        changed.append(f"{what} {'no longer exists' if current is None else 'changed'}")
    if not changed:
        return None
    return "; ".join(changed[:4]) + " since this memory was written or last confirmed"
