"""Runtime ports -- one source of truth, with automatic conflict avoidance.

The default ports (Qdrant 6333/6334) are free on most machines, but not all:
another Qdrant, a corporate agent, or an unrelated dev service may already own
them. Hardcoding them everywhere would mean setup simply fails on those
machines with a docker bind error, which is a miserable first impression.

So: setup probes for a free port, records the choice in config/runtime.json
(gitignored -- it is machine-specific), and everything reads it from here.
Nothing else in the codebase may hardcode a port number.

Docker gets the value through environment substitution in compose.yml
(`${REMEMORY_QDRANT_PORT:-6333}`), which is why `compose_env()` exists: every
`docker compose` invocation must pass it, or the container would publish the
default port while Python talks to the chosen one.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
RUNTIME_FILE = CONFIG_DIR / "runtime.json"

DEFAULTS = {
    "qdrant_port": 6333,
    "qdrant_grpc_port": 6334,
    "ollama_port": 11434,   # Ollama's own default; changing it is the user's call
    "app_lock_port": 49517,  # single-instance guard for the desktop app
}


def runtime() -> dict:
    """Effective settings: defaults <- runtime.json <- environment."""
    values = dict(DEFAULTS)
    if RUNTIME_FILE.exists():
        # A corrupt file must not brick the install; defaults still work.
        with contextlib.suppress(ValueError, OSError):
            values.update(json.loads(RUNTIME_FILE.read_text(encoding="utf-8")))
    # Environment wins, so a user can override per-shell without editing files.
    for key in values:
        env = os.environ.get(f"REMEMORY_{key.upper()}")
        if env and env.isdigit():
            values[key] = int(env)
    return values


def save_runtime(**changes) -> dict:
    """Persist chosen ports (used by setup)."""
    current = {}
    if RUNTIME_FILE.exists():
        try:
            current = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            current = {}
    current.update({k: v for k, v in changes.items() if v is not None})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def qdrant_url() -> str:
    return f"http://127.0.0.1:{runtime()['qdrant_port']}"


def ollama_url() -> str:
    return f"http://127.0.0.1:{runtime()['ollama_port']}"


def compose_env() -> dict:
    """Environment for `docker compose` so the container publishes OUR ports."""
    values = runtime()
    env = dict(os.environ)
    env["REMEMORY_QDRANT_PORT"] = str(values["qdrant_port"])
    env["REMEMORY_QDRANT_GRPC_PORT"] = str(values["qdrant_grpc_port"])
    return env


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if nothing is listening AND we can bind it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def port_is_ours(port: int) -> bool:
    """True if the port is busy but it's OUR Qdrant already running -- a
    restarted setup must not treat its own healthy container as a conflict.

    Catches Exception deliberately: a port held by a NON-HTTP service (a gRPC
    endpoint, a database, anything speaking a binary protocol) makes urllib
    raise http.client.BadStatusLine, which is not an OSError. Letting that
    escape crashed setup on exactly the machines this function exists to
    support -- the ones where something else already owns the port.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/collections", timeout=2) as r:
            body = r.read(400).decode("utf-8", "replace")
        return '"collections"' in body
    except Exception:
        return False


def pick_free_port(preferred: int, tries: int = 20) -> int | None:
    """Preferred port if usable, else the next free one above it."""
    if port_is_free(preferred) or port_is_ours(preferred):
        return preferred
    for candidate in range(preferred + 1, preferred + 1 + tries):
        if port_is_free(candidate):
            return candidate
    return None
