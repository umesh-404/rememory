"""The MCP server Claude Code talks to.

Run (Claude Code does this automatically once configured in Phase 6):

    uv run --directory <path-to-rememory> -m memory_mcp.server

(The package is `memory_mcp`, not `mcp`: a local package named `mcp` would
shadow the official SDK package of the same name and break its own imports.)

Uses FastMCP from the official `mcp` SDK: each @tool function's signature and
docstring become the tool schema Claude sees. The docstrings below are written
FOR CLAUDE -- they are the only documentation the model gets when deciding
which tool to call, so they say when to use each tool, not just what it does.

stdout discipline: stdio transport means stdout carries JSON-RPC. Nothing here
may print() to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Auto-update check FIRST, before any project module is imported: if a newer
# version is pulled, the process re-execs so the freshly pulled code -- not a
# mix of old loaded modules and new files -- is what serves the client.
# Stdlib-only; every failure is a silent skip (see updater.py).
from .updater import maybe_update

maybe_update()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from indexer.config import load_config  # noqa: E402

from .memories import MemoryError, MemoryStore  # noqa: E402
from .search import Searcher  # noqa: E402

cfg = load_config()
app = FastMCP(
    "rememory",
    instructions=(
        "Local development memory: semantic+lexical search over indexed source "
        "code, documentation, and stored knowledge for registered projects. "
        f"Registered projects: {', '.join(cfg.projects)}. "
        "Search before assuming; store durable knowledge after significant work."
    ),
)

searcher = Searcher(cfg)
memory_store = MemoryStore(cfg)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False)


def _results(results) -> str:
    if not results:
        return "No results. Try broader wording, or drop filters."
    return _dump([r.to_dict() for r in results])


def _guard(fn):
    """Convert infrastructure failures into actionable guidance for Claude.

    Without this, a stopped Docker Desktop or Ollama surfaces as a raw
    exception traceback in the tool result -- alarming, and useless for
    deciding what to do next. The message names the broken dependency and the
    exact command that fixes it, so Claude can relay it (or, for Ollama pulls,
    fix it itself).
    """
    import functools

    from indexer.embedder import EmbeddingError

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EmbeddingError as exc:
            return (
                "SERVICE DOWN (Ollama): cannot embed the query. "
                "Ollama is probably not running -- it normally starts at login; "
                "starting the Ollama app (or `ollama serve`) fixes this. "
                f"Detail: {exc}"
            )
        except Exception as exc:
            text = str(exc).lower()
            if "connect" in text or "connection" in text or "refused" in text or "timed out" in text:  # noqa: E501
                return (
                    "SERVICE DOWN (Qdrant): cannot reach the vector database at "
                    "127.0.0.1:6333. Docker Desktop is probably not running. "
                    "Fix: start Docker Desktop, or run "
                    "docker compose -f docker/compose.yml up -d (from the rememory folder). "
                    f"Detail: {type(exc).__name__}: {exc}"
                )
            raise

    return wrapper


def _clamp(value: int, lo: int, hi: int) -> int:
    """Bound caller-supplied sizes. An unclamped limit=100000 would flood the
    model's context with an entire collection -- a self-inflicted denial of
    usefulness the schema alone does not prevent."""
    return max(lo, min(hi, value))


def _bad_project(project: str | None) -> str | None:
    """A typo'd project name would otherwise return an empty result set that
    is indistinguishable from 'nothing matched' -- fail loudly instead."""
    if project is None or project in cfg.projects:
        return None
    return (
        f"REJECTED: unknown project {project!r}. "
        f"Registered: {', '.join(cfg.projects)}"
    )


# ---------------------------------------------------------------------- search
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def search_code(
    query: str,
    project: str,
    language: str | None = None,
    symbol_type: str | None = None,
    limit: int = 6,
    expand: bool = False,
) -> str:
    """Search indexed source code semantically and lexically (hybrid, then
    cross-encoder reranked for precision).

    Use this to find how something is implemented before writing new code:
    existing functions, classes, patterns, wiring. The query can be natural
    language ("how are calls routed to an agent") or an exact symbol name
    ("VoiceRouter") -- both work, and exact identifiers rank highly. Each
    result's `rerank` field (0-1) is the model's judgment that it actually
    answers the query -- treat results below ~0.3 with skepticism.

    project is required (one of the registered projects). Optionally filter by
    language (e.g. "python", "typescript") or symbol_type ("function", "class",
    "method", "interface", "module"). Set expand=true to merge each hit with
    its neighbouring chunks for surrounding context. Results include file:line
    locations you can open directly.
    """
    if err := _bad_project(project):
        return err
    return _results(
        searcher.search(
            "code",
            query,
            project=project,
            limit=_clamp(limit, 1, 25),
            filters={"language": language, "symbol_type": symbol_type},
            expand=expand,
        )
    )


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def search_docs(
    query: str,
    project: str,
    doc_type: str | None = None,
    limit: int = 6,
    expand: bool = False,
) -> str:
    """Search indexed documentation: specs, READMEs, ADRs, guides, schema docs
    (hybrid retrieval, then cross-encoder reranked).

    Use this to answer "what did we specify/decide/document" questions --
    requirements, data models, flows, operational rules. Results carry the
    heading trail (e.g. "TRD > Telephony > Fallback") so you can cite the
    exact section, and a `rerank` score (0-1) judging whether the section
    actually answers the query.

    project is required. doc_type optionally narrows to one of: readme, adr,
    openapi, schema, guide, changelog. Set expand=true to include each hit's
    neighbouring sections for fuller context.
    """
    if err := _bad_project(project):
        return err
    return _results(
        searcher.search(
            "docs",
            query,
            project=project,
            limit=_clamp(limit, 1, 25),
            filters={"doc_type": doc_type},
            expand=expand,
        )
    )


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def search_memory(
    query: str,
    project: str | None = None,
    memory_type: str | None = None,
    include_superseded: bool = False,
    limit: int = 6,
) -> str:
    """Search knowledge stored from previous development sessions: architecture
    decisions, bug investigations, feature/API summaries, deployment notes.

    CHECK THIS FIRST when starting non-trivial work -- a past session may have
    already made the relevant decision or solved the same problem. Omit
    `project` to search across ALL projects (useful for "have I solved this
    anywhere before?").

    memory_type narrows to: decision, feature, api, bug, deployment,
    implementation, design. Superseded versions are excluded unless
    include_superseded=true.
    """
    if err := _bad_project(project):
        return err
    filters: dict[str, Any] = {"memory_type": memory_type}
    if not include_superseded:
        filters["status"] = "active"
    return _results(
        searcher.search("memory", query, project=project, limit=_clamp(limit, 1, 25), filters=filters)  # noqa: E501
    )


# ---------------------------------------------------------------------- memory
@app.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
@_guard
def store_memory(
    project: str,
    memory_type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    allow_duplicate: bool = False,
) -> str:
    """Store a piece of durable development knowledge for future sessions.

    Use this after completing significant work: an architecture decision (and
    its WHY), a bug root cause, a feature summary, an API contract, deployment
    findings. Write it self-contained -- a future session has no context from
    this conversation. Do NOT store conversation transcripts, trivia, or
    anything derivable by reading the code; store conclusions.

    memory_type: decision | feature | api | bug | deployment | implementation | design.
    Keep content focused (40-8000 chars). Add 2-5 lowercase tags for browsing.
    Returns the stored memory's id -- reference it in update/delete.

    If a very similar active memory already exists the store is rejected with
    its id -- update_memory that id if yours replaces it, or retry with
    allow_duplicate=true only if the two are genuinely distinct facts.
    """
    try:
        m = memory_store.store(
            project=project, memory_type=memory_type, title=title,
            content=content, tags=tags, allow_duplicate=allow_duplicate,
        )
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    return _dump({"stored": m.id, "title": m.title, "type": m.memory_type, "project": m.project})


@app.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))  # noqa: E501
@_guard
def update_memory(
    memory_id: str,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Revise an existing memory when it is outdated or wrong.

    The old version is kept and marked superseded (history is preserved, like
    an ADR trail); a new active version is created and its NEW id returned.
    Provide only the fields that change. If you only have a vague recollection
    of the memory, find its exact id via list_memories or search_memory first.
    """
    try:
        m = memory_store.update(memory_id, title=title, content=content, tags=tags)
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    return _dump({"superseded": memory_id, "new_id": m.id, "title": m.title})


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def get_memory(memory_id: str) -> str:
    """Fetch ONE memory's FULL content and metadata by exact id.

    Use before update_memory when you need the complete existing text (search
    and list return previews/snippets; this returns everything), or to follow
    a supersedes/superseded_by link through a memory's revision history.
    """
    try:
        point = memory_store._get(memory_id)
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    payload = dict(point.payload or {})
    payload["id"] = str(point.id)
    return _dump(payload)


@app.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
@_guard
def delete_memory(memory_id: str) -> str:
    """Permanently delete ONE memory by exact id. Irreversible.

    Only for memories that are actively harmful (wrong AND misleading) or were
    stored by mistake. For merely outdated knowledge use update_memory, which
    preserves history. There is deliberately no bulk delete.
    """
    try:
        result = memory_store.delete(memory_id)
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    return _dump(result)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def list_memories(
    project: str | None = None,
    memory_type: str | None = None,
    include_superseded: bool = False,
    limit: int = 30,
    offset: int = 0,
) -> str:
    """Browse stored memories (newest first) without a search query.

    Use at the start of a session to see what is known about a project, or to
    find a memory's id for update/delete. Filters: project, memory_type, and
    include_superseded to see revision history. Paginated: the response
    carries total/has_more; pass offset to fetch the next page.
    """
    if err := _bad_project(project):
        return err
    items = memory_store.list_memories(
        project=project, memory_type=memory_type,
        include_superseded=include_superseded,
        # offset is bounded too: the pagination over-fetch reads offset+limit
        # rows, so an absurd offset would drag the whole collection through
        # one scroll.
        limit=_clamp(limit, 1, 100), offset=_clamp(offset, 0, 10_000),
    )
    if not items and offset == 0:
        return "No memories stored yet."
    page = memory_store.last_page
    return _dump({
        "total": page["total"],
        "count": len(items),
        "offset": page["offset"],
        "has_more": page["has_more"],
        "items": items,
    })


# --------------------------------------------------------------------- session
@app.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
@_guard
def save_session(
    project: str,
    summary: str,
    next_steps: str | None = None,
    files: list[str] | None = None,
) -> str:
    """Save a where-we-left-off handoff so the NEXT session can continue this
    work without re-reading the whole project.

    CALL THIS WHEN A WORK SESSION IS WRAPPING UP -- when the user says
    goodbye/that's it for today, when a milestone lands mid-conversation, or
    before context gets tight. Write it for a colleague with zero context:
    what was being worked on, what state it is in, what was decided along the
    way, what is unfinished.

    summary: what happened and current state (self-contained prose).
    next_steps: the concrete next actions, most important first.
    files: repo-relative paths the next session should read first -- this is
    what lets it open 3 files instead of 30.

    Each save replaces the previous session handoff for the project (the old
    one stays in history as superseded). The next session gets this back
    automatically at the top of get_briefing.
    """
    if err := _bad_project(project):
        return err

    parts = [summary.strip()]
    if next_steps:
        parts.append("NEXT STEPS:\n" + next_steps.strip())
    if files:
        parts.append("FILES TO READ FIRST:\n" + "\n".join(f"- {f}" for f in files))
    content = "\n\n".join(parts)

    from datetime import UTC, datetime

    title = f"Session handoff ({datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC)"

    # Exactly one active handoff per project: supersede the previous one.
    previous = memory_store.list_memories(project=project, memory_type="session", limit=1)
    try:
        m = memory_store.store(
            project=project,
            memory_type="session",
            title=title,
            content=content,
            tags=["session-handoff"],
            supersedes=previous[0]["id"] if previous else None,
            allow_duplicate=True,  # consecutive handoffs legitimately resemble each other
        )
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    return _dump({
        "saved": m.id,
        "replaces": previous[0]["id"] if previous else None,
        "note": "The next session will see this at the top of get_briefing.",
    })


# -------------------------------------------------------------------- briefing
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def get_briefing(project: str) -> str:
    """Compiled project context block -- CALL THIS ONCE AT SESSION START when
    beginning work on a registered project.

    This is the equivalent of a memory provider's profile injection: instead
    of you issuing several searches to reconstruct project state, it returns
    one block with the project description, all active decisions (full text --
    decisions are load-bearing), recent memories of other types, doc/code
    inventory, and freshness warnings for old operational notes. After
    reading it, search only for the specifics you need.
    """
    if err := _bad_project(project):
        return err
    from datetime import UTC, datetime

    from qdrant_client import models as qm

    proj = cfg.projects[project]
    lines: list[str] = [f"# {project}", ""]
    if proj.description:
        lines += [proj.description, ""]

    project_filter = qm.Filter(
        must=[qm.FieldCondition(key="project", match=qm.MatchValue(value=project))]
    )
    stats = {
        c: searcher.client.count(collection_name=c, count_filter=project_filter, exact=True).count
        for c in ("code", "docs", "memory")
    }
    lines += [f"Indexed: {stats['code']} code chunks, {stats['docs']} doc chunks, "
              f"{stats['memory']} memories.", ""]

    memories = memory_store.list_memories(project=project, limit=100)
    now = datetime.now(UTC)

    def age_days(m: dict) -> int | None:
        try:
            return (now - datetime.fromisoformat(m["created_at"])).days
        except (KeyError, TypeError, ValueError):
            return None

    sessions = [m for m in memories if m["memory_type"] == "session"]
    decisions = [m for m in memories if m["memory_type"] == "decision"]
    others = [m for m in memories if m["memory_type"] not in ("decision", "session")]

    # The session handoff comes FIRST: continuing where the last session
    # stopped is the single most valuable thing a briefing can do, and its
    # FILES TO READ FIRST list is what lets this session open three files
    # instead of thirty.
    if sessions:
        latest = memory_store._get(sessions[0]["id"]).payload or {}
        age = age_days(sessions[0])
        lines += [
            f"## Where you left off ({sessions[0]['created_at'][:16]}"
            + (f", {age}d ago" if age is not None else "") + ")",
            latest.get("content", sessions[0]["preview"]),
            "",
        ]

    if decisions:
        lines.append("## Active decisions (full text -- these are binding)")
        for m in decisions:
            full = memory_store._get(m["id"]).payload or {}
            lines += [f"\n### {m['title']}", full.get("content", m["preview"])]
        lines.append("")

    if others:
        lines.append("## Other recent memories (previews -- get_memory for full text)")
        for m in others[:10]:
            age = age_days(m)
            # Operational knowledge rots faster than decisions; flag it rather
            # than silently presenting a stale runbook as current truth.
            stale = (" [>90 days old -- verify still true]"
                     if age is not None and age > 90
                     and m["memory_type"] in ("deployment", "bug", "api") else "")
            lines.append(f"- [{m['memory_type']}] {m['title']} ({m['id'][:8]}){stale}")
            lines.append(f"  {m['preview']}")
        lines.append("")

    lines += [
        "## Working with this project",
        "- search_docs / search_code for specifics (hybrid + reranked; trust `rerank` >= 0.3)",
        "- store_memory after significant work; update_memory when a decision changes",
        "- sync_index after writing files so they are searchable immediately",
    ]
    return "\n".join(lines)


@app.prompt(name="kickoff", description="Load the project briefing and start work with full context")  # noqa: E501
def kickoff_prompt(project: str) -> str:
    """Exposed as a slash command in Claude Code (/mcp__rememory__kickoff)."""
    return (
        f"Call get_briefing for project '{project}' on the rememory MCP server and "
        f"read it carefully. If it contains a 'Where you left off' section, continue "
        f"that work: read the listed files first, then confirm in two sentences what "
        f"you are resuming and what the next step is. Otherwise summarize the active "
        f"decisions in two sentences. Treat stored decisions as binding constraints. "
        f"When this session wraps up, call save_session so the next one can continue."
    )


# ---------------------------------------------------------------------- status
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def memory_system_status() -> str:
    """Health and inventory: registered projects, points per collection, and
    when each project was last indexed. Use when search results seem stale or
    empty -- it distinguishes 'nothing indexed' from 'nothing matched'."""
    from qdrant_client import models as qm

    out: dict[str, Any] = {"projects": {}}
    for name, proj in cfg.projects.items():
        stats: dict[str, Any] = {"root": str(proj.root)}
        for coll in ("code", "docs", "memory"):
            stats[coll] = searcher.client.count(
                collection_name=coll,
                count_filter=qm.Filter(
                    must=[qm.FieldCondition(key="project", match=qm.MatchValue(value=name))]
                ),
                exact=True,
            ).count
        # Newest indexed_at gives "last indexed" without a separate ledger.
        # order_by is essential: an unordered limit-1 scroll returns an
        # ARBITRARY point, which made this field report a random past
        # timestamp. indexed_at has a datetime payload index, so ordering by
        # it is cheap.
        newest = ""
        for coll in ("code", "docs"):
            points, _ = searcher.client.scroll(
                collection_name=coll,
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(key="project", match=qm.MatchValue(value=name))]
                ),
                limit=1,
                order_by=qm.OrderBy(key="indexed_at", direction=qm.Direction.DESC),
                with_payload=["indexed_at"],
                with_vectors=False,
            )
            for p in points:
                newest = max(newest, (p.payload or {}).get("indexed_at", ""))
        stats["last_indexed"] = newest or None
        out["projects"][name] = stats
    out["model"] = cfg.embedding.name
    out["reindex_hint"] = "call sync_index to refresh the index for a project"
    return _dump(out)


@app.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
@_guard
def sync_index(project: str) -> str:
    """Incrementally re-index one project so search reflects the current files.

    Call this when search results look stale -- e.g. you just wrote or edited
    files and want them findable, or memory_system_status shows an old
    last_indexed. Fast: unchanged files are skipped by content hash, chunks of
    deleted files are purged. Typical run is a few seconds; a project with
    heavy changes can take a minute.
    """
    if project not in cfg.projects:
        return f"REJECTED: unknown project {project!r}. Registered: {', '.join(cfg.projects)}"
    proj = cfg.projects[project]
    if not proj.root.exists():
        return f"REJECTED: project root does not exist: {proj.root}"

    from indexer.pipeline import Pipeline
    from indexer.store import Store

    # One Store (and thus one QdrantClient connection pool) for the server's
    # lifetime. Creating a fresh client per sync_index call in a long-lived
    # process would accumulate unclosed connection pools -- a slow leak.
    global _SYNC_STORE, _SYNC_PIPELINE
    if "_SYNC_STORE" not in globals():
        _SYNC_STORE = Store(cfg)
        _SYNC_STORE.verify()
        _SYNC_PIPELINE = Pipeline(cfg)
    store = _SYNC_STORE

    # Same single-writer lock as the CLI and the scheduled task: without it,
    # Claude calling sync_index while the half-hourly sync runs would race
    # delete-then-write on the same points.
    from indexer.lockfile import holder_age_seconds, index_lock

    with index_lock() as acquired:
        if not acquired:
            age = holder_age_seconds() or 0.0
            return (
                f"BUSY: another index/sync is already running "
                f"({age:.0f}s old). The index will be fresh when it finishes -- "
                f"just retry the search; no action needed."
            )
        stats = _SYNC_PIPELINE.index_project(
            proj, store, searcher.embedder, only_changed=True
        )
    return _dump(
        {
            "project": project,
            "files_seen": stats.files_seen,
            "reindexed": stats.files_indexed,
            "unchanged": stats.files_skipped_unchanged,
            "deleted": stats.files_deleted,
            "renamed": stats.files_renamed,
            "failed": stats.files_failed,
            "new_chunks": stats.chunks,
            "secrets_redacted": stats.secrets_redacted,
            "errors": stats.errors[:5],
        }
    )


def main() -> None:
    print(f"rememory MCP server starting (projects: {', '.join(cfg.projects)})",
          file=sys.stderr)
    app.run()  # stdio transport


if __name__ == "__main__":
    main()
