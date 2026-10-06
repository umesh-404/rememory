"""Print ready-to-paste MCP connection config for YOUR machine.

rememory speaks standard MCP over stdio, so it works with any MCP client --
Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, or anything else that
can launch a stdio server. This script resolves the absolute paths on this
machine and prints the exact snippet/command for each client, so nothing has
to be hand-edited.

Run it any time (setup runs it for you at the end):

    uv run scripts/connect.py

It also writes the generic JSON to mcp-config.json in the repo root
(gitignored -- it contains machine-specific paths) so you can copy it later
without re-running anything.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_uv() -> str:
    """Absolute path to uv -- MCP clients launch servers outside your shell
    profile, so PATH-relative commands break in ways that are miserable to
    debug. Absolute paths always work."""
    found = shutil.which("uv")
    if found:
        # Do NOT .resolve() -- on Windows the winget shim in ...\WinGet\Links
        # is a symlink into a VERSIONED package directory that changes on
        # every uv upgrade. The shim path is the stable one; resolving it
        # would bake a path that silently breaks at the next update.
        return found
    # Common install locations, per platform.
    candidates = [
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "uv.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "uv"  # last resort; the caller is told to fix PATH


def uv_env() -> dict[str, str]:
    """uv location variables every client must launch the server with.

    MCP clients run `uv run` themselves, BEFORE any rememory code can pin
    anything, and some (Claude Desktop) pass only a minimal environment. uv
    then falls back to its default Python directory under %APPDATA% -- which
    Claude Desktop, a packaged Windows app, silently redirects into its own
    private sandbox. uv rebuilt the venv on a Python only Claude could see,
    and the Start-menu app and scheduled jobs broke with "No Python at ...".
    Passing the same, non-virtualised locations explicitly makes every
    launcher agree. Values follow whatever this setup used (setup pins them
    to folders inside the repo when they are not already set).
    """
    import os

    return {
        "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR")
        or str(ROOT / ".uv-python"),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(ROOT / ".uv-cache"),
    }


def main() -> int:
    uv = find_uv()
    root = str(ROOT)
    env = uv_env()
    # Launch the venv's interpreter directly when it exists, NOT `uv run`:
    # a client that starts uv without rememory's UV_* variables makes uv
    # rebuild the venv on a different Python (Claude Desktop did, repeatedly,
    # after rewriting its config without the env block). With no uv in the
    # launch path, no client can trigger that rebuild.
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows"
                                else "bin/python")
    if venv_py.exists():
        uv, args = str(venv_py), ["-m", "memory_mcp.server"]
    else:
        args = ["run", "--directory", root, "-m", "memory_mcp.server"]

    server_json = {"rememory": {"command": uv, "args": args, "env": env}}
    generic = json.dumps({"mcpServers": server_json}, indent=2)

    # Persist for later copy-paste (gitignored: machine-specific paths).
    (ROOT / "mcp-config.json").write_text(generic + "\n", encoding="utf-8")

    is_windows = platform.system() == "Windows"
    desktop_cfg = (
        "%APPDATA%\\Claude\\claude_desktop_config.json" if is_windows
        else "~/Library/Application Support/Claude/claude_desktop_config.json (macOS)"
             " or ~/.config/Claude/claude_desktop_config.json (Linux)"
    )
    arg_str = " ".join(f'"{a}"' if " " in a else a for a in args)
    env_flags = " ".join(f'-e "{k}={v}"' for k, v in env.items())

    print(f"""
================================================================
  rememory is installed. ONE step left: connect your client.
  (Pick whichever you use -- the server is standard MCP over
  stdio, so any MCP-capable client works.)
================================================================

The server command for this machine:
  {uv} {arg_str}

The generic config (also saved to mcp-config.json in this folder):

{generic}

---------------------------------------------------------------
CLAUDE CODE (CLI)
  Run this in your terminal (bash/cmd -- PowerShell 5.1 eats the `--`):

    claude mcp add --scope user {env_flags} rememory -- "{uv}" {arg_str}

  Then restart your Claude Code sessions. Try: /mcp__rememory__kickoff <project>

CLAUDE DESKTOP
  Settings -> Developer -> Edit Config opens:
    {desktop_cfg}
  Merge the "rememory" entry above into its "mcpServers" object,
  then FULLY quit the app (system tray too) and reopen.

CURSOR
  Add the "rememory" entry to the "mcpServers" object in:
    ~/.cursor/mcp.json            (global)  or
    <your-project>/.cursor/mcp.json  (per project)
  Then: Settings -> MCP -> verify rememory shows tools.

WINDSURF
  Add it to "mcpServers" in ~/.codeium/windsurf/mcp_config.json,
  then refresh MCP servers in settings.

VS CODE (GitHub Copilot agent mode)
  Add to "servers" in .vscode/mcp.json (or user mcp.json), shape:
    {{ "rememory": {{ "type": "stdio", "command": "{uv}", "args": [...same args...],
                    "env": {{ ...same env... }} }} }}

ANY OTHER CLIENT
  Point it at the command above with the two UV_* variables from the
  "env" block set -- stdio transport, no API keys. The variables keep uv
  on the same Python as every other rememory launcher.
---------------------------------------------------------------

After connecting, register your projects in config/projects.yaml and run:
  uv run -m indexer.cli index --project <name>
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
