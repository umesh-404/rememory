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
import threading
import time
from typing import Any

# Auto-update check FIRST, before any project module is imported: if a newer
# version is pulled, the process re-execs so the freshly pulled code -- not a
# mix of old loaded modules and new files -- is what serves the client.
# Stdlib-only; every failure is a silent skip (see updater.py).
# This one MUST stay synchronous -- it re-execs the process -- but it is
# bounded by GIT_TIMEOUT (8s) and throttled to one check per 15 minutes.
from .health import ensure_services
from .updater import maybe_update

maybe_update()

# Self-healing runs in the BACKGROUND. It shells out to docker, which on a
# cold or wedged Docker Desktop can take well over a minute (30s for `docker
# info`, 60s for `docker start`, then a readiness wait). Doing that here at
# import time meant the server had not finished starting when the client sent
# its first tool call, and the call died with "MCP error -32001: Request timed
# out" -- while the heal itself was working fine.
#
# Nothing depends on the heal having finished: every tool is wrapped in _guard,
# which reports a clear "database is offline" message if Qdrant is not up yet.
# Healing concurrently just means the next call succeeds instead.
threading.Thread(target=ensure_services, name="rememory-heal", daemon=True).start()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from indexer.config import CONFIG_DIR as ROOT_CONFIG  # noqa: E402
from indexer.config import load_config  # noqa: E402

from .memories import MemoryError, MemoryStore  # noqa: E402
from .search import INTENT_POOL, Searcher, intent_types, prefer_types  # noqa: E402

cfg = load_config()
app = FastMCP(
    "rememory",
    instructions=(
        "Local development memory: semantic+lexical search over indexed source "
        "code, documentation, and stored knowledge for registered projects. "
        f"Registered projects: {', '.join(cfg.projects)}. "
        "Search before assuming; store durable knowledge after significant work. "
        "AT SESSION START: call find_project with the working directory -- if it "
        "is already registered, continue with get_briefing (never re-register); "
        "if not, offer to create a knowledge base with register_project. "
        "(Skip get_briefing if a 'rememory briefing' was already injected.) "
        "Saving effort: get_symbol returns one function plus its callers instead "
        "of a whole file; tests_for names the tests covering a file so you run "
        "those, not the full suite; lookup_error checks for a known fix before "
        "you investigate a failure; repo_facts answers migration heads, CI gates, "
        "commands and next ID numbers (take IDs with reserve_id). With other "
        "agents in the same tree, claim_files before editing. "
        "Memories can go stale: when one contradicts the code or a newer "
        "decision, fix it (update_memory / retire_memory) instead of trusting it."
    ),
)

searcher = Searcher(cfg)
memory_store = MemoryStore(cfg)


def _reload_config() -> None:
    """Hot-reload configuration after register_project edits projects.yaml.

    load_config() is lru_cached and this module captured `cfg` at import; a
    freshly registered project would otherwise be rejected by every tool until
    the client restarts the server -- the exact papercut register_project
    exists to remove.
    """
    global cfg
    load_config.cache_clear()
    cfg = load_config()
    searcher.cfg = cfg
    memory_store.cfg = cfg


def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False)


def _results(results) -> str:
    if not results:
        return "No results. Try broader wording, or drop filters."
    return _dump([r.to_dict() for r in results])


def _is_connection_error(exc: BaseException) -> bool:
    """Is this exception (or anything in its cause/context chain) a transport
    failure, i.e. 'the database is unreachable' rather than a real bug?

    Typed checks first -- qdrant-client wraps httpx, whose transport errors
    (ConnectError, ConnectTimeout, ReadTimeout...) all derive from
    httpx.TransportError, and OS-level failures are ConnectionError/TimeoutError.
    The substring test survives only as a last resort for wrapper exceptions
    whose chain was severed; it no longer fires on arbitrary messages that
    merely contain the word 'connection'.
    """
    import httpx as _httpx

    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        # Deliberately NOT bare OSError: a permission error on some file must
        # not masquerade as "the database is down".
        if isinstance(e, _httpx.TransportError | ConnectionError | TimeoutError):
            return True
        e = e.__cause__ or e.__context__
    text = str(exc).lower()
    return "connection refused" in text or "failed to connect" in text or "timed out" in text


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
            if _is_connection_error(exc):
                return (
                    "SERVICE DOWN (Qdrant): cannot reach the vector database. "
                    "Docker Desktop is probably not running. "
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
        f"Registered: {', '.join(cfg.projects)}. "
        f"If this is a new (or broken) project, register_project creates or "
        f"repairs its knowledge base in one call."
    )


# ---------------------------------------------------------------------- search
_FRESH_SYNC_WAIT = 12.0  # seconds a search may spend re-syncing stale hits
# A file that keeps failing to index would make EVERY search re-sync and wait.
# After one attempt for a given set of stale files, later searches within the
# cooldown just flag them (and nudge a background sync) instead of waiting.
_FRESH_COOLDOWN = 120.0
_FRESH_TRIED: dict[str, tuple[float, frozenset]] = {}


def _fresh_search(collection: str, query: str, project: str, *, limit: int,
                  filters: dict, expand: bool) -> str:
    """Search, verify the hits against the files on disk, and if any are
    stale re-sync the project and search again -- so agents no longer need to
    remember sync_index, and stop acting on code that has since changed."""
    root = cfg.projects[project].root

    def run():
        res = searcher.search(collection, query, project=project, limit=limit,
                              filters=filters, expand=expand)
        return res, searcher.mark_freshness(res, root)

    results, stale = run()
    note = None
    stale_paths = frozenset(r.source_path for r in results if r.extra.get("stale"))
    tried = _FRESH_TRIED.get(project)
    if stale and tried and tried[1] == stale_paths and time.time() - tried[0] < _FRESH_COOLDOWN:
        _start_sync(project)  # background only; do not make this search wait again
        note = (f"{stale} result(s) come from files edited since indexing (marked `stale`); "
                f"a re-sync just ran without clearing them -- read those files directly.")
    elif stale:
        _FRESH_TRIED[project] = (time.time(), stale_paths)
        job = _start_sync(project)
        if not job["thread"].is_alive():
            # A job that had ALREADY finished (uncollected) predates these
            # edits: collect it and start a fresh one.
            with _SYNC_LOCK:
                if _SYNC_JOBS.get(project) is job:
                    _sync_finish(project, job)
            job = _start_sync(project)
        job["thread"].join(timeout=_FRESH_SYNC_WAIT)
        if job["thread"].is_alive():
            note = (f"{stale} result(s) come from files edited since indexing (marked "
                    f"`stale`). A re-sync is running in the background; read those "
                    f"files directly before relying on them.")
        else:
            with _SYNC_LOCK:
                if _SYNC_JOBS.get(project) is job:
                    _sync_finish(project, job)
            results, stale = run()
            if stale:
                _start_sync(project)  # the job we waited on may have predated the edit
            note = ("Files had changed since indexing; the project was re-synced and "
                    "these results reflect the current files." if not stale else
                    f"Re-synced, but {stale} result(s) are still flagged `stale` (another "
                    f"sync was busy or a file failed) -- read those files directly.")
    if not results:
        return _results(results)
    if note:
        return _dump({"note": note, "results": [r.to_dict() for r in results]})
    return _results(results)


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

    Freshness is handled for you: every returned file is re-hashed, and if any
    changed since it was indexed the project is re-synced and the search re-run
    before answering. A hit still marked `stale` means the sync could not finish
    in time -- read the actual file before relying on it.
    """
    if err := _bad_project(project):
        return err
    return _fresh_search(
        "code", query, project, limit=_clamp(limit, 1, 25),
        filters={"language": language, "symbol_type": symbol_type}, expand=expand,
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
    return _fresh_search(
        "docs", query, project, limit=_clamp(limit, 1, 25),
        filters={"doc_type": doc_type}, expand=expand,
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
    implementation, design, fix, recipe, vendor. Superseded versions are
    excluded unless include_superseded=true. Without a type, "how do I ..." /
    "recipe" queries put matching recipes first and error queries put matching
    fixes first.

    A memory is a claim from the past, not ground truth: each result carries
    age_days. If one disagrees with the code, CLAUDE.md or a newer memory,
    believe the newer source and fix the memory (update_memory/retire_memory).
    """
    if err := _bad_project(project):
        return err
    filters: dict[str, Any] = {"memory_type": memory_type}
    if not include_superseded:
        filters["status"] = "active"
    limit = _clamp(limit, 1, 25)
    wanted = None if memory_type else intent_types(query)
    if wanted:
        # Search a wider pool once and lift the wanted type to the front. A
        # second type-filtered search would always return SOMETHING of that
        # type, relevant or not; staying inside one pool keeps relevance.
        pool = searcher.search("memory", query, project=project,
                               limit=max(limit * 2, INTENT_POOL), filters=filters)
        results = prefer_types(pool, wanted, limit)
    else:
        results = searcher.search("memory", query, project=project,
                                  limit=limit, filters=filters)
    _label_memories(results)
    return _results(results)


def _label_memories(results) -> None:
    """Attach the trust signals a reader needs next to each memory result."""
    from datetime import UTC, datetime

    from .briefing import _age_days

    now = datetime.now(UTC)
    today = now.date().isoformat()
    ids = [r.id for r in results]
    payloads = {}
    if ids:
        try:
            for pt in memory_store.client.retrieve(
                    collection_name="memory", ids=ids,
                    with_payload=["suspect", "evidence", "verified_at", "recheck_after",
                                  "source_url", "read_at", "error_signature", "depends_on"]):
                payloads[str(pt.id)] = pt.payload or {}
        except Exception as exc:
            print(f"label lookup failed: {exc}", file=sys.stderr)
    for r in results:
        age = _age_days(r.extra.get("created_at"), now)
        if age is not None:
            r.extra["age_days"] = age
        p = payloads.get(r.id, {})
        for key in ("evidence", "source_url", "read_at", "error_signature"):
            if p.get(key):
                r.extra[key] = p[key]
        if p.get("verified_at"):
            r.extra["verified_days_ago"] = _age_days(p["verified_at"], now)
        if p.get("depends_on"):
            r.extra["depends_on"] = [d.get("ref") for d in p["depends_on"]]
        if p.get("suspect"):
            r.extra["SUSPECT"] = (f"{p['suspect']} -- check the code before trusting this, "
                                  f"then confirm_memory / update_memory / retire_memory")
        if p.get("recheck_after"):
            r.extra["recheck_after"] = str(p["recheck_after"])[:10]
            if str(p["recheck_after"])[:10] < today:
                r.extra["EXPIRED"] = "past its recheck date -- re-read the source first"


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
    supersedes: str | None = None,
    depends_on: list[str] | None = None,
    evidence: str | None = None,
    source_url: str | None = None,
    recheck_after: str | None = None,
    error_signature: str | None = None,
) -> str:
    """Store a piece of durable development knowledge for future sessions.

    Use this after completing significant work: an architecture decision (and
    its WHY), a bug root cause, a feature summary, an API contract, deployment
    findings. Write it self-contained -- a future session has no context from
    this conversation. Do NOT store conversation transcripts, trivia, or
    anything derivable by reading the code; store conclusions.

    memory_type: decision | feature | api | bug | deployment | implementation | design
      | fix (an error and its fix -- set error_signature to the distinctive error
        text; lookup_error finds it next time)
      | recipe (an exact command sequence that WORKED -- deploy, git, test runs)
      | vendor (a third-party price/rate/API behaviour -- requires source_url and
        evidence; expires after 30 days unless recheck_after says otherwise).
    Keep content focused (40-8000 chars). Add 2-5 lowercase tags for browsing.
    Returns the stored memory's id -- reference it in update/delete.

    depends_on: repo paths (optionally path::Symbol) this knowledge is ABOUT.
    When one changes, the memory is automatically flagged suspect -- give it for
    any memory describing how specific code works.
    evidence: tested | read-at-source | reported | inferred -- how you know.
    recheck_after: ISO date or a number of days after which it must be re-verified.
    Writing "supersedes D-123" (any ID-123 form) in the content automatically
    retires the active memory whose title carries that id.

    If a very similar active memory already exists the store is rejected with
    its id -- update_memory that id if yours replaces it, or retry with
    allow_duplicate=true only if the two are genuinely distinct facts.

    supersedes: id of an existing memory this one REPLACES (e.g. a new decision
    that reverses an old one). The old one is marked superseded in the same
    call, so no stale version is left active.

    The response lists `related` active memories on the same topic. READ THEM:
    if your new memory makes any of them wrong, retire_memory or update_memory
    it now -- this is how stale facts stop outliving the change that killed them.
    """
    if supersedes:
        try:
            supersedes = memory_store.resolve_id(supersedes)
            old = memory_store._get(supersedes)
        except MemoryError as exc:
            return f"REJECTED: {exc}"
        if (old.payload or {}).get("project") != project:
            return "REJECTED: supersedes must name a memory in the same project."
        if (old.payload or {}).get("status") != "active":
            return f"REJECTED: memory {supersedes} is already superseded."
    try:
        m = memory_store.store(
            project=project, memory_type=memory_type, title=title,
            content=content, tags=tags, allow_duplicate=allow_duplicate,
            supersedes=supersedes, depends_on=depends_on, evidence=evidence,
            source_url=source_url, recheck_after=recheck_after,
            error_signature=error_signature,
        )
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    out: dict[str, Any] = {"stored": m.id, "title": m.title, "type": m.memory_type,
                           "project": m.project}
    auto = m.auto_superseded or {}
    if auto.get("retired"):
        out["auto_superseded"] = auto["retired"]
    if auto.get("candidates"):
        out["supersede_candidates"] = auto["candidates"]
        out["supersede_note"] = ("Several active memories start with the id you said this "
                                 "replaces, so none was retired automatically. Retire the "
                                 "right one with retire_memory(id, reason, replaced_by).")
    if supersedes:
        memory_store.client.set_payload(
            collection_name="memory", payload={"superseded_by": m.id},
            points=[supersedes], wait=True,
        )
        out["superseded"] = supersedes
    if m.related:
        out["related"] = m.related
        out["check"] = ("These active memories cover the same topic. If the one you just "
                        "stored makes any of them outdated, retire_memory(id, reason, "
                        "replaced_by) or update_memory it now.")
    return _dump(out)


@app.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))  # noqa: E501
@_guard
def update_memory(
    memory_id: str,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    depends_on: list[str] | None = None,
    evidence: str | None = None,
    source_url: str | None = None,
    recheck_after: str | None = None,
    error_signature: str | None = None,
) -> str:
    """Revise an existing memory when it is outdated or wrong.

    The old version is kept and marked superseded (history is preserved, like
    an ADR trail); a new active version is created and its NEW id returned.
    Changing ONLY the title and/or tags keeps the same id (the knowledge did
    not change; references to the id stay valid).
    Provide only the fields that change. If you only have a vague recollection
    of the memory, find its exact id via list_memories or search_memory first.
    """
    try:
        m = memory_store.update(memory_id, title=title, content=content, tags=tags,
                                depends_on=depends_on, evidence=evidence,
                                source_url=source_url, recheck_after=recheck_after,
                                error_signature=error_signature)
    except MemoryError as exc:
        return f"REJECTED: {exc}"
    if m.id == memory_store.resolve_id(memory_id):
        return _dump({"updated_in_place": m.id, "title": m.title,
                      "note": "Only the title/tags changed, so the id is unchanged."})
    return _dump({"superseded": memory_id, "new_id": m.id, "title": m.title})


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def get_memory(memory_id: str) -> str:
    """Fetch ONE memory's FULL content and metadata by id (full, or the 8-char short form).

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


@app.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
@_guard
def retire_memory(memory_id: str, reason: str, replaced_by: str | None = None) -> str:
    """Mark a memory as no longer true, WITHOUT writing a replacement.

    Use when a memory is outdated and there is nothing to rewrite it into --
    the feature was removed, the decision was reversed by another memory
    (pass that memory's id as replaced_by), the file it describes is gone.
    Non-destructive: the content and your reason stay in history
    (include_superseded=true shows it); it just stops appearing as current
    knowledge in searches and briefings. Prefer this over delete_memory.
    Ids may be the 8-character short form shown in briefings and reviews.
    """
    try:
        return _dump(memory_store.retire(memory_id, reason=reason, replaced_by=replaced_by))
    except MemoryError as exc:
        return f"REJECTED: {exc}"


@app.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))  # noqa: E501
@_guard
def confirm_memory(memory_id: str) -> str:
    """Record that you checked a flagged memory against the current code/docs
    and it is STILL TRUE. review_memories and briefings stop flagging it
    (until an even newer memory on the same topic appears)."""
    try:
        return _dump(memory_store.confirm(memory_id))
    except MemoryError as exc:
        return f"REJECTED: {exc}"


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def review_memories(project: str, limit: int = 15) -> str:
    """List active memories that are PROBABLY STALE, with the reason for each.

    Flags: a newer memory says it replaces this one's id (or restates it
    almost word for word), its own title says SUPERSEDED while it is still
    active, a file it depends on changed, its recheck date passed, it names
    repo files that no longer exist, or an operational note is old and never
    re-confirmed. Merely sharing a topic (e.g. parts of one series) is not a
    flag. For each one, check it against the code /
    CLAUDE.md, then: update_memory (rewrite it), retire_memory (no longer
    true), or confirm_memory (still true). Run it when the briefing shows
    'Check before trusting', or after a big change lands.
    """
    if err := _bad_project(project):
        return err
    items = memory_store.review(project, root=cfg.projects[project].root,
                                limit=_clamp(limit, 1, 50))
    if not items:
        return "No likely-stale memories found."
    return _dump({"count": len(items), "items": items,
                  "next": "For each: update_memory, retire_memory, or confirm_memory."})


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
    items, page = memory_store.list_memories(
        project=project, memory_type=memory_type,
        include_superseded=include_superseded,
        # offset is bounded too: the pagination over-fetch reads offset+limit
        # rows, so an absurd offset would drag the whole collection through
        # one scroll.
        limit=_clamp(limit, 1, 100), offset=_clamp(offset, 0, 10_000),
        with_page=True,
    )
    if not items and offset == 0:
        return "No memories stored yet."
    return _dump({
        "total": page["total"],
        "count": len(items),
        "offset": page["offset"],
        "has_more": page["has_more"],
        "items": items,
    })


# ---------------------------------------------------------------- error fixes
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def lookup_error(error: str, project: str | None = None, limit: int = 5) -> str:
    """Has this error been seen and fixed before? CALL THIS FIRST when a
    command, test or CI step fails, before investigating.

    Paste the distinctive part of the error (a message, code, or the last line
    of a traceback). Exact/substring matches against recorded error signatures
    rank first, then semantic matches among stored fixes. When you fix a NEW
    error, store it: store_memory(memory_type='fix', error_signature=<error
    text>, content=<cause + fix>) so the next agent gets it in one step.
    """
    if err := _bad_project(project):
        return err
    import re as _re

    from qdrant_client import models as qm

    def norm(t: str) -> str:
        t = _re.sub(r"0x[0-9a-f]+|\b\d+\b", "#", t.lower())
        return " ".join(_re.sub(r"[\\/][\w.\\/-]+", "/PATH", t).split())

    needle = norm(error)[:400]
    if len(needle) < 4:
        return "REJECTED: paste the distinctive part of the error (a message or code)."
    must = [qm.FieldCondition(key="memory_type", match=qm.MatchValue(value="fix")),
            qm.FieldCondition(key="status", match=qm.MatchValue(value="active"))]
    if project:
        must.append(qm.FieldCondition(key="project", match=qm.MatchValue(value=project)))
    points, _ = memory_store.client.scroll(
        collection_name="memory", scroll_filter=qm.Filter(must=must), limit=2000,
        with_payload=True, with_vectors=False)
    exact = []
    for pt in points:
        sig = norm((pt.payload or {}).get("error_signature") or "")
        # sig-in-needle: the stored signature appears in the pasted output.
        # needle-in-sig only for a needle long enough to be distinctive --
        # "error" is a substring of almost every signature.
        if sig and (sig in needle or (len(needle) >= 12 and needle in sig)):
            pl = pt.payload or {}
            exact.append({"id": str(pt.id), "match": "error signature",
                          "project": pl.get("project"), "title": pl.get("title"),
                          "error_signature": pl.get("error_signature"),
                          "fix": pl.get("content")})
    semantic = searcher.search("memory", error, project=project, limit=_clamp(limit, 1, 10),
                               filters={"memory_type": "fix", "status": "active"})
    seen = {e["id"] for e in exact}
    sem = [dict(r.to_dict(), match="similar") for r in semantic
           if r.id not in seen and r.extra.get("rerank", 1) >= 0.25]
    if not exact and not sem:
        return ("No known fix recorded for this error. Investigate; once fixed, store it with "
                "store_memory(memory_type='fix', error_signature=..., content=cause + fix).")
    return _dump({"known_fixes": exact[:limit], "similar": sem[:limit]})


# ------------------------------------------------------------------ repo facts
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def repo_facts(project: str, topic: str | None = None) -> str:
    """Facts agents get wrong when they guess, read live from the repo:
    the alembic head (use as down_revision), the CI gate list, the next free
    decision/ID number per configured sequence (plus pending reservations),
    configured constants (e.g. append-only tables) and cross-file checks (e.g.
    which rows every alarm code needs, and any currently missing).

    topic narrows the answer: alembic | ci | ids | constants | checks | notes,
    or the name of one configured constant/check. Never guess these -- and
    never take an ID number from here without reserve_id.
    """
    if err := _bad_project(project):
        return err
    from . import facts as _facts
    from . import localdb

    proj = cfg.projects[project]
    out = _facts.collect(
        proj, topic,
        reservations_fn=lambda seq, above: localdb.reservations(project, seq, above))
    return _dump(out)


@app.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
@_guard
def reserve_id(project: str, sequence: str, agent: str = "", note: str = "") -> str:
    """Atomically take the next free number in an ID sequence (e.g. the next
    decision number D-791) so parallel agents never collide.

    Computes the highest number already used in the repo, then reserves the
    next one above it AND above every earlier reservation -- in one locked
    step shared by every agent on this machine. Say who you are (agent) and
    what it is for (note); use the returned id, do not compute your own.
    """
    if err := _bad_project(project):
        return err
    from . import facts as _facts
    from . import localdb

    proj = cfg.projects[project]
    try:
        floor = _facts.sequence_floor(proj, sequence)
    except KeyError:
        known = list((proj.facts or {}).get("id_sequences") or {})
        return (f"REJECTED: no id sequence {sequence!r} configured for {project}. "
                f"Configured: {', '.join(known) or 'none'} (add one under facts.id_sequences "
                f"in config/projects.yaml).")
    number = localdb.reserve(project, sequence, floor, agent=agent, note=note)
    ident = _facts.sequence_format(proj, sequence).format(n=number)
    return _dump({"reserved": ident, "highest_in_repo_was": floor,
                  "note": "Yours. Write it into the repo soon; the reservation keeps other "
                          "agents off it meanwhile."})


# ---------------------------------------------------------------- test mapping
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def tests_for(project: str, path: str, start_line: int | None = None,
              end_line: int | None = None) -> str:
    """Which tests cover this file (or these lines)? Use BEFORE running a slow
    suite: run just these tests to check a change in minutes, not hours.

    Answered from per-test coverage data (rememory ingests the project's
    .coverage automatically after each run recorded with per-test contexts);
    without that it falls back to a clearly-labelled heuristic (tests that
    import the module or share its name) and tells you how to record contexts.
    path is repo-relative, e.g. apps/api/engine/thinnest.py.
    """
    if err := _bad_project(project):
        return err
    from . import testmap

    proj = cfg.projects[project]
    testmap.auto_ingest(project, proj.root)
    from .symbols import inside

    if not inside(proj.root, path):
        return f"REJECTED: {path!r} is not inside {project}'s root; pass a repo-relative path."
    return _dump(testmap.tests_for(project, proj.root, path, start_line, end_line))


# ------------------------------------------------------------- symbol lookup
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def get_symbol(project: str, name: str, path: str | None = None,
               include_callers: bool = True, max_lines: int = 250) -> str:
    """Exactly one function/class/method: its current body with line numbers,
    who calls it, what it raises and what it calls -- instead of reading the
    whole file.

    name: the symbol ('lend_platform_line', 'VoiceRouter', 'VoiceRouter.route').
    path: optional repo-relative file to disambiguate. The body is read LIVE
    from disk (fresh even before the index syncs). Use the returned line range
    for edits; callers are attributed to their enclosing function.
    """
    if err := _bad_project(project):
        return err
    from pathlib import Path as _Path

    from qdrant_client import models as qm

    from . import symbols

    proj = cfg.projects[project]
    root = _Path(proj.root)
    last = name.split(".")[-1]
    must = [qm.FieldCondition(key="project", match=qm.MatchValue(value=project))]
    if path:
        must.append(qm.FieldCondition(key="source_path",
                                      match=qm.MatchValue(value=path.replace("\\", "/"))))
    candidates: list[tuple[str, int | None]] = []
    for sym in dict.fromkeys([name, last]):
        pts, _ = searcher.client.scroll(
            collection_name="code",
            scroll_filter=qm.Filter(must=must + [qm.FieldCondition(
                key="symbol_name", match=qm.MatchValue(value=sym))]),
            limit=20, with_payload=["source_path", "start_line", "symbol_name"],
            with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            candidates.append((pl.get("source_path"), pl.get("start_line")))
    if not candidates:
        hits = searcher.search("code", name, project=project, limit=10, rerank=False,
                               filters={"source_path": path.replace("\\", "/")} if path else None)
        for r in hits:
            if r.symbol and (r.symbol == name or r.symbol.split(".")[-1] == last):
                candidates.append((r.source_path, r.start_line))
    if path and not candidates:
        candidates.append((path.replace("\\", "/"), None))
    seen, defs = set(), []
    for rel, line in candidates:
        if not rel or rel in seen:
            continue
        seen.add(rel)
        span = symbols.locate(root, rel, last, line)
        if span:
            defs.append((rel, span))
    if not defs:
        return (f"No definition of {name!r} found in {project}. Try search_code for a fuzzy "
                f"match, or pass path= if you know the file.")
    out: dict[str, Any] = {"definitions": []}
    for rel, (a, b) in defs[:3]:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        body_lines = lines[a - 1:b]
        shown = body_lines[:max(20, min(max_lines, 1000))]
        body = "\n".join(f"{a + i:>5}  {t}" for i, t in enumerate(shown))
        entry: dict[str, Any] = {"location": f"{rel}:{a}-{b}", "lines": b - a + 1,
                                 "body": body}
        if len(shown) < len(body_lines):
            entry["truncated"] = f"showing {len(shown)} of {len(body_lines)} lines"
        entry.update(symbols.analyse_body("\n".join(body_lines), rel.endswith(".py"), name))
        if include_callers:
            entry["callers"] = symbols.callers(root, name, exclude=(rel, a, b))
        out["definitions"].append(entry)
    if len(defs) > 3:
        out["more_definitions"] = [f"{r}:{s[0]}" for r, s in defs[3:10]]
    return _dump(out)


# ------------------------------------------------------------------ file claims
@app.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
@_guard
def claim_files(project: str, agent: str, paths: list[str], note: str = "",
                ttl_minutes: int = 120, force: bool = False) -> str:
    """Claim files/folders you are about to edit so parallel agents in the same
    tree stay off them. CALL BEFORE EDITING when other agents may be working.

    agent: a stable name for yourself ('trial-agent', 'emails-agent'). paths:
    repo-relative files or folders (a folder covers everything below it).
    Paths already held by another agent come back as `conflicts` and are NOT
    taken -- wait, pick other work, or coordinate; force=true only with the
    user's say-so. Claims expire after ttl_minutes; release_files when done.
    """
    if err := _bad_project(project):
        return err
    if not agent.strip():
        return "REJECTED: give agent a stable name so others know who holds the claim."
    from . import localdb

    res = localdb.claim(project, agent.strip(), paths, note=note,
                        ttl_minutes=_clamp(ttl_minutes, 5, 24 * 60), force=force)
    return _dump(res)


@app.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))  # noqa: E501
@_guard
def release_files(project: str, agent: str, paths: list[str] | None = None) -> str:
    """Release your file claims when the edit is done (all of yours if paths
    is omitted). Do this before finishing so others are not blocked."""
    if err := _bad_project(project):
        return err
    from . import localdb

    return _dump({"released": localdb.release(project, agent.strip(), paths)})


@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def list_claims(project: str, paths: list[str] | None = None) -> str:
    """Who is editing what right now. Pass paths to ask about specific files
    (e.g. before stopping a run or touching a shared module)."""
    if err := _bad_project(project):
        return err
    from . import localdb

    claims = localdb.list_claims(project, paths)
    return _dump({"claims": claims}) if claims else "No active claims."


# ---------------------------------------------------------------- registration
@app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
@_guard
def find_project(path: str) -> str:
    """Which knowledge base belongs to this directory? CALL THIS AT SESSION
    START with the working directory, before registering anything.

    If the directory (or a parent of it) is already registered, you get the
    project name to use with every other tool -- continue with get_briefing;
    do NOT register again. If nothing matches, the directory has no knowledge
    base yet -- offer the user register_project.
    """
    from pathlib import Path as _Path

    try:
        target = _Path(path).expanduser().resolve()
    except OSError:
        return f"REJECTED: cannot resolve path {path!r}."

    for name, proj in cfg.projects.items():
        try:
            root = proj.root.resolve()
        except OSError:
            continue
        if target == root or root in target.parents:
            return _dump({
                "project": name,
                "root": str(root),
                "description": proj.description,
                "next": f"Call get_briefing(project='{name}') to load its context "
                        f"-- this directory's knowledge base already exists.",
            })
    return (
        f"NOT REGISTERED: no knowledge base covers {target}. "
        f"Registered projects: {', '.join(cfg.projects)}. "
        f"Offer the user to create one with register_project."
    )



@app.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))  # noqa: E501
@_guard
def register_project(
    name: str,
    root: str,
    description: str,
) -> str:
    """Create (or repair) a rememory knowledge base for a project.

    Call this when the user says anything like "create a knowledge base for
    this project in rememory", "add this project to rememory", or when a
    project you are working on is not in the registered list -- offer it.
    Also the one-step FIX when a project's registration is broken or its
    folder moved: re-registering with the correct root repairs it and
    re-indexes.

    name: short lowercase slug (letters/digits/hyphens), e.g. "my-app".
      This becomes the `project` argument for every other tool.
    root: ABSOLUTE path to the project folder (e.g. the session's working
      directory).
    description: 1-2 sentences on what the project is -- shown in briefings.

    Idempotent: registering an existing name updates its details and
    re-indexes incrementally. Registration + first index happen immediately;
    afterwards the background sync keeps the project current automatically.
    """
    import re as _re

    import yaml as _yaml

    slug = name.strip().lower()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", slug):
        return ("REJECTED: name must be a short lowercase slug "
                "(letters, digits, hyphens), e.g. 'my-app'.")
    from pathlib import Path as _Path

    root_path = _Path(root).expanduser()
    if not root_path.is_absolute():
        return f"REJECTED: root must be an absolute path, got {root!r}."
    if not root_path.is_dir():
        return (f"REJECTED: {root_path} is not an existing directory. "
                f"Pass the project's real folder (usually the session's cwd).")
    if not description.strip():
        return "REJECTED: give a 1-2 sentence description (shown in briefings)."

    # Duplicate-root guard: one directory, one knowledge base. Registering the
    # same folder under a second name would split its memories and index
    # across two projects and double every sync.
    resolved = root_path.resolve()
    for existing_name, proj in cfg.projects.items():
        if existing_name != slug and proj.root.resolve() == resolved:
            return (
                f"REJECTED: this directory is already registered as "
                f"{existing_name!r}. Use that project (get_briefing/"
                f"search_code with project='{existing_name}') instead of "
                f"registering it twice."
            )

    registry = ROOT_CONFIG / "projects.yaml"
    data = _yaml.safe_load(registry.read_text(encoding="utf-8")) if registry.exists() else None
    data = data or {}
    projects = data.get("projects") or {}
    existed = slug in projects
    previous = projects.get(slug) or {}
    projects[slug] = {
        "root": str(root_path),
        "description": description.strip(),
        "extra_ignore_dirs": previous.get("extra_ignore_dirs", []),
        "include_only": previous.get("include_only", []),
    }
    # Keep any hand-written `facts:` block -- re-registering must not wipe it.
    if previous.get("facts"):
        projects[slug]["facts"] = previous["facts"]
    data["projects"] = projects
    registry.write_text(
        "# rememory project registry (yours; gitignored).\n"
        "# Managed by the register_project tool -- hand-editing also works.\n"
        + _yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    _reload_config()

    # First index right away, under the shared writer lock.
    from indexer.embedder import Embedder as _Embedder
    from indexer.lockfile import index_lock
    from indexer.pipeline import Pipeline as _Pipeline
    from indexer.store import Store as _Store

    with index_lock() as acquired:
        if not acquired:
            return _dump({
                "registered": slug,
                "updated_existing": existed,
                "indexed": False,
                "note": "Another sync is running; the background sync will index "
                        "this project within 30 minutes, or call sync_index shortly.",
            })
        store = _Store(cfg)
        store.verify()
        with _Embedder(cfg.embedding) as embedder:
            embedder.health()
            stats = _Pipeline(cfg).index_project(
                cfg.projects[slug], store, embedder, only_changed=True
            )

    from .aftersync import after_sync
    from .hooks import install_git_hooks

    after_sync(cfg, slug, stats.changed_paths, memory_store)
    try:
        hooks_status = install_git_hooks(slug, root_path)
    except Exception as exc:  # never fail a registration over automation
        hooks_status = f"git hooks not installed: {exc}"

    return _dump({
        "registered": slug,
        "updated_existing": existed,
        "root": str(root_path),
        "files_indexed": stats.files_indexed,
        "chunks": stats.chunks,
        "secrets_redacted": stats.secrets_redacted,
        "failed": stats.files_failed,
        "auto_sync": hooks_status,
        "note": f"Knowledge base ready. Use project='{slug}' with search_code/"
                f"search_docs/store_memory/get_briefing. Background sync keeps "
                f"it current from here.",
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
def get_briefing(project: str, budget: int = 5000) -> str:
    """Compact project context -- CALL THIS ONCE AT SESSION START when beginning
    work on a registered project (skip it if a "rememory briefing" block was
    already injected into this session automatically).

    Returns, within ~budget characters (default 5000, max 40000): the latest
    handoff with its open tasks and files to read first, decisions added or
    retired since that handoff, warnings (index behind the last commit,
    memories that are suspect or probably replaced, files other agents have
    claimed), key repo facts (migration head, next free decision number), the
    10 newest active decisions as excerpts, and titles of verified recipes.
    It is an index, not an archive: follow up with get_memory / search_memory.
    """
    if err := _bad_project(project):
        return err
    return _briefing_text(project, budget)


def _briefing_text(project: str, budget: int) -> str:
    from . import facts as _facts
    from . import localdb
    from .briefing import build_briefing

    proj = cfg.projects[project]
    try:
        review = memory_store.review(project, root=proj.root, limit=10)
    except Exception as exc:  # advisory -- never fail the briefing over it
        print(f"review failed: {exc}", file=sys.stderr)
        review = []
    facts: dict[str, Any] = {}
    try:
        facts.update(_facts.collect(proj, "alembic"))
        facts.update(_facts.collect(
            proj, "ids",
            reservations_fn=lambda seq, above: localdb.reservations(project, seq, above)))
    except Exception as exc:
        print(f"facts failed: {exc}", file=sys.stderr)
    try:
        claims = localdb.list_claims(project)
    except Exception:
        claims = []
    return build_briefing(cfg, searcher.client, memory_store, project, max_chars=budget,
                          review=review, facts=facts, claims=claims)


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


# Background sync bookkeeping. A sync that outlives the client's request
# timeout must not be lost or duplicated: the thread keeps running and the
# next sync_index call collects its result. The lock makes check-then-register
# atomic -- FastMCP can serve tool calls concurrently, and without it two
# near-simultaneous calls could both see "no job" and start two threads.
_SYNC_JOBS: dict[str, dict] = {}
_SYNC_LOCK = threading.Lock()
# Comfortably under the MCP client request timeout, so a slow sync is reported
# as "still running" by us rather than as a timeout error by the client.
_SYNC_GRACE_SECONDS = 20.0


def _sync_job(project: str) -> dict | None:
    return _SYNC_JOBS.get(project)


def _sync_finish(project: str, job: dict) -> dict:
    """Collect a completed job's result and clear it."""
    _SYNC_JOBS.pop(project, None)
    out = dict(job.get("result") or {})
    out.setdefault("project", project)
    out["took_seconds"] = round(time.time() - job["started"], 1)
    return out


@app.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
@_guard
def sync_index(project: str) -> str:
    """Incrementally re-index one project so search reflects the current files.

    Call this when search results look stale -- e.g. you just wrote or edited
    files and want them findable, or memory_system_status shows an old
    last_indexed. Fast: unchanged files are skipped by content hash, chunks of
    deleted files are purged. Typical run is under a second.

    A large first index can take minutes. Rather than hold the connection open
    and risk the client's request timeout, this returns after a short wait with
    the sync still running in the background -- call it again for progress and
    the final counts. It never starts a second sync for the same project.
    """
    if project not in cfg.projects:
        return f"REJECTED: unknown project {project!r}. Registered: {', '.join(cfg.projects)}"
    proj = cfg.projects[project]
    if not proj.root.exists():
        return f"REJECTED: project root does not exist: {proj.root}"

    job = _start_sync(project)

    # Wait briefly: the common case (a warm project with few changes) finishes
    # in well under a second, and answering with the real counts is much more
    # useful than telling the caller to check back.
    thread = job["thread"]
    if thread.is_alive():
        thread.join(timeout=_SYNC_GRACE_SECONDS)
    if thread.is_alive():
        return _dump({
            "project": project,
            "status": "running",
            "elapsed_seconds": round(time.time() - job["started"], 1),
            "note": "Still indexing in the background rather than timing out "
                    "this request. Call sync_index again for the final counts; "
                    "searches already work against whatever has been written "
                    "so far.",
        })
    # Finished (just now, or earlier and uncollected) -- hand back the result.
    with _SYNC_LOCK:
        return _dump(_sync_finish(project, job))


def _start_sync(project: str) -> dict:
    """Find or start the background sync job for a project (never two at
    once). Shared by sync_index and the self-refreshing searches."""
    proj = cfg.projects[project]

    from indexer.pipeline import Pipeline
    from indexer.store import Store

    # One Store (and thus one QdrantClient connection pool) for the server's
    # lifetime. Creating a fresh client per sync_index call in a long-lived
    # process would accumulate unclosed connection pools -- a slow leak.
    global _SYNC_STORE, _SYNC_PIPELINE
    with _SYNC_LOCK:
        if "_SYNC_STORE" not in globals():
            _SYNC_STORE = Store(cfg)
            _SYNC_STORE.verify()
            _SYNC_PIPELINE = Pipeline(cfg)
    store = _SYNC_STORE

    # Same single-writer lock as the CLI and the scheduled task: without it,
    # Claude calling sync_index while the half-hourly sync runs would race
    # delete-then-write on the same points.
    from indexer.lockfile import holder_age_seconds, index_lock

    def _run_sync() -> dict:
        with index_lock() as acquired:
            if not acquired:
                age = holder_age_seconds() or 0.0
                return {
                    "project": project,
                    "busy": True,
                    "note": f"Another index/sync is already running ({age:.0f}s old). "
                            f"The index will be fresh when it finishes -- just retry "
                            f"the search; no action needed.",
                }
            stats = _SYNC_PIPELINE.index_project(
                proj, store, searcher.embedder, only_changed=True
            )
        from .aftersync import after_sync

        follow_up = after_sync(cfg, project, stats.changed_paths, memory_store)
        return {
            **follow_up,
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

    # Find-or-register atomically; the (potentially long) join happens outside
    # the lock so one slow sync never blocks tool calls for other projects.
    with _SYNC_LOCK:
        job = _sync_job(project)
        if job is None:
            job = {"started": time.time(), "result": {}, "done": False}

            def _worker() -> None:
                try:
                    job["result"].update(_run_sync())
                except Exception as exc:  # never let a thread die silently
                    job["result"].update(
                        {"project": project, "error": f"{type(exc).__name__}: {exc}"}
                    )
                finally:
                    job["done"] = True

            thread = threading.Thread(target=_worker, name=f"sync-{project}", daemon=True)
            job["thread"] = thread
            _SYNC_JOBS[project] = job
            thread.start()
    return job


def main() -> None:
    print(f"rememory MCP server starting (projects: {', '.join(cfg.projects)})",
          file=sys.stderr)
    app.run()  # stdio transport


if __name__ == "__main__":
    main()
