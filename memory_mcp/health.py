"""Self-healing startup: fix "rememory isn't running" before anyone notices.

The most user-friendly start button is the one nobody has to press. When a
client session spawns the MCP server, the single most common failure is that
the Qdrant container is stopped (machine rebooted, Docker restarted without
it, someone clicked Stop) while the Docker daemon itself is fine -- and that
case is fixable in one `docker start`. So the server fixes it.

What this deliberately does NOT do: launch Docker Desktop or Ollama
themselves. Both are GUI applications the user chose to run (or not);
force-starting them from a background process is surprising behaviour and
slow (Docker Desktop takes ~30s+). For those cases the tool guard messages
already tell the user exactly what to click, and the Start-menu
"Start rememory" shortcut does it for them.

All output to stderr (stdout is JSON-RPC). Never raises: a failed heal just
leaves things as they were, and the per-tool guards report actionable
messages when actually used.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request

from indexer.runtime import compose_env, qdrant_url

QDRANT_READY = f"{qdrant_url()}/readyz"
CONTAINER = "rememory-qdrant"


def _qdrant_up(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(QDRANT_READY, timeout=timeout):
            return True
    except OSError:
        return False


def ensure_services() -> None:
    """Heal what is cheaply healable; say one friendly line about the rest."""
    if _qdrant_up():
        return

    # Is the Docker daemon itself reachable?
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "ok"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        daemon = None
    if daemon is None or daemon.returncode != 0:
        print(
            "rememory: the vector database is offline because Docker isn't "
            "running. Start Docker Desktop (or use the 'Start rememory' "
            "shortcut) and searches will work.",
            file=sys.stderr,
        )
        return

    print("rememory: starting the local database...", file=sys.stderr)
    try:
        started = subprocess.run(
            ["docker", "start", CONTAINER],
            capture_output=True, text=True, timeout=30, check=False, env=compose_env(),
        )
    except (OSError, subprocess.SubprocessError):
        started = None
    if started is None or started.returncode != 0:
        detail = (started.stderr.strip().splitlines() or ["unknown error"])[-1] if started else ""
        print(
            f"rememory: could not start the database container ({detail}). "
            f"Run setup.ps1 / setup.sh once to recreate it.",
            file=sys.stderr,
        )
        return

    for _ in range(20):
        if _qdrant_up():
            print("rememory: ready.", file=sys.stderr)
            return
        time.sleep(1)
    print("rememory: database is starting slowly; searches may work in a moment.",
          file=sys.stderr)
