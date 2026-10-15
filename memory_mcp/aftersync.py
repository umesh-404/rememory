"""Work that should follow every sync, whoever ran it (MCP tool, CLI,
scheduled task, git hook): re-check memories that depend on changed files,
and pick up a newer per-test coverage run.

Advisory by design -- a failure here is logged to stderr and never fails the
sync that triggered it.
"""

from __future__ import annotations

import sys
from typing import Any


def after_sync(cfg, project_name: str, changed_paths: list[str],
               memory_store=None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    project = cfg.projects.get(project_name)
    if project is None:
        return out
    if changed_paths:
        try:
            if memory_store is None:
                from .memories import MemoryStore

                memory_store = MemoryStore(cfg)
            flagged = memory_store.flag_dependents(project_name, changed_paths)
            if flagged:
                out["memories_flagged_suspect"] = flagged
        except Exception as exc:
            print(f"after_sync: dependency check failed: {exc}", file=sys.stderr)
    try:
        from .testmap import auto_ingest

        ingested = auto_ingest(project_name, project.root)
        if ingested and ingested.get("ingested"):
            out["coverage_ingested"] = ingested
    except Exception as exc:
        print(f"after_sync: coverage ingest failed: {exc}", file=sys.stderr)
    return out
