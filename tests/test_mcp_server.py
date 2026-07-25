"""End-to-end test of the MCP server over REAL stdio.

Spawns the server exactly the way Claude Code will (a subprocess speaking
JSON-RPC on stdin/stdout) rather than calling tool functions in-process, so it
also catches transport-level failures: stdout pollution, import errors, slow
startup, schema problems.

Exercises the full tool surface, including the memory lifecycle:
store -> search -> update (supersede) -> list history -> delete -> verify gone.

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
                "store_memory", "update_memory", "delete_memory",
                "list_memories", "memory_system_status",
            }
            check("all 8 tools exposed", expected <= tools, ", ".join(sorted(tools)))

            async def call(name: str, **args):
                result = await session.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                return text

            # ---- search over real indexed content -------------------------
            docs = await call(
                "search_docs",
                query="which vector database does this system store embeddings in?",
                project="rememory",
                limit=3,
            )
            check("search_docs finds the architecture doc", "drant" in docs, docs[:90].replace("\n", " "))

            code = await call(
                "search_code",
                query="how are file chunks written into the vector database?",
                project="rememory",
                limit=3,
            )
            check("search_code finds store.upsert", "upsert" in code, code[:90].replace("\n", " "))

            # ---- validation rejects garbage -------------------------------
            rejected = await call(
                "store_memory",
                project="rememory", memory_type="decision",
                title="too short", content="tiny",
            )
            check("short content rejected", rejected.startswith("REJECTED"), rejected[:70])

            rejected = await call(
                "store_memory",
                project="nope", memory_type="decision",
                title="bad project", content="x" * 100,
            )
            check("unknown project rejected", rejected.startswith("REJECTED"), rejected[:70])

            # ---- memory lifecycle -----------------------------------------
            stored = json.loads(await call(
                "store_memory",
                project="rememory",
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

            found = await call(
                "search_memory",
                query="what happens to search when the reranker fails?",
                project="rememory",
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
                                project="rememory")
            check("superseded version hidden from search", mem_id not in active and new_id in active)

            history = await call("list_memories", project="rememory", include_superseded=True)
            check("history shows both versions", mem_id in history and new_id in history)

            stale = await call("update_memory", memory_id=mem_id, title="should fail")
            check("updating a superseded memory rejected", stale.startswith("REJECTED"), stale[:80])

            # ---- cleanup ---------------------------------------------------
            for mid in (mem_id, new_id):
                await call("delete_memory", memory_id=mid)
            gone = await call("list_memories", project="rememory", include_superseded=True)
            check("test memories deleted", mem_id not in gone and new_id not in gone)

            missing = await call("delete_memory", memory_id="00000000-0000-0000-0000-000000000000")
            check("deleting nonexistent id rejected", missing.startswith("REJECTED"))

            # ---- status ----------------------------------------------------
            status = json.loads(await call("memory_system_status"))
            check(
                "status reports both projects with content",
                status["projects"]["rememory"]["docs"] > 0
                and status["projects"]["rememory"]["code"] > 0,
            )

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
