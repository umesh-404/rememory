"""MCP server: the bridge between Claude Code and the memory system.

Claude Code speaks MCP (Model Context Protocol) over stdio: it launches this
server as a subprocess and exchanges JSON-RPC messages on stdin/stdout. The
server exposes tools; Claude decides when to call them.

Design constraints that shaped this package:

* stdout belongs to the PROTOCOL. A single stray print() corrupts the JSON-RPC
  stream and kills the session. All human output goes to stderr.
* Reads dominate. Search must be fast and safe; the only write paths are the
  narrow, validated memory tools. Nothing here can touch indexing.
* The `memory` collection is append-mostly: updates supersede rather than
  overwrite, deletes require the exact id. No bulk-delete tool exists at all.

Module map:
    search.py    hybrid dense+sparse retrieval over code/docs/memory
    memories.py  store / update / delete / list for authored knowledge
    server.py    FastMCP wiring: tool definitions and descriptions
"""

# Kept in lockstep with pyproject.toml [project] version -- two disagreeing
# version stamps are worse than none.
__version__ = "0.5.0"
