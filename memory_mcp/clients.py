"""Keep MCP client configs launching rememory WITHOUT uv.

The one failure that keeps coming back: a client launches rememory as
`uv run --directory <repo> -m memory_mcp.server` without rememory's UV_*
variables. uv then looks for Python in its default folder -- which for
Claude Desktop (a packaged Windows app) is a private sandbox -- decides the
repo's .venv is "foreign", and rebuilds it. On Windows the rebuild deletes
the venv and then dies with "Access is denied" on .venv\\Scripts, because
the running tray app holds pythonw.exe open. The venv is left gutted, every
client fails to start, and the tray app cannot restart either.

uv offers no project setting that prevents this (`python-install-dir` is
env-only; `managed = false` breaks `uv sync`), and it happens before any
rememory code runs. So the fix is in the launch command itself: run the
venv's own interpreter. This module finds Claude Desktop's config wherever
the install keeps it -- including the MSIX locations, where
%APPDATA%\\Claude holds only a marker file -- and rewrites a uv-based
`rememory` entry. Nothing else in the file is touched, and a backup is kept.

The server calls heal_client_configs() at startup, so if Claude Desktop ever
reverts its config, the next successful start from any client repairs it.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "rememory"


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def uv_env() -> dict[str, str]:
    """uv locations kept in the entry so anything rememory itself runs through
    uv (e.g. restoring the app's packages) agrees with setup."""
    return {
        "UV_PYTHON_INSTALL_DIR": (os.environ.get("UV_PYTHON_INSTALL_DIR")
                                  or str(ROOT / ".uv-python")),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(ROOT / ".uv-cache"),
    }


def server_entry() -> dict:
    """The recommended launch entry: the venv interpreter, no uv."""
    return {"command": str(venv_python()), "args": ["-m", "memory_mcp.server"], "env": uv_env()}


def desktop_config_paths() -> list[Path]:
    """Every Claude Desktop config file that exists on this machine."""
    home = Path.home()
    patterns: list[str] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(home / "AppData/Roaming")
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData/Local")
        patterns = [
            os.path.join(appdata, "Claude", "claude_desktop_config.json"),
            # MSIX (Microsoft Store / winget) installs keep user data here.
            os.path.join(local, "Claude-Data", "*", "claude_desktop_config.json"),
            os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                         "claude_desktop_config.json"),
        ]
    elif sys.platform == "darwin":
        patterns = [str(home / "Library/Application Support/Claude/claude_desktop_config.json")]
    else:
        patterns = [str(home / ".config/Claude/claude_desktop_config.json")]
    found: list[Path] = []
    for pat in patterns:
        for p in glob.glob(pat):
            path = Path(p)
            if path.is_file() and path not in found:
                found.append(path)
    return found


def _launches_via_uv(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    name = Path(str(entry.get("command", ""))).name.lower()
    return name in ("uv", "uv.exe", "uvx", "uvx.exe")


def heal_client_configs(quiet: bool = False) -> list[Path]:
    """Rewrite uv-based rememory entries to launch the venv interpreter.

    Only acts when the venv interpreter exists (otherwise uv is the only way
    to start). Returns the files changed. Never raises.
    """
    if not venv_python().exists():
        return []
    changed: list[Path] = []
    for path in desktop_config_paths():
        try:
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            entry = data.get("mcpServers", {}).get(SERVER_NAME)
            if not _launches_via_uv(entry):
                continue
            backup = path.with_name(path.name + ".rememory-bak")
            shutil.copy2(path, backup)
            data["mcpServers"][SERVER_NAME] = server_entry()
            tmp = path.with_name(path.name + ".rememory-tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            changed.append(path)
            if not quiet:
                print(f"rememory: {path} launched rememory through uv (which rebuilds the venv); "
                      f"switched it to the venv interpreter. Backup: {backup.name}. "
                      "Fully quit and reopen Claude Desktop to apply.", file=sys.stderr)
        except (OSError, ValueError, AttributeError, TypeError) as e:
            if not quiet:
                print(f"rememory: could not check {path}: {e}", file=sys.stderr)
    return changed


if __name__ == "__main__":
    fixed = heal_client_configs()
    print("\n".join(str(p) for p in fixed) or "no uv-based rememory entries found")
