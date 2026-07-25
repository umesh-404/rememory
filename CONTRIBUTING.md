# Contributing

## Setup

Run the installer for your OS (`setup.ps1` / `setup.sh`) — it is idempotent
and leaves you with a working stack. Development happens through `uv`; there
is no venv to activate.

## Tests

```bash
uv run tests/test_unit.py        # fast, no services -- what CI runs
uv run tests/test_roundtrip.py   # end-to-end: Ollama + Qdrant required
uv run tests/test_mcp_server.py  # full MCP server over real stdio
uv run tests/eval_retrieval.py   # retrieval quality scoreboard (fails <80% R@3)
```

A change that touches chunking, sparse vectors, or redaction needs a case in
`tests/test_unit.py`. A change that touches retrieval behavior must keep
`eval_retrieval.py` passing — if your change *should* alter retrieval, update
`config/eval.yaml` and say why in the PR.

## Invariants you must not break

They are documented (and mostly asserted) in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The short list:

- Embedding model, dimensions, prefixes, and collection vector config move
  together; changing any means a full re-index.
- MCP stdout carries JSON-RPC — never `print()` to stdout in server code.
- Sparse tokenization at query time and index time is the same imported
  function.
- The `memory` collection has no bulk-delete path. Keep it that way.
- Retrieval enhancements (rerank, expansion) must fail soft; search never
  fails because an enhancement did.

## Style

Ruff, line length 100 (`uv run ruff check .`). Comments explain *why*, not
*what*. Match the codebase's habit of documenting rejected alternatives next
to the decision.
