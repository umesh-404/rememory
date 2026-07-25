"""Everything the desktop app can DO -- the single implementation of every
button's action, shared by the dashboard window and the tray menu.

Design rules:

* Every method returns a plain dict/list (JSON-serialisable) so it can cross
  the pywebview JS bridge unchanged.
* Nothing here raises into the UI: failures come back as
  {"ok": False, "message": "<human sentence>"} and the UI shows a toast. A
  control panel that throws a traceback at you is worse than no panel.
* No shared state with the tray process. Every action is a subprocess call
  (docker / uv / git) or an HTTP call to localhost, so any process can do it
  independently -- which is what makes the two-process design safe.
* Long actions block their caller (JS awaits the promise, UI shows a spinner)
  but always under a timeout, so the window can never hang forever.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from indexer.runtime import compose_env, ollama_url, qdrant_url

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SETTINGS_FILE = DATA / "app-settings.json"
QDRANT = qdrant_url()
OLLAMA = ollama_url()
IS_WINDOWS = platform.system() == "Windows"

# Hide console windows for child processes on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if IS_WINDOWS else {}


# --------------------------------------------------------------------- helpers
def _run(args: list[str], timeout: int = 30, cwd: Path | None = None, env: dict | None = None):
    """Run a command, never raise. Returns CompletedProcess or None."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd or ROOT), check=False, env=env, **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _get_json(url: str, timeout: float = 6.0):
    """GET and parse JSON, or None.

    The timeout is deliberately generous. Qdrant runs in Docker, and with the
    WSL2 backend the first connection after an idle period regularly takes
    several seconds to be forwarded. At the old 2-second limit a perfectly
    healthy database intermittently probed as offline, which is what made the
    dashboard's Docker and Database cards flicker between states.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _uv() -> str:
    """Absolute uv path -- the app may be launched without a shell PATH."""
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    for c in (
        Path.home() / "AppData/Local/Microsoft/WinGet/Links/uv.exe",
        Path.home() / ".local/bin/uv.exe",
        Path.home() / ".local/bin/uv",
    ):
        if c.exists():
            return str(c)
    return "uv"


def _app_launcher() -> list[str]:
    """Command prefix that starts the GUI app WITHOUT a console window.

    On Windows this must be pythonw.exe. uv.exe is a console-subsystem
    program, so launching through `uv run` always creates a console; it shows
    up as a black window titled "rememory" that looks like a broken dashboard,
    and if the user clicks in it, console QuickEdit selection mode freezes the
    app at its next write to stdout.
    """
    if IS_WINDOWS:
        pythonw = ROOT / ".venv/Scripts/pythonw.exe"
        if pythonw.exists():
            return [str(pythonw)]
        return [_uv(), "run", "--extra", "app", "--directory", str(ROOT)]
    venv_python = ROOT / ".venv/bin/python"
    if venv_python.exists():
        return [str(venv_python)]
    return [_uv(), "run", "--extra", "app", "--directory", str(ROOT)]


def _ok(message: str, **extra) -> dict:
    return {"ok": True, "message": message, **extra}


def _err(message: str, **extra) -> dict:
    return {"ok": False, "message": message, **extra}


# ------------------------------------------------------------------- the API
class Api:
    """Exposed to JavaScript as `pywebview.api.*` and used by the tray menu."""

    # Scheduled-task registration changes only when setup or repair runs, but
    # status() is polled every 15 seconds by the tray. Querying it each time
    # spawned two schtasks.exe processes per poll -- thousands a day -- and on
    # a machine under memory pressure Windows eventually refuses to start
    # them, raising a modal "unable to start correctly (0xc000012d)"
    # (STATUS_COMMITMENT_LIMIT) over the dashboard. Cache it.
    _TASKS_TTL_SECONDS = 600

    def __init__(self) -> None:
        self._window = None  # set by window.py once the window exists
        self._tasks_cache: dict | None = None
        self._tasks_checked_at = 0.0
        self._version_cache: dict | None = None
        self._db_failures = 0
        self._db_was_up = False

    def _scheduled_tasks(self, force: bool = False) -> dict:
        """Which background tasks are registered, cached for _TASKS_TTL_SECONDS.

        A task whose query could not even be launched is reported as None
        ("unknown"), not False: failing to start schtasks.exe says nothing
        about whether the task exists, and showing "not scheduled" would send
        the user off repairing something that is fine.
        """
        if not IS_WINDOWS:
            return {}
        now = time.time()
        if (
            not force
            and self._tasks_cache is not None
            and now - self._tasks_checked_at < self._TASKS_TTL_SECONDS
        ):
            return self._tasks_cache

        tasks: dict = {}
        for task in ("RememorySync", "RememoryBackup"):
            r = _run(["schtasks", "/Query", "/TN", task], timeout=8)
            tasks[task] = None if r is None else (r.returncode == 0)
        self._tasks_cache = tasks
        self._tasks_checked_at = now
        return tasks

    def bind_window(self, window) -> None:
        self._window = window

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        """Everything the Overview tab shows. Cheap enough to poll."""
        # One failed probe is not evidence of an outage: a busy machine or a
        # cold WSL2 port forward can lose a single request. Retry once, then
        # require two consecutive failures before reporting Offline, so a
        # healthy database stops flickering in the UI.
        qdrant_up = _get_json(f"{QDRANT}/collections") is not None
        if not qdrant_up:
            qdrant_up = _get_json(f"{QDRANT}/collections", timeout=10.0) is not None
        if qdrant_up:
            self._db_failures = 0
        else:
            self._db_failures += 1
            if self._db_failures < 2 and self._db_was_up:
                qdrant_up = True  # ride out a single blip
        self._db_was_up = qdrant_up

        # Ask the database first, and only shell out to docker when it is
        # unreachable. `docker info` is a heavyweight process launch, and this
        # runs on a 15-second poll; skipping it in the common case (everything
        # running) removes thousands of process spawns a day.
        #
        # It also removes a contradiction the UI used to show: if the docker
        # CLI failed to *launch*, docker_up went False while the database card
        # said Running, so the dashboard reported Docker down and Qdrant up at
        # the same time. A reachable database means its container is running,
        # which means Docker is running.
        if qdrant_up:
            docker_up = True
        else:
            docker = _run(["docker", "info", "--format", "ok"], timeout=8)
            docker_up = bool(docker and docker.returncode == 0)
        ollama = _get_json(f"{OLLAMA}/api/tags")
        ollama_up = ollama is not None

        models = []
        if ollama:
            models = [m.get("name", "") for m in ollama.get("models", [])]
        needed = ["qwen3-embedding", "Qwen3-Reranker"]
        models_ready = all(any(n.lower() in m.lower() for m in models) for n in needed)

        collections = {}
        if qdrant_up:
            for name in ("code", "docs", "memory"):
                info = _get_json(f"{QDRANT}/collections/{name}")
                collections[name] = (
                    (info or {}).get("result", {}).get("points_count", 0) if info else 0
                )

        tasks = self._scheduled_tasks()

        loaded = self._loaded_models() if ollama_up else []
        ours = self._our_models()
        ours_loaded = [n for n in ours if any(
            n == m or n.split(":")[0] == m.split(":")[0] for m in loaded)]

        return {
            "services": {
                "docker": docker_up,
                "database": qdrant_up,
                "models": ollama_up and models_ready,
                "ollama": ollama_up,
            },
            "models": {
                "installed": models_ready,
                "loaded": len(ours_loaded),
                "total": len(ours),
            },
            "collections": collections,
            "tasks": tasks,
            "healthy": docker_up and qdrant_up and ollama_up and models_ready,
            "version": self._version(),
            "platform": platform.system(),
            "root": str(ROOT),
        }

    def _version(self) -> dict:
        """Installed commit, cached for the life of the process.

        This is shown under the sidebar brand and cannot change while the app
        runs: applying an update restarts it. Re-running `git log` on every
        15-second status poll was one more needless process launch.
        """
        if self._version_cache is not None:
            return self._version_cache
        r = _run(["git", "log", "-1", "--format=%h|%cs|%s"], timeout=8)
        if r and r.returncode == 0 and r.stdout.strip():
            h, date, subject = (r.stdout.strip().split("|", 2) + ["", ""])[:3]
            self._version_cache = {"commit": h, "date": date, "subject": subject}
        else:
            # Not cached: a missing answer here is usually git still warming
            # up or absent from PATH, and it costs nothing to try again later.
            return {"commit": "unknown", "date": "", "subject": ""}
        return self._version_cache

    # ------------------------------------------------------- stack control
    def start_stack(self) -> dict:
        """Start Docker (if installed and stopped), then the database."""
        if not _get_json(f"{QDRANT}/collections"):
            docker = _run(["docker", "info", "--format", "ok"], timeout=10)
            if not (docker and docker.returncode == 0):
                launched = self._launch_docker_desktop()
                if not launched:
                    return _err("Docker isn't running and Docker Desktop could not be "
                                "launched. Start Docker Desktop, then press Start again.")
                for _ in range(40):  # Docker Desktop is slow; ~2 min ceiling
                    time.sleep(3)
                    d = _run(["docker", "info", "--format", "ok"], timeout=8)
                    if d and d.returncode == 0:
                        break
                else:
                    return _err("Docker Desktop is still starting. Give it a moment "
                                "and press Start again.")

            up = self._compose("up", "-d")
            if not up:
                return _err("Could not start the database container. Try Repair.")

            for _ in range(30):
                if _get_json(f"{QDRANT}/readyz", timeout=1.5) is not None or \
                        _get_json(f"{QDRANT}/collections") is not None:
                    break
                time.sleep(1)

        if not _get_json(f"{OLLAMA}/api/tags"):
            self._launch_ollama()
            for _ in range(10):
                time.sleep(2)
                if _get_json(f"{OLLAMA}/api/tags"):
                    break

        # Bring OUR models back into memory (Stop unloaded them). Pre-warming
        # here means the first search after Start is instant instead of paying
        # a model load; other models are never touched.
        warmed = 0
        if _get_json(f"{OLLAMA}/api/tags"):
            for name in self._our_models():
                if self._load_model(name):
                    warmed += 1

        st = self.status()
        detail = f" {warmed} model{'s' if warmed != 1 else ''} loaded and ready." if warmed else ""
        return (_ok(f"rememory is running.{detail}") if st["healthy"]
                else _ok(f"Started what I could -- check the status cards.{detail}"))

    def stop_stack(self) -> dict:
        """Stop ONLY rememory's own pieces.

        Two things, both surgically scoped:
          * the `rememory-qdrant` container -- via compose, which by
            definition only knows about our own service, so other containers
            are untouched;
          * our two Ollama models, unloaded from memory by NAME. Other models
            stay loaded and Ollama itself keeps running, because both are
            shared with whatever else you use them for.
        """
        stopped_db = self._compose("stop")
        unloaded = [name for name in self._our_models() if self._unload_model(name)]

        freed = f" Freed {len(unloaded)} model{'s' if len(unloaded) != 1 else ''} from memory." \
            if unloaded else ""
        if stopped_db:
            return _ok(f"rememory stopped.{freed} Other containers, other models "
                       f"and Ollama itself were left running.")
        return _err("Could not stop the database container (is Docker running?)."
                    + (f"{freed}" if unloaded else ""))

    # ------------------------------------------------------- ollama models
    def _our_models(self) -> list[str]:
        """The two models rememory owns, read from config so this can never
        drift from what the indexer and reranker actually use."""
        names: list[str] = []
        try:
            import yaml

            cfg = yaml.safe_load((ROOT / "config" / "embedding.yaml").read_text(encoding="utf-8"))
            embed = (cfg.get("model") or {}).get("name")
            rerank = (cfg.get("reranker") or {}).get("model")
            names = [n for n in (embed, rerank) if n]
        except Exception:
            pass
        return names

    def _unload_model(self, name: str) -> bool:
        """Evict one model from memory. `keep_alive: 0` is Ollama's documented
        way to unload immediately; we try the embedding endpoint first and the
        generate endpoint second, because a model only answers on one of them."""
        for path, payload in (
            ("/api/embed", {"model": name, "input": "", "keep_alive": 0}),
            ("/api/generate", {"model": name, "prompt": "", "keep_alive": 0}),
        ):
            try:
                req = urllib.request.Request(
                    f"{OLLAMA}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=20):
                    pass
            except OSError:
                continue
            # Ollama frees the model asynchronously: /api/ps can still list it
            # for a moment after the request returns. Poll briefly rather than
            # checking once, or a successful unload reads as a failure.
            for _ in range(10):
                if not self._model_loaded(name):
                    return True
                time.sleep(0.4)
        return not self._model_loaded(name)

    def _load_model(self, name: str, keep_alive: str = "30m") -> bool:
        """Pre-warm a model so the first search after Start is instant."""
        for path, payload in (
            ("/api/embed", {"model": name, "input": "warm", "keep_alive": keep_alive}),
            ("/api/generate", {"model": name, "prompt": "hi", "raw": True,
                               "stream": False, "options": {"num_predict": 1},
                               "keep_alive": keep_alive}),
        ):
            try:
                req = urllib.request.Request(
                    f"{OLLAMA}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=180):
                    pass
            except OSError:
                continue
            if self._model_loaded(name):
                return True
        return self._model_loaded(name)

    def _loaded_models(self) -> list[str]:
        data = _get_json(f"{OLLAMA}/api/ps", timeout=4) or {}
        return [m.get("name", "") for m in data.get("models", [])]

    def _model_loaded(self, name: str) -> bool:
        return any(name == m or name.split(":")[0] == m.split(":")[0]
                   for m in self._loaded_models())

    def _compose(self, *args: str) -> bool:
        compose = ROOT / "docker" / "compose.yml"
        env = compose_env()
        r = _run(["docker", "compose", "-f", str(compose), *args], timeout=180, env=env)
        if r and r.returncode == 0:
            return True
        r = _run(["docker-compose", "-f", str(compose), *args], timeout=180, env=env)
        return bool(r and r.returncode == 0)

    def _launch_docker_desktop(self) -> bool:
        candidates = {
            "Windows": [Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                        / "Docker/Docker/Docker Desktop.exe"],
            "Darwin": [Path("/Applications/Docker.app")],
        }.get(platform.system(), [])
        for path in candidates:
            if path.exists():
                try:
                    if platform.system() == "Darwin":
                        subprocess.Popen(["open", "-a", str(path)])
                    else:
                        subprocess.Popen([str(path)], **_NO_WINDOW)
                    return True
                except OSError:
                    continue
        return False

    def _launch_ollama(self) -> bool:
        if IS_WINDOWS:
            exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama app.exe"
            if exe.exists():
                try:
                    subprocess.Popen([str(exe)], **_NO_WINDOW)
                    return True
                except OSError:
                    return False
        try:
            subprocess.Popen(["ollama", "serve"], **_NO_WINDOW)
            return True
        except OSError:
            return False

    # ----------------------------------------------------------- projects
    def projects(self) -> dict:
        """Registered projects with live chunk counts and last-indexed time."""
        try:
            import yaml
        except ImportError:
            return {"ok": False, "message": "PyYAML missing", "projects": []}

        registry = ROOT / "config" / "projects.yaml"
        if not registry.exists():
            return {"ok": True, "projects": []}
        try:
            data = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            return {"ok": False, "message": f"projects.yaml unreadable: {exc}", "projects": []}

        out = []
        for name, spec in (data.get("projects") or {}).items():
            spec = spec or {}
            root = str(spec.get("root", ""))
            resolved = root if Path(root).is_absolute() else str((ROOT / root).resolve())
            entry = {
                "name": name,
                "root": resolved,
                "description": (spec.get("description") or "").strip(),
                "exists": Path(resolved).is_dir(),
                "code": 0, "docs": 0, "memory": 0, "last_indexed": None,
            }
            for coll in ("code", "docs", "memory"):
                entry[coll] = self._count(coll, name)
            entry["last_indexed"] = self._last_indexed(name)
            out.append(entry)
        return {"ok": True, "projects": out}

    def _count(self, collection: str, project: str) -> int:
        body = json.dumps({
            "exact": True,
            "filter": {"must": [{"key": "project", "match": {"value": project}}]},
        }).encode()
        req = urllib.request.Request(
            f"{QDRANT}/collections/{collection}/points/count",
            data=body, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.load(resp).get("result", {}).get("count", 0)
        except (OSError, ValueError):
            return 0

    def _last_indexed(self, project: str) -> str | None:
        newest = None
        for coll in ("code", "docs"):
            body = json.dumps({
                "limit": 1,
                "filter": {"must": [{"key": "project", "match": {"value": project}}]},
                "order_by": {"key": "indexed_at", "direction": "desc"},
                "with_payload": ["indexed_at"],
            }).encode()
            req = urllib.request.Request(
                f"{QDRANT}/collections/{coll}/points/scroll",
                data=body, headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=4) as resp:
                    pts = json.load(resp).get("result", {}).get("points", [])
                for p in pts:
                    ts = (p.get("payload") or {}).get("indexed_at")
                    if ts and (newest is None or ts > newest):
                        newest = ts
            except (OSError, ValueError):
                continue
        return newest

    def pick_folder(self) -> dict:
        """Native folder picker -- so adding a project needs no typed paths."""
        if self._window is None:
            return _err("No window available for the folder picker.")
        try:
            import webview

            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return _err(f"Folder picker unavailable: {exc}")
        if not result:
            return {"ok": False, "cancelled": True, "message": "No folder chosen."}
        return _ok("Folder selected.", path=str(result[0]))

    def add_project(self, name: str, root: str, description: str) -> dict:
        """Register + index. Mirrors the MCP register_project tool's rules so
        the app and the assistant can never disagree about validity."""
        import re

        try:
            import yaml
        except ImportError:
            return _err("PyYAML missing.")

        slug = (name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", slug):
            return _err("Name must be lowercase letters, digits and hyphens (e.g. my-app).")
        folder = Path((root or "").strip()).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            return _err("Pick an existing project folder.")
        if not (description or "").strip():
            return _err("Add a one-line description -- it shows in briefings.")

        registry = ROOT / "config" / "projects.yaml"
        data = (yaml.safe_load(registry.read_text(encoding="utf-8")) or {}) if registry.exists() else {}  # noqa: E501
        projects = data.get("projects") or {}

        resolved = folder.resolve()
        for other, spec in projects.items():
            if other == slug:
                continue
            other_root = str((spec or {}).get("root", ""))
            other_path = Path(other_root) if Path(other_root).is_absolute() else ROOT / other_root
            try:
                if other_path.resolve() == resolved:
                    return _err(f"That folder is already registered as '{other}'.")
            except OSError:
                continue

        existing = projects.get(slug) or {}
        projects[slug] = {
            "root": str(resolved),
            "description": description.strip(),
            "extra_ignore_dirs": existing.get("extra_ignore_dirs", []),
            "include_only": existing.get("include_only", []),
        }
        data["projects"] = projects
        registry.write_text(
            "# rememory project registry (yours; gitignored).\n"
            "# Managed by the app and the register_project tool -- hand-editing also works.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        indexed = self.sync_project(slug)
        return _ok(f"'{slug}' registered and indexed." if indexed.get("ok")
                   else f"'{slug}' registered; indexing needs a retry.")

    def remove_project(self, name: str) -> dict:
        """Unregister and purge its DERIVED chunks. Memories are kept on
        purpose -- they are authored knowledge, not a rebuildable copy."""
        try:
            import yaml
        except ImportError:
            return _err("PyYAML missing.")
        registry = ROOT / "config" / "projects.yaml"
        if not registry.exists():
            return _err("No registry file.")
        data = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
        projects = data.get("projects") or {}
        if name not in projects:
            return _err(f"'{name}' is not registered.")
        del projects[name]
        data["projects"] = projects
        registry.write_text(
            "# rememory project registry (yours; gitignored).\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        for coll in ("code", "docs"):
            body = json.dumps({
                "filter": {"must": [{"key": "project", "match": {"value": name}}]}
            }).encode()
            req = urllib.request.Request(
                f"{QDRANT}/collections/{coll}/points/delete?wait=true",
                data=body, headers={"Content-Type": "application/json"}, method="POST",
            )
            with contextlib.suppress(OSError):
                urllib.request.urlopen(req, timeout=30).close()
        return _ok(f"'{name}' removed. Its stored memories were kept.")

    # --------------------------------------------------------------- sync
    def sync_all(self) -> dict:
        r = _run([_uv(), "run", "--directory", str(ROOT), "-m", "indexer.cli", "sync"],
                 timeout=1800)
        if r is None:
            return _err("Could not run the indexer (is uv installed?).")
        if "Another index/sync is already running" in (r.stdout or ""):
            return _ok("A sync is already running -- it will finish on its own.")
        if r.returncode != 0:
            return _err("Sync failed. Check that Docker and Ollama are running.")
        return _ok("All projects synced.")

    def sync_project(self, name: str) -> dict:
        r = _run([_uv(), "run", "--directory", str(ROOT), "-m", "indexer.cli",
                  "index", "--project", name, "--changed"], timeout=1800)
        if r is None:
            return _err("Could not run the indexer.")
        if r.returncode != 0:
            return _err(f"Indexing '{name}' failed.")
        return _ok(f"'{name}' is up to date.")

    def reindex_project(self, name: str) -> dict:
        r = _run([_uv(), "run", "--directory", str(ROOT), "-m", "indexer.cli",
                  "index", "--project", name, "--reset"], timeout=3600)
        if r is None or r.returncode != 0:
            return _err(f"Full re-index of '{name}' failed.")
        return _ok(f"'{name}' fully re-indexed.")

    # ----------------------------------------------------------- memories
    def memories(self, query: str = "", limit: int = 25) -> dict:
        """Browse or search stored knowledge. Search goes through the real
        retrieval pipeline, so the app sees exactly what the assistant sees."""
        code = (
            "import json,sys\n"
            "from indexer.config import load_config\n"
            "cfg=load_config()\n"
            f"q={query!r}\n"
            "if q.strip():\n"
            "    from memory_mcp.search import Searcher\n"
            "    hits=Searcher(cfg).search('memory', q, limit=%d, rerank=False)\n" % int(limit) +
            "    out=[{'id':h.id,'title':h.extra.get('title'),'memory_type':h.extra.get('memory_type'),"  # noqa: E501
            "'project':h.project,'tags':h.extra.get('tags',[]),'created_at':h.extra.get('created_at'),"  # noqa: E501
            "'content':h.content,'score':round(h.score,3)} for h in hits]\n"
            "else:\n"
            "    from memory_mcp.memories import MemoryStore\n"
            f"    ms=MemoryStore(cfg); items=ms.list_memories(limit={int(limit)})\n"
            "    out=[]\n"
            "    for m in items:\n"
            "        full=ms._get(m['id']).payload or {}\n"
            "        m['content']=full.get('content','')\n"
            "        out.append(m)\n"
            "sys.stdout.write('@@'+json.dumps(out))\n"
        )
        r = _run([_uv(), "run", "--directory", str(ROOT), "python", "-c", code], timeout=120)
        if r is None or r.returncode != 0 or "@@" not in (r.stdout or ""):
            return {"ok": False, "message": "Could not read memories (is the database running?)",
                    "memories": []}
        try:
            payload = json.loads(r.stdout.split("@@", 1)[1])
        except ValueError:
            return {"ok": False, "message": "Unexpected response.", "memories": []}
        return {"ok": True, "memories": payload}

    def delete_memory(self, memory_id: str) -> dict:
        body = json.dumps({"points": [memory_id]}).encode()
        req = urllib.request.Request(
            f"{QDRANT}/collections/memory/points/delete?wait=true",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=20).close()
        except OSError as exc:
            return _err(f"Delete failed: {exc}")
        return _ok("Memory deleted.")

    # ------------------------------------------------------------ updates
    def check_update(self) -> dict:
        """Ask the git origin whether newer commits exist."""
        if not (ROOT / ".git").exists():
            return {"ok": True, "available": False, "message": "Not a git checkout."}
        remotes = _run(["git", "remote"], timeout=10)
        if not remotes or "origin" not in (remotes.stdout or "").split():
            return {"ok": True, "available": False, "message": "No update remote configured."}
        if not _run(["git", "fetch", "--quiet", "origin"], timeout=30):
            return {"ok": True, "available": False, "message": "Could not reach GitHub."}
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        name = (branch.stdout.strip() if branch else "") or "main"
        behind = _run(["git", "rev-list", "--count", f"HEAD..origin/{name}"], timeout=10)
        count = int((behind.stdout or "0").strip() or 0) if behind else 0
        latest = _run(["git", "log", "-1", "--format=%h|%s", f"origin/{name}"], timeout=10)
        commit, subject = ((latest.stdout.strip() if latest else "") + "|").split("|")[:2]
        dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=10)
        return {
            "ok": True,
            "available": count > 0,
            "count": count,
            "commit": commit,
            "subject": subject,
            "blocked": bool(dirty and dirty.stdout.strip()),
            "message": (f"{count} update{'s' if count != 1 else ''} available"
                        if count else "You're on the latest version."),
        }

    def apply_update(self) -> dict:
        """Fast-forward, then restart the app so the new UI/code is live."""
        info = self.check_update()
        if not info.get("available"):
            return _ok("Already up to date.")
        if info.get("blocked"):
            return _err("You have local changes in the rememory folder, so the "
                        "update was skipped. Commit or discard them first.")
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        name = (branch.stdout.strip() if branch else "") or "main"
        merged = _run(["git", "merge", "--ff-only", f"origin/{name}"], timeout=60)
        if not merged or merged.returncode != 0:
            return _err("Could not fast-forward (histories diverged). "
                        "Run 'git pull --rebase' in the rememory folder.")
        _run([_uv(), "sync", "--extra", "app"], timeout=600)
        self.restart_app()
        return _ok("Updated. Restarting the app...")

    def restart_app(self) -> dict:
        """Relaunch the tray app detached, then exit this process tree."""
        try:
            subprocess.Popen(
                [*_app_launcher(), "-m", "app.main"],
                cwd=str(ROOT), **_NO_WINDOW,
            )
        except OSError as exc:
            return _err(f"Could not restart: {exc}")

        def _die() -> None:
            time.sleep(1.5)
            os._exit(0)

        import threading

        threading.Thread(target=_die, daemon=True).start()
        return _ok("Restarting...")

    # -------------------------------------------------------- maintenance
    def backup_now(self) -> dict:
        r = _run([_uv(), "run", "--directory", str(ROOT), "scripts/export_memory.py"],
                 timeout=300)
        if r is None or r.returncode != 0:
            return _err("Backup failed (is the database running?).")
        line = [x for x in (r.stdout or "").splitlines() if "exported" in x]
        return _ok(line[0].strip() if line else "Backup written to data/backups.")

    def repair(self) -> dict:
        """Launch the idempotent setup in a VISIBLE terminal -- repair is long
        and chatty, and hiding it would look like a hang."""
        try:
            if IS_WINDOWS:
                subprocess.Popen([
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "scripts" / "repair-rememory.ps1"),
                ], cwd=str(ROOT), creationflags=0x00000010)  # CREATE_NEW_CONSOLE
            else:
                subprocess.Popen(["bash", str(ROOT / "setup.sh")], cwd=str(ROOT))
        except OSError as exc:
            return _err(f"Could not start repair: {exc}")
        return _ok("Repair started in a new window.")

    def open_path(self, which: str) -> dict:
        targets = {
            "root": ROOT, "backups": DATA / "backups", "logs": DATA / "logs",
            "config": ROOT / "config", "registry": ROOT / "config" / "projects.yaml",
        }
        target = targets.get(which)
        if target is None or not Path(target).exists():
            return _err("That location doesn't exist yet.")
        try:
            if IS_WINDOWS:
                os.startfile(str(target))  # noqa: S606
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            return _err(f"Could not open: {exc}")
        return _ok("Opened.")

    def open_url(self, url: str) -> dict:
        if not url.startswith(("http://", "https://")):
            return _err("Refused a non-http link.")
        import webbrowser

        webbrowser.open(url)
        return _ok("Opened in your browser.")

    def connection_config(self) -> dict:
        """The MCP snippet for this machine, ready to copy into any client."""
        cfg = {"mcpServers": {"rememory": {
            "command": _uv(),
            "args": ["run", "--directory", str(ROOT), "-m", "memory_mcp.server"],
        }}}
        cli = (f'claude mcp add --scope user rememory -- "{_uv()}" run '
               f'--directory "{ROOT}" -m memory_mcp.server')
        return {"ok": True, "json": json.dumps(cfg, indent=2), "cli": cli}

    # --------------------------------------------------------- settings
    def settings(self) -> dict:
        data = {"auto_update": True, "launch_at_login": self._autostart_enabled()}
        if SETTINGS_FILE.exists():
            with contextlib.suppress(ValueError):
                data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        data["launch_at_login"] = self._autostart_enabled()
        return {"ok": True, "settings": data}

    def set_setting(self, key: str, value) -> dict:
        if key == "launch_at_login":
            return self._set_autostart(bool(value))
        current = self.settings()["settings"]
        current[key] = value
        current.pop("launch_at_login", None)
        DATA.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return _ok("Saved.")

    def _startup_shortcut(self) -> Path:
        return (Path(os.environ.get("APPDATA", Path.home()))
                / "Microsoft/Windows/Start Menu/Programs/Startup/rememory.lnk")

    def _autostart_enabled(self) -> bool:
        return self._startup_shortcut().exists() if IS_WINDOWS else False

    def _set_autostart(self, enable: bool) -> dict:
        if not IS_WINDOWS:
            return _err("Launch at login is Windows-only for now.")
        link = self._startup_shortcut()
        if not enable:
            try:
                link.unlink(missing_ok=True)
            except OSError as exc:
                return _err(f"Could not remove: {exc}")
            return _ok("rememory will no longer start at login.")
        ps = (
            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
            f"$s.TargetPath='{_uv()}';"
            f"$s.Arguments='run --extra app --directory \"{ROOT}\" -m app.main';"
            f"$s.WorkingDirectory='{ROOT}';$s.WindowStyle=7;$s.Save()"
        )
        r = _run(["powershell.exe", "-NoProfile", "-Command", ps], timeout=30)
        if r is None or r.returncode != 0:
            return _err("Could not create the startup shortcut.")
        return _ok("rememory will start automatically at login.")

    def quit_app(self) -> dict:
        os._exit(0)
