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
already tell the user exactly what to click, and the rememory app's Start
button does it for them.

All output to stderr (stdout is JSON-RPC). Never raises: a failed heal just
leaves things as they were, and the per-tool guards report actionable
messages when actually used.
"""

from __future__ import annotations

import subprocess
import sys
import time

from indexer.runtime import compose_env, direct_urlopen, qdrant_url

QDRANT_READY = f"{qdrant_url()}/readyz"
CONTAINER = "rememory-qdrant"

# Never flash a console window when shelling out to docker on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _qdrant_up(timeout: float = 6.0) -> bool:
    """Is Qdrant answering?

    Generous timeout on purpose: with Docker's WSL2 backend the first request
    after an idle period is often slow to be forwarded, and a short limit made
    a healthy database look offline.
    """
    try:
        with direct_urlopen(QDRANT_READY, timeout=timeout):
            return True
    except OSError:
        return False


def ensure_services() -> None:
    """Heal what is cheaply healable; say one friendly line about the rest."""
    if _qdrant_up():
        return

    # Is the Docker daemon itself reachable? `docker info` talks to the daemon
    # and is slow on a cold Docker Desktop -- 30s, because timing out here and
    # declaring Docker down is worse than waiting.
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "ok"],
            capture_output=True, text=True, timeout=30, check=False, **_NO_WINDOW,
        )
        reachable = daemon.returncode == 0
        determined = True
    except subprocess.SubprocessError:
        # Timed out or otherwise inconclusive. We genuinely do not know
        # whether Docker is running, and saying "Docker isn't running" when
        # it is sends the user chasing the wrong problem.
        reachable, determined = False, False
    except OSError:
        reachable, determined = False, True  # docker not installed / not on PATH

    if not reachable:
        if determined:
            print(
                "rememory: the vector database is offline because Docker "
                "isn't running. Start Docker Desktop and searches will work.",
                file=sys.stderr,
            )
        else:
            print(
                "rememory: could not reach the vector database, and Docker "
                "did not respond in time. If Docker Desktop is running, give "
                "it a moment; otherwise run scripts/diagnose.py.",
                file=sys.stderr,
            )
        return

    print("rememory: starting the local database...", file=sys.stderr)
    try:
        started = subprocess.run(
            ["docker", "start", CONTAINER],
            capture_output=True, text=True, timeout=60, check=False,
            env=compose_env(), **_NO_WINDOW,
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
