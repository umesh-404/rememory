"""Automation hooks, so rememory stays current without anyone remembering to
ask it to.

Two kinds, both installed idempotently and both safe to re-run:

* Git hooks in each registered project (post-commit, post-merge,
  post-checkout, post-rewrite). Each launches a background, windowless
  incremental sync of THAT project, so the index follows commits, pulls,
  branch switches and rebases within seconds instead of waiting for the
  30-minute scheduled sync. The hook returns immediately; git never waits.

* Agent lifecycle hooks in every installed agentic IDE/CLI that has them
  (Claude Code, Codex, Cursor, VS Code Copilot, Gemini CLI, Windsurf, Kiro):
  the compact briefing is injected when a session starts -- and, where the
  client supports it, after context compaction, exactly when an agent loses
  its working picture of the project -- and every file the agent writes
  triggers the coalescing background sync. Clients without hooks (Zed,
  JetBrains Junie, ...) rely on the MCP server's instructions.

Hooks are written between marker lines. Anything else in an existing hook
file or settings.json is preserved untouched; re-installing only replaces
our own block (so a moved repo or new interpreter path self-corrects).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEGIN = "# >>> rememory auto-sync >>>"
END = "# <<< rememory auto-sync <<<"
GIT_HOOKS = ("post-commit", "post-merge", "post-checkout", "post-rewrite")
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _windowless_python() -> Path:
    """Interpreter for the git hook's launcher.

    On Windows the venv's pythonw.exe is a uv trampoline that spawns a
    console python.exe (a black window flashing on every commit); the BASE
    interpreter's pythonw.exe never gets a console. scheduled.py only needs
    the stdlib and launches the venv interpreter itself, windowless.
    """
    if sys.platform != "win32":
        venv = ROOT / ".venv" / "bin" / "python"
        return venv if venv.exists() else Path(sys.executable)
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                candidate = Path(value.strip()) / "pythonw.exe"
                if candidate.exists():
                    return candidate
    except OSError:
        pass
    return ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _hooks_dir(repo: Path) -> tuple[Path | None, str]:
    """Where git will actually look for hooks (honours core.hooksPath and
    worktrees), or (None, reason) when we should not touch it."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=10, check=False,
            stdin=subprocess.DEVNULL, **NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git unavailable ({exc})"
    if out.returncode != 0:
        return None, "not a git repository"
    hooks = Path(out.stdout.strip())
    if not hooks.is_absolute():
        hooks = (repo / hooks).resolve()
    # A custom hooksPath inside the work tree (husky, lefthook...) is tracked
    # project code shared with the team -- never write machine-specific paths
    # into it. A path inside .git is private to this clone and fine.
    try:
        rel = hooks.resolve().relative_to(repo.resolve())
        if rel.parts and rel.parts[0] != ".git":
            return None, f"uses a tracked hooks folder ({rel.as_posix()}); add manually"
    except ValueError:
        pass  # outside the repo entirely (global hooksPath) -- still private
    return hooks, ""


def _block(project: str) -> str:
    py = _windowless_python().as_posix()
    script = (ROOT / "scripts" / "scheduled.py").as_posix()
    # Backgrounded with all std handles detached: git returns at once and the
    # sync can never block or print into the user's terminal.
    return (f"{BEGIN}\n"
            f"# Re-indexes this project in rememory after the change (background).\n"
            f"\"{py}\" \"{script}\" sync {shlex.quote(project)} </dev/null >/dev/null 2>&1 &\n"
            f"{END}\n")


def _with_block(text: str, block: str) -> str | None:
    """The hook text with our block in place, or None if it must not be
    touched. The block goes RIGHT AFTER the shebang: hooks that end in
    `exit 0` or `exec ...` (pre-commit's template does) would never reach
    an appended block. It backgrounds itself, so running first is harmless."""
    if BEGIN in text and END in text:
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        return head + block.rstrip("\n") + tail
    if BEGIN in text or END in text:
        return None  # a damaged block: never guess where it ends
    if not text.strip():
        return "#!/bin/sh\n" + block
    first, _, rest = text.partition("\n")
    if not first.startswith("#!"):
        return block + text  # no shebang: git runs it with sh
    shell = first.lower()
    if not any(sh in shell for sh in ("/sh", "bash", "/dash", "/zsh", "env sh")):
        return None  # python/node/... hook: shell lines would break it
    return first + "\n" + block + rest


def install_git_hooks(project: str, repo: Path) -> str:
    """Install/refresh our block in each hook file. Returns a status line."""
    repo = Path(repo)
    hooks, why = _hooks_dir(repo)
    if hooks is None:
        return f"{project}: skipped -- {why}"
    hooks.mkdir(parents=True, exist_ok=True)
    block = _block(project)
    changed = []
    skipped = []
    for name in GIT_HOOKS:
        path = hooks / name
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{name} (unreadable: {type(exc).__name__})")
            continue
        new = _with_block(text, block)
        if new is None:
            skipped.append(f"{name} (not a shell script, or a half-written rememory block "
                           f"-- add the sync line by hand)")
            continue
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            changed.append(name)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o755)
    if skipped:
        return (f"{project}: git hooks {'partly updated' if changed else 'not changed'} "
                f"({hooks}); skipped: {'; '.join(skipped)}")
    return f"{project}: git hooks {'updated' if changed else 'already current'} ({hooks})"


def uninstall_git_hooks(repo: Path) -> None:
    hooks, _ = _hooks_dir(Path(repo))
    if hooks is None:
        return
    for name in GIT_HOOKS:
        path = hooks / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if BEGIN not in text:
            continue
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        remaining = (head + tail.lstrip("\n")).strip()
        if remaining in ("", "#!/bin/sh"):
            path.unlink()
        else:
            path.write_text(remaining + "\n", encoding="utf-8", newline="\n")


# ------------------------------------------------------- agent IDE/CLI hooks
# One script (scripts/agent_hook.py) serves every client; only the config
# shape differs. Each installer touches a client's config ONLY if that
# client is installed (its config directory exists), merges into existing
# settings, replaces just our own entries, and keeps a .rememory-backup.
# Schemas follow each vendor's hooks documentation as of Oct 2026.
OUR_SCRIPT = (ROOT / "scripts" / "agent_hook.py").as_posix().lower()


def _home() -> Path:
    return Path.home()


def _cmd(mode: str, client: str) -> str:
    py = _windowless_python()
    return f'"{py}" "{ROOT / "scripts" / "agent_hook.py"}" {mode} --client {client}'


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None  # unparseable (JSONC etc.): never risk rewriting it
    return data if isinstance(data, dict) else None


def _save_json(path: Path, data: dict, before: dict) -> bool:
    if data == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".rememory-backup")
    if path.exists() and not backup.exists():
        # Keep the FIRST backup: it is the user's config before rememory ever
        # touched it. Later installs only differ in our own entries.
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True


def _is_ours(command: object) -> bool:
    """Only OUR script, by full path -- another tool's agent_hook.py is not."""
    if not isinstance(command, str):
        return False
    norm = command.replace("\\", "/").lower()
    return OUR_SCRIPT in norm


def _strip_ours(entries: list) -> list:
    """Remove our hooks, keeping the user's -- including user hooks that share
    a matcher group with ours (only our inner entries are dropped)."""
    kept = []
    for e in entries:
        if not isinstance(e, dict):
            kept.append(e)
            continue
        if isinstance(e.get("hooks"), list):  # {matcher, hooks: [...]} shape
            inner = [h for h in e["hooks"]
                     if not (isinstance(h, dict) and _is_ours(h.get("command")))]
            if inner:
                kept.append({**e, "hooks": inner})
            continue
        if _is_ours(e.get("command")) or _is_ours((e.get("action") or {}).get("command")):
            continue
        kept.append(e)
    return kept


def _merge_events(path: Path, events: dict[str, list], *, wrapper: str | None = "hooks",
                  extra: dict | None = None) -> str:
    """Generic {<wrapper>: {Event: [entries]}} merge."""
    data = _load_json(path)
    if data is None:
        return f"skipped -- cannot parse {path}; not touching it"
    before = json.loads(json.dumps(data))
    target = data.setdefault(wrapper, {}) if wrapper else data
    if not isinstance(target, dict):
        return f"skipped -- unexpected '{wrapper}' format in {path}"
    for event, ours in events.items():
        current = target.get(event) or []
        if not isinstance(current, list):
            return f"skipped -- unexpected {event} format in {path}"
        target[event] = _strip_ours(current) + ours
    for k, v in (extra or {}).items():
        data.setdefault(k, v)
    changed = _save_json(path, data, before)
    return f"{'installed' if changed else 'already current'} ({path})"


def _claude() -> str | None:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or _home() / ".claude")
    if not base.is_dir():
        return None
    return _merge_events(base / "settings.json", {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
            {"type": "command", "command": _cmd("session-start", "claude"), "timeout": 30}]}],
        "PostToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [
            {"type": "command", "command": _cmd("post-edit", "claude"), "timeout": 10,
             "async": True}]}],
    })


def _codex() -> str | None:
    base = _home() / ".codex"
    if not base.is_dir():
        return None
    status = _merge_events(base / "hooks.json", {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
            {"type": "command", "command": _cmd("session-start", "codex"), "timeout": 30}]}],
        "PostToolUse": [{"matcher": "apply_patch|edit|write", "hooks": [
            {"type": "command", "command": _cmd("post-edit", "codex"), "timeout": 10}]}],
    })
    return status + " -- approve it once in Codex with /hooks (Codex asks before running new hooks)"


def _cursor() -> str | None:
    base = _home() / ".cursor"
    if not base.is_dir():
        return None
    return _merge_events(base / "hooks.json", {
        "sessionStart": [{"command": _cmd("session-start", "cursor"), "timeout": 30}],
        "afterFileEdit": [{"command": _cmd("post-edit", "cursor"), "timeout": 10}],
    }, extra={"version": 1})


def _copilot() -> str | None:
    if not (_home() / ".copilot").is_dir() and not (_home() / ".vscode").is_dir():
        return None
    return _merge_events(_home() / ".copilot" / "hooks" / "rememory.json", {
        "SessionStart": [{"type": "command", "command": _cmd("session-start", "copilot"),
                          "timeout": 30}],
        "PostToolUse": [{"type": "command", "command": _cmd("post-edit", "copilot"),
                         "timeout": 10}],
    })


def _gemini() -> str | None:
    base = _home() / ".gemini"
    if not base.is_dir():
        return None
    return _merge_events(base / "settings.json", {
        "SessionStart": [{"matcher": "startup|resume|clear", "hooks": [
            {"type": "command", "command": _cmd("session-start", "gemini"),
             "timeout": 30000}]}],
        "AfterTool": [{"matcher": "write_file|replace|edit", "hooks": [
            {"type": "command", "command": _cmd("post-edit", "gemini"), "timeout": 10000}]}],
    })


def _windsurf() -> str | None:
    base = _home() / ".codeium" / "windsurf"
    if not base.is_dir():
        return None
    cmd = _cmd("post-edit", "windsurf")
    status = _merge_events(base / "hooks.json", {
        "post_write_code": [{"command": cmd, "powershell": f"& {cmd}", "show_output": False}],
    })
    return status + " -- Windsurf has no session-start hook; it relies on the MCP instructions"


def _kiro() -> str | None:
    base = _home() / ".kiro"
    if not base.is_dir():
        return None
    path = base / "hooks" / "rememory.json"
    data = {"version": "v1", "hooks": [
        {"name": "rememory-briefing", "trigger": "SessionStart",
         "action": {"command": _cmd("session-start", "kiro")}, "timeout": 30},
        {"name": "rememory-reindex", "trigger": "PostFileSave",
         "action": {"command": _cmd("post-edit", "kiro")}, "timeout": 10},
    ]}
    before = _load_json(path) or {}
    changed = _save_json(path, data, before)
    return f"{'installed' if changed else 'already current'} ({path})"


CLIENTS = {"Claude Code": _claude, "Codex": _codex, "Cursor": _cursor,
           "VS Code Copilot": _copilot, "Gemini CLI": _gemini, "Windsurf": _windsurf,
           "Kiro": _kiro}


def install_agent_hooks() -> list[str]:
    """Install for every detected client; returns one status line each."""
    out = []
    for name, fn in CLIENTS.items():
        try:
            status = fn()
        except Exception as exc:  # one client's odd config must not stop the rest
            status = f"failed: {type(exc).__name__}: {exc}"
        if status is not None:
            out.append(f"{name}: {status}")
    return out
