"""One hook entry point for every agentic IDE/CLI that has lifecycle hooks.

    agent_hook.py session-start --client claude|codex|copilot|gemini|cursor|kiro|plain
    agent_hook.py post-edit     --client <same>

session-start: prints the compact rememory briefing for the project the
session is in, in the output shape that client injects into the model's
context. Fires at startup and -- where the client supports it -- after
context compaction, which is exactly when an agent loses its picture of the
project mid-session.

post-edit: the agent just wrote a file. Mark that file's project dirty and
start (detached, windowless) the coalescing per-project sync, then return at
once -- several clients expect edit hooks to finish in ~100 ms.

Rules every client imposes, honoured here:
* never block the agent: any failure exits 0 with empty/neutral output;
* stdout carries ONLY the payload (Gemini rejects anything but one JSON
  object); diagnostics go to stderr;
* text is phrased as facts, not instructions -- injected context that reads
  like commands trips prompt-injection defences.

Input field names differ per client (cwd / workspace_roots / GEMINI_CWD;
tool_input.file_path / file_path / tool_info.file_path); all are accepted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIEFING_BUDGET = 4500

# Run under the BASE interpreter (no console window on Windows) but with the
# venv's packages importable.
for _sp in (ROOT / ".venv" / "Lib" / "site-packages",
            *sorted((ROOT / ".venv" / "lib").glob("python3*/site-packages"))):
    if _sp.is_dir() and str(_sp) not in sys.path:
        import site

        site.addsitedir(str(_sp))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JSON_CLIENTS = {"claude", "codex", "copilot", "gemini"}


def _read_stdin() -> dict:
    try:
        if sys.stdin is None or sys.stdin.closed:
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _cwd(data: dict) -> str:
    roots = data.get("workspace_roots") or []
    return (data.get("cwd") or (roots[0] if roots else None)
            or (data.get("tool_info") or {}).get("cwd")
            or os.environ.get("GEMINI_CWD") or os.environ.get("CLAUDE_PROJECT_DIR")
            or os.getcwd())


def _edited_file(data: dict) -> str | None:
    ti = data.get("tool_input") or {}
    info = data.get("tool_info") or {}
    for v in (ti.get("file_path"), ti.get("path"), ti.get("notebook_path"),
              data.get("file_path"), info.get("file_path")):
        if isinstance(v, str) and v:
            return v
    return None


def _project_for(path: str):
    """(name, project) whose root contains path, deepest root first."""
    from indexer.config import load_config

    cfg = load_config()
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return cfg, None, None
    best = None
    for name, proj in cfg.projects.items():
        try:
            root = proj.root.resolve()
        except OSError:
            continue
        if (target == root or root in target.parents) and (
                best is None or len(str(root)) > len(str(best[1].root))):
            best = (name, proj)
    return (cfg, *best) if best else (cfg, None, None)


def _emit(client: str, event: str, text: str) -> None:
    if client in JSON_CLIENTS:
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}} \
            if text else {}
        sys.stdout.write(json.dumps(out))
    elif client == "cursor":
        sys.stdout.write(json.dumps({"additional_context": text} if text else {}))
    elif text:
        sys.stdout.write(text)
    sys.stdout.flush()


# ----------------------------------------------------------- session start
def session_start(client: str, data: dict) -> None:
    cfg, name, _proj = _project_for(_cwd(data))
    if name is None:
        _emit(client, "SessionStart", "")
        return
    from memory_mcp import facts as _facts
    from memory_mcp import localdb
    from memory_mcp.briefing import build_briefing
    from memory_mcp.memories import MemoryStore

    store = MemoryStore(cfg)
    proj = cfg.projects[name]
    try:
        review = store.review(name, root=proj.root, limit=10)
    except Exception:
        review = []
    facts: dict = {}
    try:
        facts.update(_facts.collect(proj, "alembic"))
        facts.update(_facts.collect(
            proj, "ids", reservations_fn=lambda s, a: localdb.reservations(name, s, a)))
    except Exception:
        pass
    try:
        claims = localdb.list_claims(name)
    except Exception:
        claims = []
    body = build_briefing(cfg, store.client, store, name, max_chars=BRIEFING_BUDGET,
                          review=review, facts=facts, claims=claims)
    source = data.get("source") or ""
    lead = (f"rememory briefing for project '{name}' (loaded automatically"
            + (" after context compaction" if source == "compact" else "")
            + f"; the rememory MCP tools take project='{name}'; get_briefing is not "
              f"needed again this session).")
    _emit(client, "SessionStart", lead + "\n\n" + body)


# --------------------------------------------------------------- post edit
def _windowless_python() -> str:
    from memory_mcp.hooks import _windowless_python as wp

    return str(wp())


def post_edit(client: str, data: dict) -> None:
    path = _edited_file(data)
    if path:
        if not Path(path).is_absolute():
            path = str(Path(_cwd(data)) / path)
        _cfg, name, _proj = _project_for(path)
        if name is not None:
            flags = {}
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
                flags["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
            else:
                flags["start_new_session"] = True
            subprocess.Popen(
                [_windowless_python(), str(ROOT / "scripts" / "scheduled.py"), "sync", name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=str(ROOT), close_fds=True, **flags,
            )
    if client in JSON_CLIENTS or client == "cursor":
        sys.stdout.write("{}")
        sys.stdout.flush()


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else ""
    client = "plain"
    if "--client" in args:
        i = args.index("--client")
        if i + 1 < len(args):
            client = args[i + 1].lower()
    data = _read_stdin()
    try:
        if mode == "session-start":
            session_start(client, data)
        elif mode == "post-edit":
            post_edit(client, data)
    except Exception as exc:  # never break the agent's session
        print(f"rememory hook ({mode}) skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        if mode == "session-start" and client in JSON_CLIENTS | {"cursor"}:
            sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
