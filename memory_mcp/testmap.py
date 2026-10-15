"""Which tests cover this file? -- from coverage data, so a change can be
checked by running 80 tests instead of a two-hour suite.

Source of truth: a coverage.py data file recorded WITH per-test contexts
(`[run] dynamic_context = test_function`, or pytest-cov `--cov-context=test`).
Contexts do not change any line or branch count -- a coverage ratchet scores
the same run identically -- they only remember WHICH test executed each line.
rememory snapshots that file (SQLite backup API, so a run still writing it is
never read half-done) into data/testmap/<project>.sqlite whenever the
project's .coverage is newer than the last snapshot; every sync checks.

Without context data the answer degrades, honestly labelled, to a heuristic:
test files that import the module or carry its name.

The data file schema read here (file, context, line_bits, arc) is
coverage.py's documented SQLite format; numbits are decoded locally so the
coverage package is not a dependency.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "testmap"
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _snapshot(project: str) -> Path:
    return DATA_DIR / f"{project}.sqlite"


def _meta_path(project: str) -> Path:
    return DATA_DIR / f"{project}.json"


def _read_meta(project: str) -> dict:
    try:
        return json.loads(_meta_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def numbits_to_nums(blob: bytes) -> list[int]:
    out = []
    for i, byte in enumerate(blob or b""):
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << bit):
                out.append(i * 8 + bit)
    return out


def _context_count(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute("SELECT COUNT(*) FROM context WHERE context != ''").fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------------------------- ingest
def ingest(project: str, root: Path, coverage_file: str | None = None,
           *, force: bool = False) -> dict[str, Any]:
    src = Path(coverage_file) if coverage_file else Path(root) / ".coverage"
    if not src.is_absolute():
        src = Path(root) / src
    if not src.exists():
        return {"ingested": False, "reason": f"no coverage data at {src}"}
    mtime = src.stat().st_mtime
    meta = _read_meta(project)
    if not force and meta.get("source") == str(src) and meta.get("source_mtime") == mtime:
        return {"ingested": False, "reason": "already current", **meta}
    try:
        contexts = _context_count(src)
    except sqlite3.Error as exc:
        return {"ingested": False, "reason": f"not a coverage SQLite file ({exc})"}
    if contexts == 0:
        return {
            "ingested": False,
            "reason": "this coverage run recorded no per-test contexts, so it cannot say "
                      "which test covers what",
            "fix": f"record the next run with contexts: {rcfile_hint(project, root)}",
        }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _snapshot(project).with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    tmp.replace(_snapshot(project))
    meta = {"source": str(src), "source_mtime": mtime, "contexts": contexts,
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _meta_path(project).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return {"ingested": True, **meta}


def auto_ingest(project: str, root: Path) -> dict | None:
    """Cheap check called after every sync: re-snapshot only when newer."""
    src = Path(root) / ".coverage"
    try:
        if not src.exists():
            return None
        meta = _read_meta(project)
        if meta.get("source_mtime") == src.stat().st_mtime:
            return None
        if not meta and time.time() - src.stat().st_mtime < 5:
            return None  # a run may still be writing it; next sync gets it
        return ingest(project, root)
    except Exception as exc:  # advisory -- never break a sync
        return {"ingested": False, "reason": f"{type(exc).__name__}: {exc}"}


# -------------------------------------------------------------------- query
def _test_id(context: str, root: Path) -> str:
    """Normalise a context to something pytest accepts."""
    ctx = re.sub(r"\|(run|setup|teardown)$", "", context)
    if "::" in ctx or "/" in ctx:
        return ctx  # pytest-cov --cov-context=test already gives node ids
    # dynamic_context=test_function gives `pkg.mod.Class.test_fn`; find the
    # longest dotted prefix that is a real file under the root.
    parts = ctx.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = Path(root, *parts[:cut]).with_suffix(".py")
        if candidate.exists():
            rel = candidate.relative_to(root).as_posix()
            return rel + "::" + "::".join(parts[cut:])
    return ctx


def tests_for(project: str, root: Path, path: str, start: int | None = None,
              end: int | None = None, limit: int = 200) -> dict[str, Any]:
    rel = path.replace("\\", "/").removeprefix("./")
    snap = _snapshot(project)
    meta = _read_meta(project)
    if not snap.exists():
        return _heuristic(project, root, rel, why="no per-test coverage ingested yet")
    conn = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute("SELECT id, path FROM file").fetchall()
        file_ids = [fid for fid, p in rows
                    if p.replace("\\", "/") == rel or p.replace("\\", "/").endswith("/" + rel)]
        if not file_ids:
            return _heuristic(project, root, rel,
                              why="file not present in the ingested coverage run")
        contexts = dict(conn.execute("SELECT id, context FROM context").fetchall())
        hits: dict[int, set[int]] = {}
        q = ",".join("?" * len(file_ids))
        for ctx_id, blob in conn.execute(
                f"SELECT context_id, numbits FROM line_bits WHERE file_id IN ({q})", file_ids):
            hits.setdefault(ctx_id, set()).update(numbits_to_nums(blob))
        for ctx_id, a, b in conn.execute(
                f"SELECT context_id, fromno, tono FROM arc WHERE file_id IN ({q})", file_ids):
            s = hits.setdefault(ctx_id, set())
            if a > 0:
                s.add(a)
            if b > 0:
                s.add(b)
    finally:
        conn.close()

    lo = start or 0
    hi = end or 10**9
    tests: dict[str, int] = {}
    for ctx_id, lines in hits.items():
        ctx = contexts.get(ctx_id, "")
        if not ctx:
            continue
        n = sum(1 for ln in lines if lo <= ln <= hi)
        if n:
            tid = _test_id(ctx, Path(root))
            tests[tid] = max(tests.get(tid, 0), n)
    ranked = sorted(tests.items(), key=lambda kv: (-kv[1], kv[0]))
    ids = [t for t, _ in ranked[:limit]]
    result: dict[str, Any] = {
        "source": "coverage contexts",
        "coverage_run": meta.get("ingested_at"),
        "file": rel,
        "lines": f"{start}-{end}" if start or end else "all",
        "tests": len(tests),
        "test_ids": ids,
    }
    if len(tests) > limit:
        result["truncated"] = f"showing the {limit} tests touching the most lines"
    if ids:
        files = sorted({t.split("::")[0] for t in ids if "::" in t})
        result["run"] = ("uv run pytest " + " ".join(files)) if files and len(files) <= 40 \
            else f"{len(files)} test files -- run by node id list"
    else:
        result["note"] = "No recorded test executes these lines: they are uncovered."
    try:
        src_mtime = (Path(root) / rel).stat().st_mtime
        if meta.get("source_mtime") and src_mtime > meta["source_mtime"]:
            result["warning"] = ("the file changed after this coverage run; new code paths "
                                 "may have no mapping yet")
    except OSError:
        pass
    return result


def _heuristic(project: str, root: Path, rel: str, *, why: str) -> dict[str, Any]:
    root = Path(root)
    stem = Path(rel).stem
    module = rel[:-3].replace("/", ".") if rel.endswith(".py") else None
    tails = []
    if module:
        parts = module.split(".")
        tails = [".".join(parts[i:]) for i in range(len(parts) - 1) if len(parts) - i >= 2]
    found: dict[str, str] = {}
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
            timeout=20, check=False, stdin=subprocess.DEVNULL, **NO_WINDOW,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        listed = []
    test_files = [f for f in listed if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", f)
                  or re.search(r"\.(test|spec)\.[jt]sx?$", f)]
    # A generic file name (page.tsx, index.ts, utils.py) says nothing on its
    # own; require its parent folder too, or every test in the repo matches.
    parent_dir = Path(rel).parent.name
    generic = stem.lower() in GENERIC_STEMS
    key = f"{parent_dir}/{stem}" if generic and parent_dir else stem
    for f in test_files:
        if not generic and stem and stem in Path(f).stem:
            found[f] = "name matches"
    pattern = None
    if tails:
        alts = [re.escape(t) for t in tails[:3]]
        parent = module.rsplit(".", 1)[0] if module and "." in module else ""
        if parent:
            # `from apps.api.core import alerting` names the module only as a word.
            alts.append(rf"from +[A-Za-z0-9_.]*{re.escape(parent.split('.')[-1])} +import +"
                        rf"([^#]*[ ,(])?{re.escape(stem)}([ ,)]|$)")
        pattern = "|".join(alts)
    elif stem:
        # JS/TS (and most other ecosystems): an import/require naming the
        # module file, e.g. from './Button' or require("../lib/button.js").
        pattern = rf"""['"/]{re.escape(key)}(\.[cm]?[jt]sx?)?['"]"""
    if pattern:
        try:
            out = subprocess.run(
                # Pathspec globs, not the file list: hundreds of explicit
                # paths overflow the Windows command line and git grep fails.
                ["git", "-C", str(root), "grep", "-l", "-E", pattern, "--",
                 "*_test.py", "test_*.py", "*.test.*", "*.spec.*"],
                capture_output=True, text=True, timeout=30, check=False,
                stdin=subprocess.DEVNULL, **NO_WINDOW,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            out = []
        for f in out:
            if f in test_files:
                found.setdefault(f, "imports the module")
    files = sorted(found)
    return {
        "source": "heuristic (imports / file names) -- NOT coverage",
        "why": why,
        "file": rel,
        "test_files": files[:80],
        "count": len(files),
        "run": _run_command(root, rel, files),
        "for_exact_mapping": (rcfile_hint(project, root) if rel.endswith(".py")
                              else _js_related(root, rel)),
    }


GENERIC_STEMS = {"index", "page", "layout", "route", "routes", "utils", "util", "types",
                 "main", "app", "mod", "lib", "helpers", "constants", "config", "service",
                 "models", "schemas", "api", "__init__", "loading", "error", "default"}


def _run_command(root: Path, rel: str, files: list[str]) -> str | None:
    if not files:
        return None
    py = [f for f in files if f.endswith(".py")]
    if py:
        return "uv run pytest " + " ".join(py[:40])
    runner = _js_runner(root, rel)
    return f"{runner} {' '.join(files[:40])}" if runner else None


def _js_runner(root: Path, rel: str = "") -> str | None:
    """Test runner from the package.json nearest the file (monorepos keep one
    per app), falling back to the repo root's."""
    pkg = None
    here = (Path(root) / rel).parent if rel else Path(root)
    for d in [here, *here.parents]:
        try:
            pkg = json.loads((d / "package.json").read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            pass
        if d == Path(root):
            break
    if pkg is None:
        return None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if "vitest" in deps:
        return "npx vitest run"
    if "jest" in deps:
        return "npx jest"
    return None


def _js_related(root: Path, rel: str) -> str:
    """JS runners can select tests by import graph without any coverage."""
    runner = _js_runner(root, rel)
    if runner == "npx vitest run":
        return f"npx vitest related {rel} --run   (import-graph exact, no coverage needed)"
    if runner == "npx jest":
        return f"npx jest --findRelatedTests {rel}   (import-graph exact, no coverage needed)"
    return "per-test coverage is only ingested from coverage.py today"


# ------------------------------------------------------------------ rcfile
def rcfile(project: str, root: Path) -> Path:
    """Write a coverage rcfile = the project's own [tool.coverage.*] config +
    dynamic_context = test_function. Same counts, plus per-test contexts."""
    import tomllib

    root = Path(root)
    sections: dict[str, dict] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        sections = dict((data.get("tool") or {}).get("coverage") or {})
    run = dict(sections.get("run") or {})
    run["dynamic_context"] = "test_function"
    sections["run"] = run
    lines = [f"# Generated by rememory for {project}: the project's own coverage settings",
             "# plus per-test contexts. Line/branch counts are unchanged.", ""]
    for name, values in sections.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{name}]")
        for k, v in values.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, list):
                v = "\n    " + "\n    ".join(str(x) for x in v)
            elif isinstance(v, dict):
                v = "\n    " + "\n    ".join(f"{a} = {b}" for a, b in v.items())
            lines.append(f"{k} = {v}")
        lines.append("")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{project}.coveragerc"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def rcfile_hint(project: str, root: Path) -> str:
    try:
        path = rcfile(project, root)
    except Exception as exc:
        return f"(could not build rcfile: {exc})"
    return (f"uv run python -m coverage run --rcfile=\"{path}\" -m pytest ... "
            f"(rememory picks the new .coverage up on its next sync)")
