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


def _icon_path() -> Path | None:
    """The generated multi-resolution .ico, creating it if setup never did."""
    ico = Path(__file__).resolve().parent.parent / "data" / "rememory.ico"
    if ico.exists():
        return ico
    try:
        from .icon import write_ico

        ico.parent.mkdir(parents=True, exist_ok=True)
        write_ico(str(ico))
        return ico if ico.exists() else None
    except Exception:
        return None


def _apply_window_icon() -> None:
    """Give the dashboard rememory's icon instead of the interpreter's.

    On Windows a window inherits its icon from the executable, so launching
    through python/pythonw leaves the title bar and taskbar showing Python's
    default icon. pywebview does not expose the WebView2 window's icon, so we
    set it directly with WM_SETICON.

    The window is located by walking our own process's visible top-level
    windows rather than by title or by reaching into pywebview's internals --
    that works whichever GUI backend pywebview picked, and cannot be confused
    by another window that happens to share our title.
    """
    if sys.platform != "win32":
        return
    ico = _icon_path()
    if ico is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

        # Load at the two sizes Windows asks for: the title bar and Alt-Tab.
        small = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        big = user32.LoadImageW(None, str(ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if not small and not big:
            return

        me = os.getpid()
        targets: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me and user32.IsWindowVisible(hwnd):
                targets.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        for hwnd in targets:
            if small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            if big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
    except Exception:
        pass  # cosmetic only -- never stop the dashboard from opening


def main() -> int:
    # Same reason as the tray: the interpreter this runs under may have been
    # handed a console window, which would sit behind the dashboard as an empty
    # black window (see app/main.py:hide_own_console).
    from .main import hide_own_console

    hide_own_console()

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
    # Set the icon once the window is actually on screen -- WM_SETICON needs a
    # real HWND, which does not exist until then.
    window.events.shown += _apply_window_icon
    webview.start()  # blocks until the user closes the window
    return 0


if __name__ == "__main__":
    sys.exit(main())
