"""rememory tray app -- the entry point.

    uv run --extra app -m app.main

Owns the main thread for the pystray event loop; the dashboard is launched as
a child process (see window.py for why). Also:

* single-instance guard, so clicking the shortcut twice doesn't leave two
  icons in the tray;
* a background status poll that keeps the icon's dot and tooltip honest
  (green = running, amber = something's down);
* a periodic update check, so a long-running tray app still notices new
  versions -- with the actual update applied by the dashboard's banner or the
  tray menu, both of which restart the app afterwards.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Binding a loopback port IS the lock (atomic, self-cleaning). A RANGE,
# because on a machine where something else owns the first port we must
# not mistake that for "rememory is already running" and refuse to start.
LOCK_PORT_RANGE = range(49517, 49527)
POLL_SECONDS = 15
UPDATE_CHECK_SECONDS = 6 * 60 * 60

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


class TrayApp:
    def __init__(self) -> None:
        from .backend import Api

        self.api = Api()
        self.icon = None
        self.window_proc: subprocess.Popen | None = None
        self.state = "unknown"
        self.update_available = False
        self._stop = threading.Event()

    # ------------------------------------------------------------- window
    def open_dashboard(self, *_args) -> None:
        """Show the dashboard, reusing the existing window if it's still open."""
        if self.window_proc is not None and self.window_proc.poll() is None:
            return  # already open; the OS will surface it
        try:
            self.window_proc = subprocess.Popen(
                [sys.executable, "-m", "app.window"], cwd=str(ROOT), **_NO_WINDOW
            )
        except OSError as exc:
            self._notify(f"Could not open the dashboard: {exc}")

    # -------------------------------------------------------- menu actions
    def _threaded(self, fn, *args) -> None:
        """Menu clicks must return immediately or the tray menu freezes."""
        threading.Thread(target=fn, args=args, daemon=True).start()

    def do_start(self, *_a) -> None:
        self._threaded(lambda: self._notify(self.api.start_stack()["message"]))

    def do_stop(self, *_a) -> None:
        self._threaded(lambda: self._notify(self.api.stop_stack()["message"]))

    def do_sync(self, *_a) -> None:
        self._notify("Syncing all projects…")
        self._threaded(lambda: self._notify(self.api.sync_all()["message"]))

    def do_backup(self, *_a) -> None:
        self._threaded(lambda: self._notify(self.api.backup_now()["message"]))

    def do_update(self, *_a) -> None:
        def run() -> None:
            info = self.api.check_update()
            if not info.get("available"):
                self._notify("You're on the latest version.")
                return
            self._notify(f"Updating to {info.get('commit', 'latest')}…")
            self._notify(self.api.apply_update()["message"])

        self._threaded(run)

    def do_repair(self, *_a) -> None:
        self._threaded(lambda: self._notify(self.api.repair()["message"]))

    def do_quit(self, *_a) -> None:
        self._stop.set()
        if self.window_proc is not None and self.window_proc.poll() is None:
            self.window_proc.terminate()
        if self.icon is not None:
            self.icon.stop()

    def _notify(self, message: str) -> None:
        """Native notification where supported; never fatal if it isn't."""
        try:
            if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", False):
                self.icon.notify(message, "rememory")
                return
        except Exception:
            pass
        print(f"rememory: {message}", file=sys.stderr)

    # -------------------------------------------------------------- polling
    def _poll(self) -> None:
        last_update_check = 0.0
        while not self._stop.is_set():
            try:
                st = self.api.status()
                svc = st.get("services", {})
                if st.get("healthy"):
                    state, tip = "ok", "rememory — running"
                elif svc.get("database"):
                    state, tip = "warn", "rememory — degraded (check the dashboard)"
                else:
                    state, tip = "warn", "rememory — stopped"
                counts = st.get("collections", {})
                if state == "ok":
                    tip += (f"\n{counts.get('code', 0)} code · {counts.get('docs', 0)} docs"
                            f" · {counts.get('memory', 0)} memories")
                if state != self.state:
                    self.state = state
                    self._refresh_icon()
                if self.icon is not None:
                    self.icon.title = tip
            except Exception:
                pass  # a status hiccup must never kill the tray

            now = time.time()
            if now - last_update_check > UPDATE_CHECK_SECONDS:
                last_update_check = now
                try:
                    info = self.api.check_update()
                    if info.get("available") and not self.update_available:
                        self.update_available = True
                        self._refresh_icon()
                        self._notify(f"Update available ({info.get('commit', '')}). "
                                     f"Open rememory to install it.")
                except Exception:
                    pass

            self._stop.wait(POLL_SECONDS)

    def _refresh_icon(self) -> None:
        from .icon import make_icon

        if self.icon is not None:
            self.icon.icon = make_icon(self.state)
            self.icon.menu = self._menu()  # relabels the update item

    # ----------------------------------------------------------------- menu
    def _menu(self):
        import pystray

        item, menu, sep = pystray.MenuItem, pystray.Menu, pystray.Menu.SEPARATOR
        return menu(
            item("Open rememory", self.open_dashboard, default=True),
            sep,
            item("Start", self.do_start),
            item("Stop", self.do_stop),
            sep,
            item("Sync all projects", self.do_sync),
            item("Back up memories", self.do_backup),
            item("Update available — install" if self.update_available
                 else "Check for updates", self.do_update),
            item("Repair…", self.do_repair),
            sep,
            item("Quit", self.do_quit),
        )

    # ----------------------------------------------------------------- run
    def run(self) -> int:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            print("The tray app needs pystray and pillow. Install them with:\n"
                  "  uv sync --extra app", file=sys.stderr)
            return 1

        import pystray

        from .icon import make_icon

        self.icon = pystray.Icon(
            "rememory", make_icon("unknown"), "rememory — starting…", menu=self._menu()
        )
        threading.Thread(target=self._poll, daemon=True).start()
        # Blocking, and on the main thread -- pystray requires this for
        # cross-platform correctness.
        self.icon.run()
        return 0


def _acquire_single_instance() -> socket.socket | None:
    """Bind a loopback port as a lock: it is atomic, needs no cleanup, and the
    OS releases it even if the process is killed (a PID file would not)."""
    ours = None
    for port in LOCK_PORT_RANGE:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            ours = sock
            break
        except OSError:
            sock.close()
            continue
    if ours is not None:
        return ours
    # Every port in the range is taken. That almost certainly means another
    # rememory tray is running -- but it could also be an unrelated app
    # squatting the whole range, so say which we think it is and let the
    # caller decide rather than silently doing nothing.
    return None


def main() -> int:
    lock = _acquire_single_instance()
    if lock is None:
        print("rememory is already running -- look for the tray icon.", file=sys.stderr)
        return 0

    app = TrayApp()
    # Heal a stopped database in the background so the tray comes up instantly.
    threading.Thread(
        target=lambda: __import__("memory_mcp.health", fromlist=["ensure_services"])
        .ensure_services(),
        daemon=True,
    ).start()
    try:
        return app.run()
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
