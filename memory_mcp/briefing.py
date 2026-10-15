"""The session-start briefing, built to a hard character budget.

Why a budget
------------
The first version returned the project description, EVERY active decision in
full, ten other memories and an inventory. On a project with a long decision
trail that came to ~121k characters -- more than a client will accept from one
tool call, so the tool meant to START a session was the one agents learned to
avoid. A briefing is an index, not an archive: it should say where things
stand and where to look, and leave the detail to search_memory/get_memory.

Priority order (each section only gets what the ones above it left over):

1. Where you left off -- the latest session handoff (capped).
2. Freshness warnings -- index behind the last commit, memories that a newer
   memory probably replaced. A stale fact reads as authoritative, so the
   warning outranks the facts themselves.
3. The 10 most recent active decisions, each trimmed to a few sentences.
4. One-line titles of other recent memories.

Shared by the get_briefing MCP tool and the Claude Code SessionStart hook
(scripts/claude_hook.py), so an agent gets the same block either way.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

DEFAULT_BUDGET = 5000
MAX_BUDGET = 40000
TOP_DECISIONS = 10
HANDOFF_SHARE = 0.34      # at most this much of the budget for the handoff
CHANGES_SHARE = 0.12      # "changed since the last session"
WARNINGS_SHARE = 0.12     # "check before trusting"
TITLE_CHARS = 110
DECISION_CHARS = 320      # per-decision excerpt
OTHER_TITLES = 8

NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _age_days(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max((now - dt).days, 0)
    except (TypeError, ValueError):
        return None


def excerpt(text: str, limit: int) -> str:
    """First `limit` chars, cut at a sentence or line boundary when one is
    reasonably close, so excerpts do not end mid-word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", "\n", ", "):
        pos = cut.rfind(sep)
        if pos >= limit * 0.6:
            return cut[: pos + 1].rstrip() + " ..."
    return cut.rsplit(" ", 1)[0] + " ..."


def last_commit_time(root) -> str | None:
    """ISO time of HEAD's commit, or None (not a repo / git missing)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=5, check=False,
            stdin=subprocess.DEVNULL, **NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def last_indexed(client, project: str) -> str | None:
    from qdrant_client import models as qm

    newest = ""
    for coll in ("code", "docs"):
        try:
            points, _ = client.scroll(
                collection_name=coll,
                scroll_filter=qm.Filter(must=[
                    qm.FieldCondition(key="project", match=qm.MatchValue(value=project))
                ]),
                limit=1,
                order_by=qm.OrderBy(key="indexed_at", direction=qm.Direction.DESC),
                with_payload=["indexed_at"],
                with_vectors=False,
            )
        except Exception:
            continue
        for p in points:
            newest = max(newest, (p.payload or {}).get("indexed_at", ""))
    return newest or None


def index_lag_warning(client, project: str, root) -> str | None:
    """One line if the newest commit is newer than the newest indexed chunk."""
    commit = last_commit_time(root)
    indexed = last_indexed(client, project)
    if not commit or not indexed:
        return None
    try:
        c = datetime.fromisoformat(commit)
        i = datetime.fromisoformat(indexed)
        if i.tzinfo is None:
            i = i.replace(tzinfo=UTC)
    except ValueError:
        return None
    if c <= i:
        return None
    mins = int((c - i).total_seconds() // 60)
    return (f"Code index is behind the latest commit by ~{mins} min "
            f"(a sync is triggered automatically; searches re-check the files they return).")


def excerpt_lines(text: str, limit: int) -> str:
    """Like excerpt() but keeps line breaks (lists stay lists)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(chr(10), 1)[0]
    return (cut if len(cut) > limit * 0.5 else text[:limit].rsplit(" ", 1)[0]) + chr(10) + "..."


def _split_handoff(body: str) -> tuple[str, str, str]:
    """save_session writes SUMMARY, then 'NEXT STEPS:' and 'FILES TO READ FIRST:'."""
    summary, steps, files = body, "", ""
    if "FILES TO READ FIRST:" in summary:
        summary, files = summary.split("FILES TO READ FIRST:", 1)
    if "NEXT STEPS:" in summary:
        summary, steps = summary.split("NEXT STEPS:", 1)
    return summary.strip(), steps.strip(), files.strip()


def build_briefing(
    cfg,
    client,
    memory_store,
    project: str,
    *,
    max_chars: int = DEFAULT_BUDGET,
    review: list[dict[str, Any]] | None = None,
    facts: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> str:
    max_chars = max(1500, min(int(max_chars), MAX_BUDGET))
    now = datetime.now(UTC)
    proj = cfg.projects[project]

    everything = memory_store.list_memories(project=project, limit=400, full_content=True,
                                            include_superseded=True)
    active = [m for m in everything if m.get("status") == "active"]
    sessions = [m for m in active if m["memory_type"] == "session"]
    decisions = [m for m in active if m["memory_type"] == "decision"]
    recipes = [m for m in active if m["memory_type"] == "recipe"]
    others = [m for m in active
              if m["memory_type"] not in ("decision", "session", "recipe", "fix")]

    out: list[str] = [f"# {project}"]
    if proj.description:
        out.append(excerpt(proj.description, 300))
    heads = ((facts or {}).get("alembic") or {}).get("heads") or []
    if heads:
        out.append("Alembic head: " + ", ".join(h["revision"] for h in heads)
                   + (" (MULTIPLE HEADS -- merge first)" if len(heads) > 1 else ""))
    for name, seq in list(((facts or {}).get("id_sequences") or {}).items())[:3]:
        out.append(f"Next {name} number: {seq['next_free']} (take it with reserve_id, "
                   f"never by hand)")
    out.append(f"Trimmed to ~{max_chars} chars -- get_memory(id) / search_memory for detail.")

    def used() -> int:
        return sum(len(s) + 1 for s in out)

    def capped(lines: list[str], share: float) -> list[str]:
        """Keep whole lines until the section's share of the budget is spent."""
        kept, size = [], 0
        for ln in lines:
            if size + len(ln) + 1 > max_chars * share and kept:
                kept.append("- ...")
                break
            kept.append(ln)
            size += len(ln) + 1
        return kept

    # 1. Handoff: summary, then open tasks and files as their own lists.
    cutoff = None
    if sessions:
        s = sessions[0]
        cutoff = s.get("created_at")
        age = _age_days(cutoff, now)
        summary, steps, files = _split_handoff(s.get("content") or "")
        cap = int(max_chars * HANDOFF_SHARE)
        steps_txt = excerpt_lines(steps, int(cap * 0.40))
        files_txt = excerpt_lines(files, int(cap * 0.20))
        out += ["", f"## Where you left off ({(cutoff or '')[:16]}"
                + (f", {age}d ago" if age is not None else "") + ")",
                excerpt(summary, max(300, cap - len(steps_txt) - len(files_txt)))]
        if steps_txt:
            out += ["### Open tasks", steps_txt]
        if files_txt:
            out += ["### Read first", files_txt]

    # 2. What changed since that handoff (other agents keep working).
    if cutoff:
        new_dec = [m for m in decisions if (m.get("created_at") or "") > cutoff]
        retired = [m for m in everything if m.get("status") == "superseded"
                   and m["memory_type"] != "session" and (m.get("updated_at") or "") > cutoff]
        if new_dec or retired:
            lines = [f"- NEW decision: {excerpt(m['title'], TITLE_CHARS)} ({m['id'][:8]})"
                     for m in new_dec]
            lines += [f"- RETIRED: {excerpt(m['title'], TITLE_CHARS)} ({m['id'][:8]})"
                      for m in retired]
            out += ["", "## Changed since the last session"] + capped(lines, CHANGES_SHARE)

    # 3. Trust warnings and who else is working.
    warnings: list[str] = []
    lag = index_lag_warning(client, project, proj.root)
    if lag:
        warnings.append(lag)
    for r in (review or [])[:4]:
        warnings.append(f"Possibly stale: {excerpt(r['title'], 80)} ({r['id'][:8]}) "
                        f"-- {excerpt(r['reason'], 120)}")
    if review and len(review) > 4:
        warnings.append(f"...more: review_memories(project='{project}')")
    for c in (claims or [])[:6]:
        warnings.append(f"Claimed by {c['agent']}: {c['path']} ({c['minutes_left']} min left)"
                        + (f" -- {c['note']}" if c.get("note") else ""))
    if warnings:
        out += ["", "## Check before trusting"] + capped([f"- {w}" for w in warnings],
                                                         WARNINGS_SHARE)

    # 4. Top decisions, newest first.
    if decisions:
        out += ["", f"## Active decisions (newest {min(TOP_DECISIONS, len(decisions))} "
                    f"of {len(decisions)} -- binding unless a newer one says otherwise)"]
        shown = 0
        n = min(TOP_DECISIONS, len(decisions))
        room = max_chars - used() - 700  # keep space for titles + rules
        per = max(0, min(DECISION_CHARS, room // max(n, 1) - TITLE_CHARS - 30))
        for m in decisions[:TOP_DECISIONS]:
            line = (f"- {excerpt(m['title'], TITLE_CHARS)} "
                    f"[{(m.get('created_at') or '')[:10]}, {m['id'][:8]}]"
                    + (f": {excerpt(m.get('content') or '', per)}" if per >= 60 else ""))
            if used() + len(line) > max_chars - 450:
                break
            out.append(line)
            shown += 1
        if len(decisions) > shown:
            out.append(f"- ...{len(decisions) - shown} more: search_memory(query, "
                       f"project='{project}', memory_type='decision')")

    # 5. Titles only: recipes first (agents keep reconstructing commands).
    for label, items in (("Verified recipes", recipes), ("Other recent memories", others)):
        if not items or used() > max_chars - 450:
            continue
        out += ["", f"## {label} (titles; get_memory for text)"]
        for m in items[:OTHER_TITLES]:
            line = f"- [{m['memory_type']}] {m['title']} ({m['id'][:8]})"
            if used() + len(line) > max_chars - 350:
                break
            out.append(line)

    out += ["", "## Working rules",
            "- search_code/get_symbol before reading whole files; tests_for before a slow suite.",
            "- lookup_error first when something fails; store fixes as memory_type='fix'.",
            "- A memory contradicted by code/docs is stale: update_memory or retire_memory it.",
            "- claim_files before editing shared files; save_session before wrapping up."]

    text = "\n".join(out)
    if len(text) > max_chars:  # hard guarantee, whatever the sections did
        text = text[: max_chars - 40].rsplit("\n", 1)[0] + "\n... (briefing truncated to budget)"
    return text
