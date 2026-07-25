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


def main() -> int:
    uv = find_uv()
    root = str(ROOT)
    args = ["run", "--directory", root, "-m", "memory_mcp.server"]

    server_json = {"rememory": {"command": uv, "args": args}}
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

    claude mcp add --scope user rememory -- "{uv}" run --directory "{root}" -m memory_mcp.server

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
    {{ "rememory": {{ "type": "stdio", "command": "{uv}", "args": [...same args...] }} }}

ANY OTHER CLIENT
  Point it at the command above -- stdio transport, no env vars,
  no API keys.
---------------------------------------------------------------

After connecting, register your projects in config/projects.yaml and run:
  uv run -m indexer.cli index --project <name>
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
