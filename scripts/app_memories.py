"""List or search stored memories as JSON, for the desktop app.

The dashboard's Memories tab runs this through `uv run` in the project
environment: the app process itself deliberately imports neither
qdrant_client nor the retrieval stack (see app/backend.py's design rules),
and running a real script with argv replaces the previous approach of
generating Python source in a string -- user input now crosses the boundary
as an argument, never as code.

Search goes through the real Searcher pipeline so the app sees exactly what
the assistant sees.

Output: '@@' + a JSON array on stdout. The sentinel separates the payload
from any stray stdout an import might produce.
"""

from __future__ import annotations

import argparse
import json
import sys

from indexer.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", default="", help="semantic search; empty = newest first")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()
    limit = max(1, min(args.limit, 100))

    cfg = load_config()
    if args.query.strip():
        from memory_mcp.search import Searcher

        # status filter matches the MCP search_memory tool and the browse path
        # below: without it, the search box surfaced every superseded revision
        # next to its replacement, indistinguishable in the UI.
        hits = Searcher(cfg).search(
            "memory", args.query, limit=limit, rerank=False,
            filters={"status": "active"},
        )
        out = [
            {
                "id": h.id,
                "title": h.extra.get("title"),
                "memory_type": h.extra.get("memory_type"),
                "project": h.project,
                "tags": h.extra.get("tags", []),
                "created_at": h.extra.get("created_at"),
                "content": h.content,
                "score": round(h.score, 3),
            }
            for h in hits
        ]
    else:
        from memory_mcp.memories import MemoryStore

        out = MemoryStore(cfg).list_memories(limit=limit, full_content=True)
    sys.stdout.write("@@" + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
