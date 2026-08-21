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

import json
import subprocess
import sys
import time
from pathlib import Path

from indexer.runtime import compose_env, direct_urlopen, qdrant_url

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker" / "compose.yml"
QDRANT_READY = f"{qdrant_url()}/readyz"
QDRANT_COLLECTIONS = f"{qdrant_url()}/collections"
CONTAINER = "rememory-qdrant"
REQUIRED_COLLECTIONS = ("code", "docs", "memory")

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


def _docker(*args: str, timeout: int = 120):
    """Run a docker command, never raise. Returns CompletedProcess or None."""
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True,
            timeout=timeout, check=False, env=compose_env(), **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _container_exists() -> bool:
    """Does the container exist at all (running or not)?"""
    out = _docker("ps", "-a", "--filter", f"name=^{CONTAINER}$",
                  "--format", "{{.Names}}", timeout=30)
    return bool(out and out.returncode == 0 and CONTAINER in out.stdout)


def _compose_up() -> tuple[bool, str]:
    """Create the container from compose: network, image pull, bind mounts.

    This is what makes a wiped Docker self-heal. `docker start` cannot help
    when the container was removed (docker system prune, "clear volumes and
    images", or a brand-new machine) -- compose recreates it, pulling the
    pinned image if it is gone. The data is a BIND MOUNT under data/qdrant,
    not a Docker volume, so an existing index reattaches untouched; deleting
    Docker volumes never had the power to destroy it.

    Generous timeout: a cold image pull is ~100 MB over whatever link the
    user has.
    """
    if not COMPOSE_FILE.exists():
        return False, f"{COMPOSE_FILE} is missing"
    detail = "unknown error"
    # The compose plugin first (`docker compose`), then the standalone
    # docker-compose binary that older installs still have.
    plugin = _docker("compose", "-f", str(COMPOSE_FILE), "up", "-d", timeout=900)
    if plugin is not None and plugin.returncode == 0:
        return True, ""
    if plugin is not None and plugin.stderr.strip():
        detail = plugin.stderr.strip().splitlines()[-1]
    try:
        legacy = subprocess.run(
            ["docker-compose", "-f", str(COMPOSE_FILE), "up", "-d"],
            capture_output=True, text=True, timeout=900, check=False,
            env=compose_env(), **_NO_WINDOW,
        )
        if legacy.returncode == 0:
            return True, ""
        if legacy.stderr.strip():
            detail = legacy.stderr.strip().splitlines()[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return False, detail


def _ensure_collections() -> None:
    """Create any missing collection once Qdrant answers.

    Wiping Docker (or a fresh clone whose data/ is empty) leaves a healthy
    but EMPTY database: every search would then fail with a confusing
    "collection not found" until the user re-ran setup. Creating them here
    closes the loop, so opening the app is all it takes. Existing
    collections are never touched -- create_collections.py is idempotent and
    refuses to recreate `memory`.
    """
    try:
        with direct_urlopen(QDRANT_COLLECTIONS, timeout=10) as resp:
            payload = json.load(resp)
        have = {c["name"] for c in payload["result"]["collections"]}
    except (OSError, ValueError, KeyError):
        return  # cannot tell; leave it alone
    missing = [c for c in REQUIRED_COLLECTIONS if c not in have]
    if not missing:
        return
    print(f"rememory: creating missing collections ({', '.join(missing)})...",
          file=sys.stderr)
    script = ROOT / "scripts" / "create_collections.py"
    if not script.exists():
        return
    try:
        made = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=300, check=False,
            cwd=str(ROOT), **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        made = None
    if made is not None and made.returncode == 0:
        print("rememory: collections ready. Ask your assistant to sync a "
              "project, or run: uv run -m indexer.cli sync", file=sys.stderr)
    else:
        print("rememory: could not create the collections automatically -- "
              "run: uv run scripts/create_collections.py", file=sys.stderr)


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

    # Two different repairs, depending on what is actually missing.
    #
    # Container merely stopped -> `docker start` (fast, the common case).
    # Container GONE -> compose up, which recreates the network, pulls the
    # pinned image if Docker was pruned, and recreates the container against
    # the same bind-mounted data. This is the path that makes "I cleared my
    # Docker volumes and images" (or a brand-new machine) heal by itself,
    # instead of dead-ending at "run setup again".
    if _container_exists():
        print("rememory: starting the local database...", file=sys.stderr)
        started = _docker("start", CONTAINER, timeout=60)
        ok = started is not None and started.returncode == 0
        detail = ("unknown error" if started is None or not started.stderr.strip()
                  else started.stderr.strip().splitlines()[-1])
        if not ok:
            # A container can also disappear between the check and the start
            # (or be in a state `start` refuses); compose can still fix it.
            ok, detail = _compose_up()
    else:
        print("rememory: the database container is missing -- recreating it "
              "(this pulls the database image if needed, and your existing "
              "index and memories are untouched)...", file=sys.stderr)
        ok, detail = _compose_up()

    if not ok:
        print(
            f"rememory: could not bring up the database container ({detail}). "
            f"Run setup.ps1 / setup.sh once, or scripts/diagnose.py to see why.",
            file=sys.stderr,
        )
        return

    for _ in range(20):
        if _qdrant_up():
            print("rememory: ready.", file=sys.stderr)
            _ensure_collections()
            return
        time.sleep(1)
    print("rememory: database is starting slowly; searches may work in a moment.",
          file=sys.stderr)
