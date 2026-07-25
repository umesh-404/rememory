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

import os
import sys
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"


def _apply_safe_mode() -> bool:
    """Optionally force WebView2 to render without the GPU.

    A handful of GPU/driver combinations paint nothing at all: the page loads,
    the layout is correct and fully measurable from JavaScript, but the window
    stays blank. Software rendering sidesteps it at the cost of some smoothness,
    which beats an invisible dashboard.

    This is opt-in via REMEMORY_UI_SAFE_MODE=1 rather than always-on, because
    the fault is rare and disabling the GPU for everyone would be a poor trade.
    The variable must be set before pywebview is imported -- WebView2 reads its
    browser arguments once, when it creates its environment.
    """
    if os.environ.get("REMEMORY_UI_SAFE_MODE") != "1":
        return False
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    flags = "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer"
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{existing} {flags}".strip()
    return True


def main() -> int:
    safe_mode = _apply_safe_mode()
    if safe_mode:
        print("UI safe mode: GPU rendering disabled.", file=sys.stderr)

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
