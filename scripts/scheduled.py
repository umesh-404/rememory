"""Background jobs, run by Task Scheduler. NO console window, ever.

    pythonw.exe scripts/scheduled.py sync
    pythonw.exe scripts/scheduled.py backup

Why this replaced the PowerShell versions
-----------------------------------------
The scheduled tasks used to run `powershell.exe -WindowStyle Hidden -File
scripts\\sync.ps1`. Hidden is applied by PowerShell itself, only after the
process has started, so Task Scheduler still flashed a console window on
screen every 30 minutes -- black windows appearing and vanishing for no
visible reason, which reads as malware to anyone watching.

pythonw.exe is a GUI-subsystem binary: Windows never gives it a console, so
there is nothing to flash. Child processes it spawns get CREATE_NO_WINDOW for
the same reason.

Behaviour is otherwise the same as the scripts it replaces: append one line
per run to data/logs/, exit quietly when the services are not up (a laptop
that is simply not running Docker should not accumulate error noise), and
rotate the log when it gets large.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "data" / "logs"

NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
MAX_LOG_BYTES = 1_000_000


def hide_own_console() -> None:
    """Hide the console window this process was given, if any.

    The docstring above says pythonw.exe never gets a console -- that is true
    of the *base* interpreter's pythonw.exe, but the venv's pythonw.exe is a
    uv trampoline that re-launches the console-subsystem interpreter, and that
    child allocates a visible console window for the whole job: a black
    python.exe window appearing every 30 minutes for no visible reason.
    setup.ps1 now registers the task against the base pythonw.exe, where no
    console ever exists; this hide covers installs whose task still points at
    the venv trampoline (tasks are only re-registered by re-running setup).

    Same technique as app/main.py:hide_own_console -- hiding, not FreeConsole,
    so stdout/stderr writes keep working (and keep going nowhere visible).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass  # cosmetic only -- never prevent the job from running


def log(name: str, message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / f"{name}.log"
    # Rotate before writing so the file never grows without bound.
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {message}\n")


def _reachable(url: str, timeout: float = 5.0) -> bool:
    """Direct request -- a proxy is never correct for our own loopback ports."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout):
            return True
    except Exception:
        return False


def _ports() -> tuple[int, int]:
    import json

    qdrant, ollama = 6333, 11434
    path = ROOT / "config" / "runtime.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            qdrant = int(data.get("qdrant_port", qdrant))
            ollama = int(data.get("ollama_port", ollama))
        except (ValueError, OSError, TypeError):
            pass
    return qdrant, ollama


def _python() -> str:
    """The venv interpreter. sys.executable already is it when run correctly,
    but fall back explicitly so a misconfigured task still works."""
    if sys.platform == "win32":
        candidate = ROOT / ".venv/Scripts/python.exe"
    else:
        candidate = ROOT / ".venv/bin/python"
    return str(candidate) if candidate.exists() else sys.executable


def run_job(name: str, args: list[str], keep: str) -> int:
    qdrant, ollama = _ports()
    if not _reachable(f"http://127.0.0.1:{qdrant}/readyz"):
        log(name, "skipped: qdrant not reachable")
        return 0
    if name == "sync" and not _reachable(f"http://127.0.0.1:{ollama}/api/tags"):
        log(name, "skipped: ollama not reachable")
        return 0

    try:
        proc = subprocess.run(
            [_python(), *args],
            capture_output=True, text=True, timeout=3600,
            cwd=str(ROOT), check=False, **NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(name, f"FAILED to launch: {type(exc).__name__}: {exc}")
        return 1

    output = (proc.stdout or "") + (proc.stderr or "")
    summary = " | ".join(
        line.strip() for line in output.splitlines() if keep in line
    )[:800]
    if proc.returncode == 0:
        log(name, f"ok: {summary or 'completed'}")
        return 0
    last = (output.strip().splitlines() or ["no output"])[-1][:300]
    log(name, f"FAILED (exit {proc.returncode}): {last}")
    return 1


# ------------------------------------------------------- per-project sync
# Git hooks and agent post-edit hooks fire in bursts (a rebase rewrites 20
# commits; an agent saves 30 files in a minute). Running one sync per event
# would embed the same files over and over. Instead every event just marks
# the project DIRTY; one worker per project syncs until nothing is dirty.
# Twenty events therefore cost one or two syncs, not twenty.
DEBOUNCE_SECONDS = 4.0      # let a burst settle before the first pass
BUSY_RETRY_SECONDS = 10.0   # another indexer holds the global lock
BUSY_RETRY_MAX = 60         # ~10 minutes, then give up (scheduled sync covers it)
WORKER_STALE_SECONDS = 300.0  # the worker heartbeats every few seconds while alive
HEARTBEAT_SECONDS = 15.0


def _flag(project: str, kind: str) -> Path:
    safe = "".join(c for c in project if c.isalnum() or c in "-_")
    return ROOT / "data" / f".sync-{kind}-{safe}"


def _take_worker(lock: Path) -> bool:
    import os
    import time

    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < WORKER_STALE_SECONDS:
                    return False
                lock.unlink()  # a crashed worker (no heartbeat for 5 min); take over
            except OSError:
                return False
    return False


def sync_project(project: str) -> int:
    """Mark dirty; if no worker is running, become it and sync until clean.

    No event can be lost: the worker re-checks the dirty flag AFTER releasing
    its lock and takes the lock again if a late event slipped in between its
    last check and the release. Any pass that could not run (services down,
    pass cap hit) leaves the flag set for the next event or worker.
    """
    import time

    dirty = _flag(project, "dirty")
    lock = _flag(project, "worker")
    dirty.parent.mkdir(parents=True, exist_ok=True)
    dirty.touch()
    rc = 0
    while True:
        if not _take_worker(lock):
            return rc  # a live worker exists and will see the dirty flag
        try:
            time.sleep(DEBOUNCE_SECONDS)
            passes = 0
            while dirty.exists():
                if passes >= 10:
                    log("sync", f"{project}: still dirty after {passes} passes; leaving "
                                f"it for the next event or the scheduled sync")
                    return rc  # dirty stays set
                dirty.unlink(missing_ok=True)
                passes += 1
                for _ in range(BUSY_RETRY_MAX):
                    rc, busy, ran = _run_project_index(project, lock)
                    if not ran:
                        dirty.touch()  # nothing was indexed; keep the work pending
                        return rc
                    if not busy:
                        break
                    time.sleep(BUSY_RETRY_SECONDS)
                    lock.touch()
                else:
                    dirty.touch()
                    log("sync", f"{project}: gave up waiting for the index lock")
                    return 1
        finally:
            lock.unlink(missing_ok=True)
        if not dirty.exists():
            return rc
        # An event landed between our last check and releasing the lock: loop.


def _run_project_index(project: str, lock: Path) -> tuple[int, bool, bool]:
    """Returns (returncode, busy, ran). Heartbeats the worker lock while the
    index runs, so a long first index is never mistaken for a dead worker."""
    import time

    qdrant, ollama = _ports()
    if not (_reachable(f"http://127.0.0.1:{qdrant}/readyz")
            and _reachable(f"http://127.0.0.1:{ollama}/api/tags")):
        log("sync", f"{project}: skipped, services not reachable")
        return 0, False, False
    try:
        proc = subprocess.Popen(
            [_python(), "-m", "indexer.cli", "index", "--project", project, "--changed"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), **NO_WINDOW,
        )
    except OSError as exc:
        log("sync", f"{project}: FAILED to launch: {exc}")
        return 1, False, False
    import threading

    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read() or ""),
                              daemon=True)
    reader.start()
    deadline = time.time() + 3600
    while True:
        lock.touch()  # heartbeat: this worker is alive
        try:
            proc.wait(timeout=HEARTBEAT_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                log("sync", f"{project}: killed after 1 h")
                break
    reader.join(timeout=10)
    out = "".join(chunks)
    if "already running" in out:
        return 0, True, True
    summary = " | ".join(ln.strip() for ln in out.splitlines() if "chunks" in ln)[:400]
    log("sync", f"{project}: {'ok' if proc.returncode == 0 else 'FAILED'}: "
                f"{summary or out.strip()[-200:]}")
    return proc.returncode or 0, False, True


def main() -> int:
    hide_own_console()  # before anything slow, so no black window lingers
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "sync" and len(sys.argv) > 2:
        return sync_project(sys.argv[2])
    if job == "sync":
        return run_job("sync", ["-m", "indexer.cli", "sync"], keep="chunks")
    if job == "backup":
        return run_job("backup", ["scripts/export_memory.py"], keep="exported")
    sys.stderr.write("usage: scheduled.py sync [project]|backup\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
