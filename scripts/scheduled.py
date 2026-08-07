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


def main() -> int:
    hide_own_console()  # before anything slow, so no black window lingers
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "sync":
        return run_job("sync", ["-m", "indexer.cli", "sync"], keep="chunks")
    if job == "backup":
        return run_job("backup", ["scripts/export_memory.py"], keep="exported")
    sys.stderr.write("usage: scheduled.py sync|backup\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
