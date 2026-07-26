"""Automatic self-update on server startup.

When the MCP server starts (i.e. whenever a client session begins), rememory
checks its GitHub origin for new commits and, if any exist, fast-forwards to
the latest version and restarts itself -- so users stay current without ever
thinking about it.

Design constraints, each load-bearing:

* NEVER break startup. Offline, no remote, git missing, rate-limited -- every
  failure is a silent (stderr-logged) skip. An update check is an
  enhancement; the memory server must come up regardless.
* NEVER touch user changes. The pull is --ff-only, and it is skipped entirely
  if tracked files are modified locally (a user hacking on their clone keeps
  full control; gitignored files like config/projects.yaml and data/ are
  never at risk either way).
* stderr ONLY. stdout carries JSON-RPC; one stray print would kill the
  session.
* Re-exec after updating. The running process has already loaded the OLD
  code; pulling and then continuing would import a mix of old and new
  modules. After a successful pull the process replaces itself with a fresh
  one (loop-guarded by an env flag), so the version that answers the client
  is the version that was just pulled. If dependencies changed (uv.lock in
  the pull), they finish syncing on the next `uv run` launch -- noted to the
  user.
* Throttled + stampede-safe. Multiple clients (Claude Code + Desktop + an
  editor) can spawn servers within the same second; a marker file limits
  checks to one per THROTTLE_MINUTES across all of them.
* Opt-out: set REMEMORY_AUTO_UPDATE=0 (documented in README).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = ROOT / "data" / ".last-update-check"
THROTTLE_MINUTES = 15
GIT_TIMEOUT = 8  # seconds; a slow network must not stall session startup
# Suppress the console window Windows creates for child processes.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
_REEXEC_FLAG = "REMEMORY_UPDATE_REEXEC"


def _log(msg: str) -> None:
    print(f"rememory: {msg}", file=sys.stderr)


def _bar(fraction: float, width: int = 28) -> None:
    """One-line progress bar on stderr, redrawn in place via carriage return.
    In log viewers that do not honour it, it degrades to a few short lines."""
    filled = int(width * fraction)
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(chr(13) + "rememory: updating [" + bar + "] " + f"{int(fraction * 100):3d}%")
    sys.stderr.flush()


def _bar_end() -> None:
    sys.stderr.write(chr(10))
    sys.stderr.flush()


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            # No console window: this runs at MCP server startup, i.e. every
            # time a client session begins, where a flashing black window is
            # alarming and explains nothing.
            **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def maybe_update() -> None:
    """Check origin for updates; fast-forward and re-exec if found."""
    if os.environ.get("REMEMORY_AUTO_UPDATE", "1") == "0":
        return
    if os.environ.get(_REEXEC_FLAG):  # we ARE the freshly updated process
        return

    # Throttle across concurrent client launches.
    try:
        if MARKER.exists() and time.time() - MARKER.stat().st_mtime < THROTTLE_MINUTES * 60:
            return
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.touch()
    except OSError:
        pass  # a filesystem hiccup must not block startup

    if not (ROOT / ".git").exists():
        return
    remotes = _git("remote")
    if remotes is None or "origin" not in remotes.stdout.split():
        return  # no upstream configured (e.g. a development copy)

    fetched = _git("fetch", "--quiet", "origin")
    if fetched is None or fetched.returncode != 0:
        return  # offline or unreachable -- try again after the throttle window

    branch_proc = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch_proc is None or branch_proc.returncode != 0:
        return
    branch = branch_proc.stdout.strip()
    if branch == "HEAD":  # detached head -- user is doing something deliberate
        return

    behind_proc = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    if behind_proc is None or behind_proc.returncode != 0:
        return
    behind = int(behind_proc.stdout.strip() or 0)
    if behind == 0:
        return  # already up to date -- say nothing, stay out of the way

    latest = _git("rev-parse", "--short", f"origin/{branch}")
    version = latest.stdout.strip() if latest and latest.returncode == 0 else "latest"
    _log(f"new update available ({version})")

    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty is None or dirty.stdout.strip():
        _log("update skipped (local changes present -- run: git pull --ff-only)")
        return

    _bar(0.3)
    pulled = _git("merge", "--ff-only", f"origin/{branch}")
    if pulled is None or pulled.returncode != 0:
        _bar_end()
        _log("update skipped (histories diverged -- run: git pull --rebase)")
        return
    _bar(0.8)
    _bar(1.0)
    _bar_end()
    _log(f"updated successfully ({version})")

    # Replace this process with a fresh interpreter so the code that serves
    # the client is the code that was just pulled. The env flag prevents an
    # update loop if anything goes sideways.
    os.environ[_REEXEC_FLAG] = "1"
    try:
        os.execv(sys.executable, [sys.executable, "-m", "memory_mcp.server"])
    except OSError as exc:
        _log(f"restart failed ({exc}); the update takes effect on your next session.")
