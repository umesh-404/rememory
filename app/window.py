"""The dashboard window -- runs as its OWN process.

Why a separate process: pystray's event loop and pywebview's event loop both
demand the main thread (this is documented in both projects). Rather than
fight that, the tray owns the main thread of the app process and launches the
dashboard as a child process, which then owns its own main thread. The two
share no state -- every action in backend.Api is a subprocess or localhost
call -- so nothing needs synchronising between them.

Launched by the tray as:  python -m app.window
"""

from __future__ import annotations

import sys
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"


def main() -> int:
    try:
        import webview
    except ImportError:
        print("The desktop app needs pywebview. Install it with:\n"
              "  uv sync --extra app", file=sys.stderr)
        return 1

    from .backend import Api

    api = Api()
    window = webview.create_window(
        "rememory",
        str(UI_DIR / "index.html"),
        js_api=api,
        width=1120,
        height=760,
        min_size=(880, 620),
        background_color="#0a0c11",
        text_select=True,
    )
    # The folder picker needs a window handle, and the window only exists now.
    api.bind_window(window)
    webview.start()  # blocks until the user closes the window
    return 0


if __name__ == "__main__":
    sys.exit(main())
