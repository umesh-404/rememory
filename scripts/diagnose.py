"""Report exactly why rememory cannot reach its services.

Run this on a machine where the dashboard shows Docker or Database offline
while they look fine to you:

    python scripts\\diagnose.py

Deliberately stdlib-only, and deliberately does NOT import indexer or
memory_mcp: it has to work on a machine whose virtualenv is broken or half
built, which is exactly when it is needed. It reads config/runtime.json the
same way indexer/runtime.py does, so a port mismatch shows up as a mismatch
rather than as a silent failure.

Changes nothing. Safe to run at any time.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_FILE = ROOT / "config" / "runtime.json"
DEFAULT_QDRANT_PORT = 6333
DEFAULT_OLLAMA_PORT = 11434
CONTAINER = "rememory-qdrant"

NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

problems: list[str] = []


def say(label: str, value: str) -> None:
    print(f"  {label:<24} {value}")


def head(title: str) -> None:
    print(f"\n=== {title} ===")


def run(args: list[str], timeout: int = 30):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            check=False, **NO_WINDOW,
        )
    except subprocess.SubprocessError:
        return "timeout"
    except OSError:
        return None


def resolve_ports() -> tuple[int, int]:
    qdrant, ollama = DEFAULT_QDRANT_PORT, DEFAULT_OLLAMA_PORT
    if RUNTIME_FILE.exists():
        try:
            data = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
            qdrant = int(data.get("qdrant_port", qdrant))
            ollama = int(data.get("ollama_port", ollama))
        except (ValueError, OSError, TypeError) as exc:
            problems.append(f"config/runtime.json is unreadable ({exc}); using defaults.")
    for name, current in (("QDRANT_PORT", qdrant), ("OLLAMA_PORT", ollama)):
        env = os.environ.get(f"REMEMORY_{name}")
        if env and env.isdigit() and int(env) != current:
            problems.append(
                f"Environment REMEMORY_{name}={env} overrides config/runtime.json "
                f"({current}). The app and the container may disagree."
            )
            if name == "QDRANT_PORT":
                qdrant = int(env)
            else:
                ollama = int(env)
    return qdrant, ollama


def tcp_open(port: int, timeout: float = 5.0) -> tuple[bool, str]:
    start = time.time()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True, f"connected in {time.time() - start:.2f}s"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def http_get(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """GET with proxies explicitly disabled.

    An empty ProxyHandler is the only reliable way to guarantee a direct
    loopback connection: on Windows, urllib honours NO_PROXY only when the
    proxy settings themselves came from the environment, and falls back to the
    system configuration's own bypass rules otherwise.
    """
    start = time.time()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            resp.read(200)
            return True, f"HTTP {resp.status} in {time.time() - start:.2f}s"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} after {time.time() - start:.2f}s"
    except (OSError, urllib.error.URLError) as exc:
        return False, f"{type(exc).__name__}: {exc} (after {time.time() - start:.2f}s)"


def main() -> int:
    print("rememory diagnostics -- reads only, changes nothing")
    say("root", str(ROOT))

    head("Configured ports")
    qdrant_port, ollama_port = resolve_ports()
    say("runtime.json", "present" if RUNTIME_FILE.exists() else "absent (using defaults)")
    say("qdrant port", str(qdrant_port))
    say("ollama port", str(ollama_port))

    head("Proxy settings")
    # A proxy is never correct for 127.0.0.1, but urllib and httpx both pick
    # these up, and on Windows urllib also reads the system configuration.
    # A machine with a proxy set can have a healthy database that every client
    # reports as unreachable, which is worth naming explicitly.
    proxy_vars = {
        k: v for k, v in os.environ.items()
        if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") and v
    }
    for k, v in proxy_vars.items():
        say(k, v)
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    say("NO_PROXY", no_proxy or "(unset)")
    system_proxy = ""
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if enabled:
                system_proxy = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            key.Close()
        except OSError:
            pass
    say("system proxy", system_proxy or "(none)")
    if (proxy_vars or system_proxy) and not all(
        h in no_proxy for h in ("127.0.0.1", "localhost")
    ):
        problems.append(
            "A proxy is configured and NO_PROXY does not list 127.0.0.1 and "
            "localhost. Clients can end up sending loopback requests to the "
            "proxy, which makes a healthy database look unreachable. rememory "
            "now bypasses proxies for loopback itself -- if an older component "
            "still fails, set NO_PROXY=127.0.0.1,localhost"
        )
    if not proxy_vars and not system_proxy:
        say("verdict", "no proxy configured -- not a factor here")

    head("Docker")
    container_running = False
    published = ""
    info = run(["docker", "info", "--format", "ok"], timeout=45)
    if info is None:
        say("docker CLI", "NOT FOUND on PATH")
        problems.append("The docker command is not on PATH. Is Docker Desktop installed?")
    elif info == "timeout":
        say("docker info", "TIMED OUT after 45s")
        problems.append(
            "`docker info` did not answer in 45s. Docker Desktop is probably still "
            "starting, or its WSL2 backend is wedged -- restart Docker Desktop."
        )
    elif info.returncode != 0:
        say("docker info", f"FAILED (exit {info.returncode})")
        detail = (info.stderr or "").strip().splitlines()
        if detail:
            say("", detail[-1][:160])
        problems.append("The Docker daemon is not reachable. Start Docker Desktop.")
    else:
        say("docker info", "ok (daemon reachable)")

    ps = run(["docker", "ps", "-a", "--filter", f"name={CONTAINER}",
              "--format", "{{.Names}}|{{.State}}|{{.Status}}|{{.Ports}}"], timeout=45)
    if isinstance(ps, subprocess.CompletedProcess) and ps.returncode == 0:
        line = (ps.stdout or "").strip()
        if not line:
            say("container", f"'{CONTAINER}' DOES NOT EXIST")
            problems.append(
                f"Container '{CONTAINER}' is missing. Run setup once to recreate it."
            )
        else:
            name, state, status, ports = (line.split("|", 3) + ["", "", ""])[:4]
            say("container", f"{name} [{state}] {status}")
            say("published ports", ports or "(none)")
            container_running = state == "running"
            published = ports
            if not container_running:
                problems.append(
                    f"Container '{name}' exists but is {state}. "
                    f"Press Start in the rememory app, or: docker start {CONTAINER}"
                )

    head("Vector database (Qdrant)")
    ok, detail = tcp_open(qdrant_port)
    say(f"tcp 127.0.0.1:{qdrant_port}", ("OPEN " if ok else "CLOSED ") + detail)
    if ok:
        for path in ("/readyz", "/collections"):
            got, d = http_get(f"http://127.0.0.1:{qdrant_port}{path}")
            say(f"GET {path}", ("ok " if got else "FAILED ") + d)
            if not got:
                problems.append(
                    f"The port is open but {path} failed ({d}). Something other than "
                    f"Qdrant may be listening on port {qdrant_port}."
                )
    elif container_running:
        # The most informative case: the container is up, yet the port the app
        # talks to is dead. Compare against what the container actually
        # publishes rather than pattern-matching Docker's port format, which
        # collapses consecutive ports into ranges like 6333-6334->6333-6334.
        problems.append(
            f"The container is running but nothing answers on 127.0.0.1:{qdrant_port}, "
            f"which the app is configured to use. The container publishes: "
            f"{published or '(nothing)'}. If that does not cover {qdrant_port}, the "
            f"container was created for a different port -- re-run setup to recreate "
            f"it against config/runtime.json."
        )
    else:
        problems.append(
            f"Nothing is listening on 127.0.0.1:{qdrant_port} -- this is why the "
            f"dashboard says the database is offline."
        )

    head("Ollama")
    ok, detail = tcp_open(ollama_port)
    say(f"tcp 127.0.0.1:{ollama_port}", ("OPEN " if ok else "CLOSED ") + detail)
    if ok:
        got, d = http_get(f"http://127.0.0.1:{ollama_port}/api/tags")
        say("GET /api/tags", ("ok " if got else "FAILED ") + d)
    else:
        problems.append("Ollama is not running. Start the Ollama app.")

    head("Verdict")
    if not problems:
        print("  No problems found -- every service answered.")
        print("  If the dashboard still disagrees, restart it (tray icon > Quit, then")
        print("  reopen rememory) so it re-reads config/runtime.json.")
    else:
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
