"""Self-healing startup: fix "rememory isn't running" before anyone notices.

The most user-friendly start button is the one nobody has to press. When a
client session spawns the MCP server, the single most common failure is that
the Qdrant container is stopped (machine rebooted, Docker restarted without
it, someone clicked Stop) while the Docker daemon itself is fine -- and that
case is fixable in one `docker start`. So the server fixes it.

What this deliberately does NOT do: launch Docker Desktop. It is a heavy GUI
application (~30s+ to start) that the user chose to run or not; force-starting
it from a background process is surprising. The tool guard messages tell the
user what to click, and the rememory app's Start button does it for them.

Ollama IS started, though -- a reversal of the original policy, which lumped
it in with Docker Desktop. The two are not alike: Ollama comes up in ~2s, setup
already starts it unasked, and rememory is completely inert without it (no
embeddings means no search and no indexing). Leaving it down turned an
ordinary reboot into "rememory isn't starting", with nothing in the log to say
why.

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

from indexer.runtime import compose_env, direct_urlopen, ollama_url, qdrant_url

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker" / "compose.yml"
QDRANT_READY = f"{qdrant_url()}/readyz"
QDRANT_COLLECTIONS = f"{qdrant_url()}/collections"
OLLAMA_TAGS = f"{ollama_url()}/api/tags"
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


def _ollama_up(timeout: float = 3.0) -> bool:
    """Is Ollama answering?"""
    try:
        with direct_urlopen(OLLAMA_TAGS, timeout=timeout):
            return True
    except OSError:
        return False


def launch_ollama() -> bool:
    """Start Ollama without a console window. True if a launch was issued.

    Shared by the self-heal below and the app's Start button, so the two can
    never disagree about how Ollama is found. Prefers the desktop app (it
    manages the server and keeps Ollama's own tray icon and auto-updates
    working), then falls back to a bare `ollama serve`.

    The child's std handles are ALWAYS pointed at DEVNULL. This runs inside
    the MCP server, whose stdout is the JSON-RPC pipe: an `ollama serve` that
    inherited it would write its log lines straight into the protocol stream
    and kill the session. It also detaches the child from our session so the
    server outlives this process.
    """
    quiet = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        import os

        app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
        if app.exists():
            try:
                subprocess.Popen([str(app)], **quiet, **_NO_WINDOW)
                return True
            except OSError:
                pass
        detach = {"creationflags": 0x08000000 | 0x00000200}  # NO_WINDOW | NEW_PROCESS_GROUP
    else:
        if sys.platform == "darwin":
            try:
                if subprocess.run(["open", "-a", "Ollama"], **quiet,
                                  timeout=15, check=False).returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        detach = {"start_new_session": True}
    try:
        subprocess.Popen(["ollama", "serve"], **quiet, **detach)
        return True
    except OSError:
        return False


def _ensure_ollama() -> None:
    """Start Ollama if it is installed but not running."""
    if _ollama_up():
        return
    print("rememory: starting Ollama (the local AI model runtime)...", file=sys.stderr)
    if not launch_ollama():
        print("rememory: Ollama is not installed or could not be launched -- "
              "install it from https://ollama.com/download, then reopen rememory.",
              file=sys.stderr)
        return
    for _ in range(30):  # ~2s is typical; allow for a cold start
        if _ollama_up():
            print("rememory: Ollama ready.", file=sys.stderr)
            return
        time.sleep(1)
    print("rememory: Ollama is starting slowly; searches will work once it is up.",
          file=sys.stderr)


def pin_venv_to_minor_link() -> bool:
    """Re-root .venv on uv's minor-version link (cpython-3.12-...) if it is
    pinned to a patch folder (cpython-3.12.13-...). True if it changed it.

    uv keeps `cpython-3.12-<platform>` as a link to the NEWEST 3.12 patch and
    deletes old patch folders when it upgrades Python. A venv whose `home` is
    the link follows upgrades transparently; one pinned to a patch folder dies
    the moment that folder is removed -- every `uv run`, the dashboard's
    Memories tab and the scheduled jobs then fail with "No Python at ...". On
    Windows the removal is usually only PARTIAL (a running rememory holds
    pythonw.exe open, so only python.exe is deleted), which made this look
    like python.exe randomly vanishing. A plain `uv venv` records the link;
    some recreation paths record the patch folder, so we normalise it here,
    at startup, while the pinned interpreter still exists.

    Only rewritten when the link exists and holds the same interpreter kind;
    anything that is not a uv-managed CPython is left alone.
    """
    import re

    cfg = ROOT / ".venv" / "pyvenv.cfg"
    try:
        # utf-8-sig: tolerate a byte-order mark (e.g. PowerShell 5.1's
        # Set-Content -Encoding utf8 adds one), which would otherwise hide
        # the first line -- `home` -- from the ^ anchor below.
        text = cfg.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    m = re.search(r"^home\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    if not m:
        return False
    home = Path(m.group(1))
    # Windows: home is the install folder. POSIX: home is its bin/ subfolder.
    install, tail = (home.parent, home.name) if home.name == "bin" else (home, "")
    mm = re.fullmatch(r"(cpython-\d+\.\d+)\.\d+(-.+)", install.name)
    if not mm:
        return False  # already the minor link, or not uv-managed
    link = install.with_name(mm.group(1) + mm.group(2))
    new_home = link / tail if tail else link
    probe = new_home / ("python.exe" if sys.platform == "win32" else "python3")
    if not probe.exists():
        return False
    try:
        cfg.write_text(text[:m.start(1)] + str(new_home) + text[m.end(1):], encoding="utf-8")
    except OSError:
        return False
    print(f"rememory: re-rooted the virtualenv on {link.name} so Python patch "
          f"upgrades can no longer break it.", file=sys.stderr)
    return True


def ensure_services() -> None:
    """Heal what is cheaply healable; say one friendly line about the rest.

    Ollama and Qdrant are checked INDEPENDENTLY. This used to open with
    `if _qdrant_up(): return`, so on the common day where the database was
    fine but Ollama was not (a reboot where Ollama did not auto-start), the
    heal returned before Ollama was even looked at, and nothing was logged.
    """
    pin_venv_to_minor_link()
    _ensure_ollama()
    _ensure_qdrant()


def _ensure_qdrant() -> None:
    """Bring the vector database up: start, recreate, and fill collections."""
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
