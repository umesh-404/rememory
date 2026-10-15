"""Install rememory's automation: git hooks in every registered project and
lifecycle hooks in every installed agentic IDE/CLI. Idempotent; safe to
re-run any time (setup and Repair run it for you).

    uv run scripts/install_hooks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer.config import load_config  # noqa: E402
from memory_mcp.hooks import install_agent_hooks, install_git_hooks  # noqa: E402


def main() -> int:
    cfg = load_config()
    print("Git hooks (re-index on commit, merge, checkout, rebase):")
    for name, proj in cfg.projects.items():
        try:
            print("  " + install_git_hooks(name, proj.root))
        except Exception as exc:
            print(f"  {name}: failed: {exc}")
    print("Agent hooks (briefing at session start / after compaction, re-index on edit):")
    lines = install_agent_hooks()
    for line in lines or ["  no supported agent IDE/CLI detected"]:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
