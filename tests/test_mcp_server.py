"""End-to-end test of the MCP server over REAL stdio.

Spawns the server exactly the way Claude Code will (a subprocess speaking
JSON-RPC on stdin/stdout) rather than calling tool functions in-process, so it
also catches transport-level failures: stdout pollution, import errors, slow
startup, schema problems.

Works against WHATEVER projects this machine has registered (rememory no
longer indexes its own source, so nothing here may assume a project named
"rememory" exists). Project-bound checks pick a registered project at runtime;
the memory lifecycle is self-cleaning (every memory it stores, it deletes).

Deliberately NOT exercised, because both would mutate real user data:
  * save_session -- it supersedes the project's ACTIVE handoff, and deleting
    the test handoff afterwards would not restore the previous one;
  * register_project -- it rewrites config/projects.yaml.

Exercises the rest of the tool surface, including the memory lifecycle:
store -> get -> search -> update (supersede) -> list history -> delete ->
verify gone.

    uv run tests\\test_mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def note(message: str) -> None:
    print(f"  [SKIP] {message}")


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "memory_mcp.server"],
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---- tool discovery -------------------------------------------
            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {
                "search_code", "search_docs", "search_memory",
                "store_memory", "update_memory", "get_memory", "delete_memory",
                "list_memories", "find_project", "register_project",
                "save_session", "get_briefing", "memory_system_status",
                "sync_index", "retire_memory", "confirm_memory", "review_memories",
                "lookup_error", "repo_facts", "reserve_id", "tests_for", "get_symbol",
                "claim_files", "release_files", "list_claims",
            }
            check(f"all {len(expected)} tools exposed", expected <= tools,
                  ", ".join(sorted(expected - tools)) or ", ".join(sorted(tools)))

            prompts = {p.name for p in (await session.list_prompts()).prompts}
            check("kickoff prompt exposed", "kickoff" in prompts, ", ".join(sorted(prompts)))

            async def call(tool_name: str, /, **args):
                result = await session.call_tool(tool_name, args)
                text = result.content[0].text if result.content else ""
                return text

            # ---- pick projects from this machine's registry ----------------
            status = json.loads(await call("memory_system_status"))
            projects: dict = status.get("projects", {})
            if not projects:
                note("no projects registered -- project-bound checks skipped; "
                     "register one and re-run for full coverage")
                print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
                return 1 if failures else 0

            write_project = next(iter(projects))
            content_project = next(
                (n for n, s in projects.items() if s.get("code", 0) or s.get("docs", 0)),
                None,
            )

            # ---- search over real indexed content -------------------------
            if content_project:
                for tool, coll in (("search_code", "code"), ("search_docs", "docs")):
                    if not projects[content_project].get(coll, 0):
                        note(f"{tool}: {content_project} has no {coll} chunks")
                        continue
                    out = await call(
                        tool,
                        query="where is the main entry point and how is it configured?",
                        project=content_project,
                        limit=3,
                    )
                    check(
                        f"{tool} answers without error",
                        not out.startswith("REJECTED") and "SERVICE DOWN" not in out,
                        out[:90].replace("\n", " "),
                    )
            else:
                note("no project has indexed content -- search checks skipped")

            # ---- validation rejects garbage -------------------------------
            rejected = await call(
                "store_memory",
                project=write_project, memory_type="decision",
                title="too short", content="tiny",
            )
            check("short content rejected", rejected.startswith("REJECTED"), rejected[:70])

            rejected = await call(
                "store_memory",
                project="nope", memory_type="decision",
                title="bad project", content="x" * 100,
            )
            check("unknown project rejected", rejected.startswith("REJECTED"), rejected[:70])

            # ---- find_project resolves a registered root -------------------
            root = projects[write_project].get("root")
            if root:
                found = await call("find_project", path=root)
                check("find_project resolves the root", write_project in found, found[:80])
            # The probe path must live OUTSIDE any registrable root: a path
            # inside this repo would match on a contributor machine where the
            # rememory repo itself is registered.
            import tempfile

            outside = str(Path(tempfile.gettempdir()) / "rememory-no-such-dir-xyz")
            sad = await call("find_project", path=outside)
            check("find_project reports unregistered paths", "NOT REGISTERED" in sad, sad[:80])

            # ---- memory lifecycle (self-cleaning) ---------------------------
            stored = json.loads(await call(
                "store_memory",
                project=write_project,
                memory_type="decision",
                title="MCP test: reranker fallback policy",
                content=(
                    "Test memory from the MCP verification run. Reranking is an "
                    "enhancement, never a dependency: any failure falls back to "
                    "first-stage RRF order so search cannot break. Safe to delete."
                ),
                tags=["test", "adapter"],
            ))
            mem_id = stored["stored"]
            check("store_memory returns id", bool(mem_id))

            full = json.loads(await call("get_memory", memory_id=mem_id))
            check("get_memory returns full content",
                  "falls back" in json.dumps(full), str(full)[:80])

            found = await call(
                "search_memory",
                query="what happens to search when the reranker fails?",
                project=write_project,
            )
            check("search_memory finds it semantically", mem_id in found)

            updated = json.loads(await call(
                "update_memory",
                memory_id=mem_id,
                content=(
                    "REVISED test memory from MCP verification. The reranker "
                    "falls back to RRF order on any failure, and a missing "
                    "model disables it for the session. Safe to delete."
                ),
            ))
            new_id = updated["new_id"]
            check("update supersedes, new id differs", new_id != mem_id)

            active = await call("search_memory", query="reranker fallback RRF failure policy",
                                project=write_project)
            check("superseded version hidden from search", mem_id not in active and new_id in active)

            history = await call("list_memories", project=write_project, include_superseded=True)
            check("history shows both versions", mem_id in history and new_id in history)

            stale = await call("update_memory", memory_id=mem_id, title="should fail")
            check("updating a superseded memory rejected", stale.startswith("REJECTED"), stale[:80])

            renamed = json.loads(await call("update_memory", memory_id=new_id[:8],
                                            title="MCP test: reranker fallback (renamed)",
                                            tags=["mcp-test", "renamed"]))
            check("title/tag-only update keeps the id (short id accepted)",
                  renamed.get("updated_in_place") == new_id, str(renamed)[:120])
            after = json.loads(await call("get_memory", memory_id=new_id))
            check("rename stored, old title kept in previous_titles",
                  after.get("title", "").endswith("(renamed)") and after.get("previous_titles")
                  and after.get("status") == "active", str(after)[:160])
            check("renamed memory still found by search", new_id in await call(
                "search_memory", query="reranker fallback renamed", project=write_project))

            # ---- staleness: dependency + retire + confirm (self-cleaning) ----
            dep_file = None
            if root:
                for cand in ("README.md", "pyproject.toml", "package.json", "CLAUDE.md"):
                    if (Path(root) / cand).exists():
                        dep_file = cand
                        break
            extra_ids: list[str] = []
            if dep_file:
                dep = json.loads(await call(
                    "store_memory", project=write_project, memory_type="implementation",
                    title="MCP test: dependency-tracked note",
                    content=f"Test memory that depends on {dep_file}; safe to delete. "
                            f"It verifies depends_on, evidence and confirm/retire.",
                    depends_on=[dep_file], evidence="read-at-source", allow_duplicate=True,
                ))
                extra_ids.append(dep["stored"])
                got = json.loads(await call("get_memory", memory_id=dep["stored"]))
                check("depends_on recorded with a content hash",
                      got.get("depends_paths") == [dep_file]
                      and bool((got.get("depends_on") or [{}])[0].get("hash")), str(got)[:120])
                conf = await call("confirm_memory", memory_id=dep["stored"])
                check("confirm_memory records verification", "verified_at" in conf, conf[:80])
                ret = await call("retire_memory", memory_id=dep["stored"],
                                 reason="test cleanup: retired by the MCP verification run")
                check("retire_memory marks it superseded", "retired" in ret, ret[:80])

            fix = json.loads(await call(
                "store_memory", project=write_project, memory_type="fix",
                title="MCP test: ZZQ-TEST-ERROR fix",
                content="Test fix memory: ZZQ-TEST-ERROR means the test fixture was not "
                        "reset; reset it. Safe to delete.",
                error_signature="ZZQ-TEST-ERROR 4711 fixture not reset", allow_duplicate=True,
            ))
            extra_ids.append(fix["stored"])
            look = await call("lookup_error",
                              error="Traceback ... ZZQ-TEST-ERROR 9999 fixture not reset",
                              project=write_project)
            check("lookup_error finds the fix by signature (numbers normalised)",
                  fix["stored"] in look, look[:100])
            nofix = await call("store_memory", project=write_project, memory_type="fix",
                               title="no sig", content="x" * 60)
            check("fix without error_signature rejected", nofix.startswith("REJECTED"))
            nov = await call("store_memory", project=write_project, memory_type="vendor",
                             title="no source", content="x" * 60)
            check("vendor fact without source_url rejected", nov.startswith("REJECTED"))

            review = await call("review_memories", project=write_project, limit=3)
            check("review_memories answers", not review.startswith("REJECTED"), review[:80])

            # ---- repo facts, claims, test map, symbols ----------------------
            facts = json.loads(await call("repo_facts", project=write_project))
            check("repo_facts answers", facts.get("project") == write_project, str(facts)[:80])

            agent = "mcp-test-agent"
            claim = json.loads(await call("claim_files", project=write_project, agent=agent,
                                          paths=["zz_mcp_test_dir/"], ttl_minutes=5))
            check("claim_files grants a free path", claim["granted"] == ["zz_mcp_test_dir"])
            clash = json.loads(await call("claim_files", project=write_project,
                                          agent="other-agent",
                                          paths=["zz_mcp_test_dir/file.py"], ttl_minutes=5))
            check("a second agent sees the conflict", bool(clash["conflicts"]), str(clash)[:90])
            listed = await call("list_claims", project=write_project,
                                paths=["zz_mcp_test_dir/x"])
            check("list_claims shows the holder", agent in listed)
            rel = json.loads(await call("release_files", project=write_project, agent=agent))
            check("release_files frees it", rel["released"] >= 1)

            tf = json.loads(await call("tests_for", project=write_project,
                                       path=dep_file or "README.md"))
            check("tests_for answers (coverage or labelled heuristic)",
                  "source" in tf, str(tf)[:90])

            if content_project and projects[content_project].get("code", 0):
                hit = await call("search_code", query="function", project=content_project,
                                 limit=1)
                try:
                    sym = (json.loads(hit) if hit.startswith("[") else
                           json.loads(hit)["results"])[0].get("symbol")
                except (ValueError, KeyError, IndexError):
                    sym = None
                if sym:
                    got = await call("get_symbol", project=content_project,
                                     name=sym.split(".")[-1], include_callers=False)
                    check("get_symbol returns a live body",
                          "definitions" in got or "No definition" in got, got[:90])

            # ---- briefing (read-only, safe on any project) ------------------
            briefing = await call("get_briefing", project=write_project)
            check("get_briefing renders the project header",
                  write_project in briefing, briefing[:80].replace("\n", " "))
            check("get_briefing stays within budget", len(briefing) <= 5000, str(len(briefing)))
            for mid in extra_ids:
                await call("delete_memory", memory_id=mid)

            # ---- cleanup ---------------------------------------------------
            for mid in (mem_id, new_id):
                await call("delete_memory", memory_id=mid)
            gone = await call("list_memories", project=write_project, include_superseded=True)
            check("test memories deleted", mem_id not in gone and new_id not in gone)

            missing = await call("delete_memory", memory_id="00000000-0000-0000-0000-000000000000")
            check("deleting nonexistent id rejected", missing.startswith("REJECTED"))

            # ---- status ----------------------------------------------------
            if content_project:
                s = projects[content_project]
                check(
                    "status reports indexed content",
                    (s.get("code", 0) + s.get("docs", 0)) > 0,
                    f"{content_project}: code={s.get('code', 0)} docs={s.get('docs', 0)}",
                )

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
