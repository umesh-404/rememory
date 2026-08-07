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
# The base port comes from runtime() (config/runtime.json / REMEMORY_APP_LOCK_PORT)
# like every other port in the system -- nothing may hardcode one.
from indexer.runtime import runtime as _runtime  # noqa: E402

_LOCK_BASE = _runtime()["app_lock_port"]
LOCK_PORT_RANGE = range(_LOCK_BASE, _LOCK_BASE + 10)
POLL_SECONDS = 15
UPDATE_CHECK_SECONDS = 6 * 60 * 60

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def hide_own_console() -> None:
    """Hide the console window this process was given, if any.

    Even launched through the venv's pythonw.exe, a console can still appear:
    uv's pythonw.exe is a trampoline that re-execs the console-subsystem base
    interpreter, and that child gets a console window. Explorer titles and
    icons it from the shortcut, so it shows up as an empty black window called
    "rememory" that looks like a broken dashboard -- and clicking in it puts
    the console into QuickEdit selection mode, which freezes the process at
    its next write to stdout.

    Hiding rather than calling FreeConsole deliberately: the console stays
    attached so stdout/stderr writes keep working (and keep going nowhere
    visible), which is exactly what a background app wants. Freeing it would
    invalidate the streams and risk breaking any later print().
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass  # cosmetic only -- never prevent the app from starting


def _windowless_python() -> str:
    """Interpreter to spawn children with -- pythonw.exe where it exists.

    sys.executable is not good enough on Windows: uv's venv pythonw.exe is a
    trampoline that re-execs the console-subsystem base interpreter, so
    sys.executable inside the tray reports python.exe. Spawning children with
    that relies entirely on CREATE_NO_WINDOW to suppress a console; naming
    pythonw.exe explicitly means there is no console to suppress in the first
    place.
    """
    if sys.platform == "win32":
        pythonw = ROOT / ".venv/Scripts/pythonw.exe"
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


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
                [_windowless_python(), "-m", "app.window"], cwd=str(ROOT), **_NO_WINDOW
            )
        except OSError as exc:
            self._notify(f"Could not open the dashboard: {exc}")

    def serve_open_requests(self, lock: socket.socket) -> None:
        """Serve the lock socket: identity banner, then 'open' / 'quit' verbs.

        The single-instance lock is already a listening socket, so this costs
        nothing extra. The banner ("rememory") is what lets a later launch
        tell OUR socket apart from an unrelated service squatting a port in
        the range -- without it, detection had to assume first-free-port,
        which meant a second launch simply bound the NEXT port and two tray
        icons appeared. 'open' raises the dashboard; 'quit' shuts this
        instance down (used by --replace during restarts). Errors are
        swallowed deliberately -- an unrelated program probing the port must
        never take down the tray.
        """
        while not self._stop.is_set():
            try:
                conn, _ = lock.accept()
            except OSError:
                return
            data = b""
            with conn:
                try:
                    conn.settimeout(2)
                    conn.sendall(b"rememory\n")
                    data = conn.recv(32)
                except OSError:
                    continue
            if b"quit" in data:
                self.do_quit()
                return
            if b"open" in data:
                self.open_dashboard()

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
    OS releases it even if the process is killed (a PID file would not). A
    range, because a port squatted by an unrelated app must not read as
    "rememory is already running" -- identity is checked separately, by the
    banner handshake in _find_running_instance()."""
    for port in LOCK_PORT_RANGE:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            return sock
        except OSError:
            sock.close()
            continue
    return None


def _find_running_instance() -> int | None:
    """Port of an already-running rememory tray, or None.

    Connecting is not enough -- any service could own a port in the range --
    so a peer only counts as rememory if it presents the banner. (A tray from
    a pre-banner version stays silent and is treated as foreign; the one
    transitional restart across that upgrade can briefly show two icons.)
    """
    for port in LOCK_PORT_RANGE:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                sock.settimeout(1)
                if sock.recv(16).startswith(b"rememory"):
                    return port
        except OSError:
            continue
    return None


def _send_verb(port: int, verb: bytes) -> bool:
    """Deliver 'open' or 'quit' to the instance on `port` (banner checked)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.settimeout(1)
            if not sock.recv(16).startswith(b"rememory"):
                return False
            sock.sendall(verb + b"\n")
        return True
    except OSError:
        return False


def main() -> int:
    hide_own_console()  # before anything slow, so no black window ever flashes

    # Single-instance handling, by handshake rather than by bind-failure:
    # binding "the first free port" can never DETECT a first instance (the
    # second launch just binds the next port), which is exactly how every
    # restart used to leave two tray icons -- the old one still running
    # pre-update code. --replace (used by Api.restart_app) asks the running
    # instance to quit and takes its place; a plain launch hands it an 'open'.
    replace = "--replace" in sys.argv[1:]
    running = _find_running_instance()
    if running is not None:
        if not replace:
            _send_verb(running, b"open")
            print("rememory is already running -- asked it to show the dashboard.",
                  file=sys.stderr)
            return 0
        _send_verb(running, b"quit")
        for _ in range(40):  # wait up to ~10s for the old instance to let go
            if _find_running_instance() is None:
                break
            time.sleep(0.25)

    lock = _acquire_single_instance()
    if lock is None:
        print(f"rememory could not bind any lock port ({LOCK_PORT_RANGE.start}-"
              f"{LOCK_PORT_RANGE.stop - 1} all taken by other programs) -- "
              "running without a single-instance guard is not safe, so exiting.",
              file=sys.stderr)
        return 1

    app = TrayApp()
    # Serve the lock socket so a later launch can raise the dashboard.
    threading.Thread(target=app.serve_open_requests, args=(lock,), daemon=True).start()
    # Clicking the shortcut should show the dashboard. The tray icon alone
    # looks like nothing happened -- especially now that the launcher is
    # windowless and there is no console to hint that anything started.
    # --tray-only exists for a future login-autostart, which should not steal
    # focus with a window the user did not ask for.
    if "--tray-only" not in sys.argv[1:]:
        app.open_dashboard()
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
