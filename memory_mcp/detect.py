"""Zero-config repo facts: detect what a repo uses without being told.

facts.py answers questions a project has configured (or alembic / GitHub CI,
which it finds itself). This module widens the zero-config net so a freshly
registered repo gets useful facts on day one:

  migrations    -- Django, Prisma, Rails, Drizzle, Laravel, EF Core, Flyway and
                   the filename-sequenced tools (knex, sequelize, golang-migrate,
                   goose, dbmate): latest revision, count, next value, warnings.
  task_gates    -- CI other than GitHub (GitLab, CircleCI, Azure, Bitbucket),
                   pre-commit / husky / lefthook, and local task runners
                   (make, package.json, just, Task, tox, nox, poe, hatch, pdm,
                   taskipy), normalised to {system, source, name, command}.
  toolchain     -- package managers, language versions, workspaces, default
                   branch, and the test / lint / typecheck commands to run.
  id_sequences  -- ADR directories and inline markdown IDs (D-12, REQ-003...)
                   as specs in the shape facts.max_in_sequence takes.

Safety: repo code is never executed. Everything is parsed as text, YAML
(safe_load), TOML, JSON, ast or regex. Walks skip vendored / generated dirs,
stop at depth 4 and read at most ~1 MB per file, so a 2k-file repo scans in
well under a second. Every entry names the repo-relative file it came from.
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
import tomllib
from collections import Counter, defaultdict
from configparser import ConfigParser
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "vendor", "dist", "build", ".claude",
             "__pycache__", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
             ".nox", "target", ".turbo", "coverage", ".idea", ".vscode"}
MAX_DEPTH = 4                 # directories below root that are walked
MAX_ENTRIES = 60_000          # hard stop for pathological trees
MAX_READ = 1_000_000          # bytes read from any one file
GATE_NAMES = re.compile(r"(^|[:_\-.])(test|tests|lint|typecheck|type-check|types|check|format|"
                        r"fmt|build|ci|validate|verify)($|[:_\-.])", re.I)
GATE_WORDS = re.compile(r"guardrail|gate|check|lint|test|format|typecheck|mypy|ruff|pytest|"
                        r"audit|verify|validate|conformance|smoke|scan|ci\b", re.I)


# ------------------------------------------------------------------ scanning
class _Scan:
    """One bounded walk of the repo, shared by every detector in all()."""

    def __init__(self, root: Path):
        self.root = root
        self.dirs: dict[str, list[str]] = {}   # rel dir ("" = root) -> file names
        self.truncated = False
        count = 0
        root_s = str(root)
        for cur, dirnames, filenames in os.walk(root_s):
            rel = os.path.relpath(cur, root_s).replace("\\", "/")
            rel = "" if rel == "." else rel
            depth = 0 if not rel else rel.count("/") + 1
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not (d.startswith(".") and d not in (".github", ".circleci",
                                                                     ".husky", ".config"))]
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            self.dirs[rel] = filenames
            count += len(filenames) + len(dirnames)
            if count > MAX_ENTRIES:
                self.truncated = True
                break

    def files(self):
        for d, names in self.dirs.items():
            for n in names:
                yield f"{d}/{n}" if d else n

    def dirs_named(self, name: str) -> list[str]:
        return sorted(d for d in self.dirs if d.rsplit("/", 1)[-1] == name)


def _scan(root: Path, scan: _Scan | None) -> _Scan:
    return scan if scan is not None and scan.root == root else _Scan(root)


def _read(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_READ)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _json(path: Path) -> Any:
    text = _read(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _toml(path: Path) -> dict:
    text = _read(path)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


class _YamlLoader:
    """SafeLoader that tolerates GitLab's !reference and other custom tags."""

    _cls = None

    @classmethod
    def get(cls):
        if cls._cls is None:
            import yaml

            class Loader(yaml.SafeLoader):
                pass

            def _seq(loader, node):
                if isinstance(node, yaml.SequenceNode):
                    return loader.construct_sequence(node)
                if isinstance(node, yaml.MappingNode):
                    return loader.construct_mapping(node)
                return loader.construct_scalar(node)

            Loader.add_constructor("!reference", _seq)
            Loader.add_multi_constructor("!", lambda loader, suffix, node: _seq(loader, node))
            cls._cls = Loader
        return cls._cls


def _yaml(path: Path) -> Any:
    import yaml

    text = _read(path)
    if text is None:
        return None
    try:
        return yaml.load(text, Loader=_YamlLoader.get())  # noqa: S506 -- SafeLoader subclass
    except yaml.YAMLError:
        return None


def _first_present(root: Path, names: tuple[str, ...]) -> str | None:
    """First of `names` present at root, spelled as on disk (Windows is case-blind)."""
    try:
        listing = set(os.listdir(root))
    except OSError:
        return None
    return next((n for n in names if n in listing and (root / n).is_file()), None)


def _pyproject(root: Path) -> dict:
    p = root / "pyproject.toml"
    return _toml(p) if p.is_file() else {}


def _package_json(root: Path) -> dict:
    p = root / "package.json"
    data = _json(p) if p.is_file() else None
    return data if isinstance(data, dict) else {}


def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# =============================================================== migrations
def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _django_meta(text: str) -> tuple[list, list] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            deps, replaces = [], []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                        and isinstance(stmt.targets[0], ast.Name):
                    name = stmt.targets[0].id
                    if name == "dependencies" and isinstance(stmt.value, (ast.List, ast.Tuple)):
                        # element-wise: swappable_dependency(...) entries are not literals
                        deps = [v for v in (_literal(e) for e in stmt.value.elts)
                                if isinstance(v, (tuple, list)) and len(v) == 2]
                    elif name == "replaces":
                        replaces = [v for v in (_literal(stmt.value) or [])
                                    if isinstance(v, (tuple, list)) and len(v) == 2]
            return deps, replaces
    return None


_DJANGO_FILE = re.compile(r"^(\d{4})_\w+\.py$")


def _django(root: Path, scan: _Scan) -> dict | None:
    apps: dict[str, Any] = {}
    warnings: list[str] = []
    for d in scan.dirs_named("migrations"):
        names = scan.dirs.get(d, [])
        mig = sorted(n for n in names if _DJANGO_FILE.match(n))
        if "__init__.py" not in names or not mig:
            continue
        app = d.rsplit("/", 2)[-2] if "/" in d else root.name
        nodes: dict[str, list[str]] = {}
        replaced_by: dict[str, str] = {}
        for n in mig:
            meta = _django_meta(_read(root / d / n) or "")
            if meta is None:
                continue
            deps, replaces = meta
            name = n[:-3]
            nodes[name] = [str(dn) for (da, dn) in deps if da == app]
            for _ra, rn in replaces:
                replaced_by[str(rn)] = name
        if not nodes:
            continue
        squashed = sorted({v for v in replaced_by.values()})
        live = {n: [replaced_by.get(dep, dep) for dep in deps]
                for n, deps in nodes.items() if n not in replaced_by}
        depended = {dep for deps in live.values() for dep in deps}
        leaves = sorted(n for n in live if n not in depended)
        top = max(int(n[:4]) for n in nodes)
        entry = {"source": d, "leaves": leaves, "count": len(nodes),
                 "next_number": f"{top + 1:04d}"}
        if squashed:
            entry["squashed"] = squashed
        if len(leaves) > 1:
            warnings.append(f"{app}: {len(leaves)} leaf migrations {leaves} -- conflict, "
                            f"run makemigrations --merge before adding another")
        apps[app] = entry
    if not apps:
        return None
    return {"tool": "django", "source": next(iter(apps.values()))["source"], "apps": apps,
            "latest": {a: e["leaves"] for a, e in apps.items()},
            "count": sum(e["count"] for e in apps.values()), "warnings": warnings}


_TS14 = re.compile(r"^(\d{14})_")


def _prisma(root: Path, scan: _Scan) -> list[dict]:
    out = []
    for d in scan.dirs:
        if not (d == "prisma/migrations" or d.endswith("/prisma/migrations")):
            continue
        subs = sorted(x for x in scan.dirs if x.startswith(d + "/") and x.count("/") == d.count(
            "/") + 1 and _TS14.match(x.rsplit("/", 1)[-1]))
        names = [x.rsplit("/", 1)[-1] for x in subs]
        if not names and "migration_lock.toml" not in scan.dirs[d]:
            continue
        provider = _get(_toml(root / d / "migration_lock.toml"), "provider")
        warnings = []
        empty = [n for n, s in zip(names, subs, strict=True)
                 if "migration.sql" not in scan.dirs.get(s, [])]
        if empty:
            warnings.append(f"migration dir(s) without migration.sql: {empty[:5]}")
        out.append({"tool": "prisma", "source": d, "latest": names[-1] if names else None,
                    "count": len(names), "provider": provider, "warnings": warnings})
    return out


_SCHEMA_VER = re.compile(r"Schema(?:\[[\d.]+\])?\.define\(\s*version:\s*([\d_]+)")


def _rails(root: Path, scan: _Scan) -> dict | None:
    names = sorted(n for n in scan.dirs.get("db/migrate", []) if re.match(r"^\d{14}_\w+\.rb$", n))
    if not names:
        return None
    latest = names[-1][:14]
    out = {"tool": "rails", "source": "db/migrate", "latest": names[-1][:-3],
           "latest_version": latest, "count": len(names), "warnings": []}
    m = _SCHEMA_VER.search(_read(root / "db" / "schema.rb") or "")
    if m:
        ver = m.group(1).replace("_", "")
        out["schema_version"] = ver
        if ver.ljust(14, "0") < latest:
            out["warnings"].append(f"db/schema.rb version {ver} is older than the latest "
                                   f"migration {latest} -- run db:migrate and commit schema.rb")
    return out


_DRIZZLE_OUT = re.compile(r"""\bout\s*:\s*['"`]([^'"`]+)['"`]""")


def _drizzle(root: Path, scan: _Scan) -> dict | None:
    cfg = next((n for n in sorted(scan.dirs.get("", []))
                if re.match(r"^drizzle\.config\.(ts|js|mjs|cjs|mts|json)$", n)), None)
    out_dir = "drizzle"
    if cfg:
        m = _DRIZZLE_OUT.search(_read(root / cfg) or "")
        if m:
            out_dir = m.group(1).strip().lstrip("./").rstrip("/") or "drizzle"
    journal = root / out_dir / "meta" / "_journal.json"
    if not journal.is_file():
        return {"tool": "drizzle", "source": cfg, "latest": None, "count": 0,
                "warnings": [f"no {out_dir}/meta/_journal.json yet"]} if cfg else None
    entries = [e for e in (_get(_json(journal), "entries") or []) if isinstance(e, dict)]
    last = max(entries, key=lambda e: e.get("idx", -1), default=None)
    warnings = []
    idxs = [e.get("idx") for e in entries]
    if len(set(idxs)) != len(idxs):
        warnings.append("duplicate idx values in _journal.json (merge conflict?)")
    return {"tool": "drizzle", "source": _rel(root, journal), "config": cfg,
            "latest": last.get("tag") if last else None,
            "latest_idx": last.get("idx") if last else None, "count": len(entries),
            "warnings": warnings}


_LARAVEL = re.compile(r"^(\d{4}_\d{2}_\d{2}_\d{6})_(\w+)\.php$")


def _laravel(root: Path, scan: _Scan) -> dict | None:
    names = sorted(n for n in scan.dirs.get("database/migrations", []) if _LARAVEL.match(n))
    if not names:
        return None
    return {"tool": "laravel", "source": "database/migrations", "latest": names[-1][:-4],
            "count": len(names), "warnings": []}


_EF = re.compile(r"^(\d{14})_(\w+)\.cs$")
_EF_CTX = re.compile(r"\[DbContext\(typeof\(([\w.]+)\)\)\]")


def _efcore(root: Path, scan: _Scan) -> dict | None:
    contexts: dict[str, dict] = {}
    for d in scan.dirs:
        names = scan.dirs[d]
        migs = sorted(n for n in names if _EF.match(n) and not n.endswith(".Designer.cs"))
        if not migs:
            continue
        for n in migs:
            designer = root / d / (n[:-3] + ".Designer.cs")
            m = _EF_CTX.search(_read(designer) or "") if designer.name in names else None
            ctx = m.group(1) if m else f"({d})"
            e = contexts.setdefault(ctx, {"source": d, "migrations": []})
            e["migrations"].append(n[:-3])
    if not contexts:
        return None
    by_ctx = {c: {"source": e["source"], "latest": max(e["migrations"]),
                  "count": len(e["migrations"])} for c, e in contexts.items()}
    return {"tool": "efcore", "source": next(iter(by_ctx.values()))["source"],
            "contexts": by_ctx, "latest": {c: e["latest"] for c, e in by_ctx.items()},
            "count": sum(e["count"] for e in by_ctx.values()), "warnings": []}


_FLYWAY = re.compile(r"^V(\d+(?:[._]\d+)*)__.+\.sql$")


def _flyway(root: Path, scan: _Scan) -> dict | None:
    found: list[tuple[tuple[int, ...], str, str]] = []
    dirs = dict(scan.dirs)
    deep = "src/main/resources/db/migration"   # Maven/Gradle default, below MAX_DEPTH
    if deep not in dirs and (root / deep).is_dir():
        dirs[deep] = os.listdir(root / deep)
    for d, names in dirs.items():
        for n in names:
            m = _FLYWAY.match(n)
            if m:
                ver = tuple(int(x) for x in re.split(r"[._]", m.group(1)))
                found.append((ver, n, d))
    if not found:
        return None
    found.sort()
    warnings = []
    dupes = [v for v, c in Counter(v for v, _, _ in found).items() if c > 1]
    if dupes:
        warnings.append(f"duplicate versions: {['.'.join(map(str, v)) for v in dupes[:5]]}")
    top = found[-1][0]
    nxt = ".".join(map(str, top[:-1] + (top[-1] + 1,)))
    return {"tool": "flyway", "source": found[-1][2], "latest": found[-1][1],
            "latest_version": ".".join(map(str, top)), "next_version": nxt,
            "count": len(found), "warnings": warnings}


_SEQ_FILE = re.compile(r"^(\d{1,14})[_-](.+?)\.(up\.sql|down\.sql|sql|js|cjs|mjs|ts|go)$")
_GENERIC_DIRS = ("migrations", "db/migrations", "migrate", "sql/migrations",
                 "database/migrations", "src/migrations", "db/migration")


def _generic(root: Path, scan: _Scan, claimed: set[str]) -> list[dict]:
    out = []
    pkg = _package_json(root)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    top = set(scan.dirs.get("", []))
    for d in _GENERIC_DIRS:
        if d in claimed or d not in scan.dirs:
            continue
        hits = [(m.group(1), n, m.group(3)) for n in scan.dirs[d]
                if (m := _SEQ_FILE.match(n)) and not _FLYWAY.match(n)]
        if not hits:
            continue
        exts = Counter(e for _, _, e in hits)
        sample = _read(root / d / max(hits)[1]) or ""
        if any(e.endswith(("up.sql", "down.sql")) for e in exts):
            tool = "golang-migrate"
        elif "+goose" in sample:
            tool = "goose"
        elif "migrate:up" in sample:
            tool = "dbmate"
        elif any(n.startswith("knexfile.") for n in top) or "knex" in deps:
            tool = "knex"
        elif ".sequelizerc" in top or "sequelize-cli" in deps or "sequelize" in deps:
            tool = "sequelize"
        else:
            tool = "sql-files" if "sql" in exts else "migration-files"
        nums = sorted({h[0] for h in hits}, key=int)
        width = Counter(len(n) for n in nums).most_common(1)[0][0]
        timestamp = width >= 12 and nums[-1][:2] in ("19", "20")
        latest = max(hits, key=lambda h: int(h[0]))
        warnings = []
        if not timestamp:
            dup = sorted({h[0] for h in hits if sum(1 for x in hits if x[0] == h[0]) > (
                2 if tool == "golang-migrate" else 1)})
            if dup:
                warnings.append(f"duplicate sequence numbers: {dup[:5]}")
        entry = {"tool": tool, "source": d, "latest": latest[1], "count": len(nums),
                 "style": "timestamp" if timestamp else "sequential", "width": width,
                 "warnings": warnings}
        if timestamp:
            entry["next"] = time.strftime("%Y%m%d%H%M%S", time.gmtime())[:width]
        else:
            entry["next"] = str(int(nums[-1]) + 1).zfill(width)
        out.append(entry)
    return out


def migrations(root: Path, *, _scan_: _Scan | None = None) -> list[dict]:
    """One entry per detected migration tool (alembic is left to facts.py)."""
    root = Path(root)
    scan = _scan(root, _scan_)
    out: list[dict] = []
    for fn in (_django, _rails, _drizzle, _laravel, _efcore, _flyway):
        try:
            r = fn(root, scan)
        except Exception as exc:  # a detector must never take down the rest
            r = {"tool": fn.__name__.strip("_"), "source": None,
                 "warnings": [f"detector failed: {exc}"]}
        if r:
            out.append(r)
    try:
        out.extend(_prisma(root, scan))
    except Exception as exc:
        out.append({"tool": "prisma", "source": None, "warnings": [f"detector failed: {exc}"]})
    claimed = {e["source"] for e in out if e.get("source")}
    claimed |= {a["source"] for e in out if e["tool"] == "django" for a in e["apps"].values()}
    out.extend(_generic(root, scan, claimed))
    return out


# ==================================================================== gates
def _cmd(script: Any) -> str:
    if isinstance(script, list):
        parts = []
        for s in script:
            parts.extend(s if isinstance(s, list) else [s])
        return " && ".join(" ".join(str(p).split()) for p in parts if p is not None)[:300]
    return " ".join(str(script or "").split())[:300]


def _gate(system: str, source: str, name: str, command: str, **extra) -> dict:
    g = {"system": system, "source": source, "name": name, "command": command,
         "gate": bool(GATE_WORDS.search(name) or GATE_WORDS.search(command))}
    g.update(extra)
    return g


_GITLAB_RESERVED = {"image", "services", "stages", "types", "before_script", "after_script",
                    "variables", "cache", "include", "default", "workflow", "spec"}


def _gitlab_load(root: Path, rel: str, seen: set[str], depth: int) -> list[tuple[str, dict]]:
    if rel in seen or depth > 5:
        return []
    seen.add(rel)
    data = _yaml(root / rel)
    if not isinstance(data, dict):
        return []
    docs = [(rel, data)]
    inc = data.get("include") or []
    for item in inc if isinstance(inc, list) else [inc]:
        local = item if isinstance(item, str) else (item.get("local")
                                                       if isinstance(item, dict) else None)
        if not isinstance(local, str) or "://" in local or "*" in local:
            continue
        docs.extend(_gitlab_load(root, local.lstrip("/"), seen, depth + 1))
    return docs


def _gitlab(root: Path) -> list[dict]:
    if not (root / ".gitlab-ci.yml").is_file():
        return []
    out = []
    for rel, data in _gitlab_load(root, ".gitlab-ci.yml", set(), 0):
        for name, job in data.items():
            if name in _GITLAB_RESERVED or str(name).startswith(".") or not isinstance(job, dict):
                continue
            if not any(k in job for k in ("script", "trigger", "extends", "run")):
                continue
            af = job.get("allow_failure")
            out.append(_gate("gitlab-ci", rel, str(name), _cmd(job.get("script")),
                             stage=job.get("stage", "test"), gating=not bool(af)))
    return out


def _circleci(root: Path) -> list[dict]:
    rel = ".circleci/config.yml"
    data = _yaml(root / rel)
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs") or {}
    in_workflow: set[str] = set()
    for wf in (data.get("workflows") or {}).values():
        for j in (wf.get("jobs") or []) if isinstance(wf, dict) else []:
            in_workflow.add(next(iter(j)) if isinstance(j, dict) else str(j))
    out = []
    for jname, job in jobs.items() if isinstance(jobs, dict) else []:
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            run = step.get("run") if isinstance(step, dict) else None
            if run is None:
                continue
            cmd = run.get("command") if isinstance(run, dict) else run
            sname = run.get("name") if isinstance(run, dict) else None
            out.append(_gate("circleci", rel, f"{jname}: {sname or _cmd(cmd)[:50]}", _cmd(cmd),
                             job=jname, in_workflow=jname in in_workflow or not in_workflow))
    return out


def _azure_steps(node: Any, path: str, acc: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        for key in ("script", "bash", "pwsh", "powershell"):
            if isinstance(node.get(key), str):
                acc.append((path, node.get("displayName") or "", node[key]))
        for key in ("stages", "jobs", "steps"):
            for child in node.get(key) or []:
                if isinstance(child, dict):
                    label = child.get("stage") or child.get("job") or ""
                    _azure_steps(child, f"{path}/{label}".strip("/") if label else path, acc)


def _azure(root: Path) -> list[dict]:
    out = []
    for rel in ("azure-pipelines.yml", "azure-pipelines.yaml", ".azure-pipelines.yml"):
        data = _yaml(root / rel)
        if isinstance(data, dict):
            acc: list[tuple[str, str, str]] = []
            _azure_steps(data, "", acc)
            for path, disp, script in acc:
                label = disp or _cmd(script)[:50]
                out.append(_gate("azure-pipelines", rel, f"{path}: {label}" if path else label,
                                 _cmd(script)))
    return out


def _bitbucket(root: Path) -> list[dict]:
    rel = "bitbucket-pipelines.yml"
    data = _yaml(root / rel)
    if not isinstance(data, dict):
        return []
    out = []

    def steps(items: Any, where: str) -> None:
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("step"), dict):
                s = it["step"]
                out.append(_gate("bitbucket", rel, f"{where}: {s.get('name') or 'step'}",
                                 _cmd(s.get("script"))))
            par = it.get("parallel")
            if isinstance(par, dict):
                par = par.get("steps")
            if isinstance(par, list):
                steps(par, where)

    pipes = data.get("pipelines") or {}
    if isinstance(pipes, dict):
        for kind, val in pipes.items():
            if isinstance(val, list):
                steps(val, kind)
            elif isinstance(val, dict):
                for branch, items in val.items():
                    steps(items, f"{kind}/{branch}")
    return out


def _precommit(root: Path) -> list[dict]:
    rel = ".pre-commit-config.yaml"
    data = _yaml(root / rel)
    if not isinstance(data, dict):
        return []
    out = []
    for repo in data.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        url, rev = repo.get("repo", ""), repo.get("rev")
        short = str(url).rstrip("/").rsplit("/", 1)[-1]
        for hook in repo.get("hooks") or []:
            if isinstance(hook, dict) and hook.get("id"):
                hid = str(hook["id"])
                out.append(_gate("pre-commit", rel, hid, f"pre-commit run {hid} --all-files",
                                 repo=f"{short}@{rev}" if rev else short,
                                 stages=hook.get("stages")))
    return out


def _git_hooks(root: Path) -> list[dict]:
    out = []
    husky = root / ".husky"
    if husky.is_dir():
        for f in sorted(husky.iterdir()):
            if f.is_file() and not f.name.startswith(("_", ".")):
                lines = [ln.strip() for ln in (_read(f) or "").splitlines()
                         if ln.strip() and not ln.strip().startswith("#")
                         and "husky.sh" not in ln]
                out.append(_gate("husky", f".husky/{f.name}", f.name, " && ".join(lines)[:300]))
    for rel in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml"):
        data = _yaml(root / rel)
        if not isinstance(data, dict):
            continue
        for hook, spec in data.items():
            if not isinstance(spec, dict):
                continue
            for key in ("commands", "scripts"):
                for cname, c in (spec.get(key) or {}).items():
                    run = c.get("run") if isinstance(c, dict) else ""
                    out.append(_gate("lefthook", rel, f"{hook}: {cname}", _cmd(run or cname)))
    return out


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_][\w.\-/ ]*?)\s*::?(?![=])(.*)$")


def _makefile(root: Path) -> list[dict]:
    rel = _first_present(root, ("GNUmakefile", "makefile", "Makefile"))
    if not rel:
        return []
    text = _read(root / rel) or ""
    out, seen = [], set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line or line[0] in "\t #" or "=" in line.split(":", 1)[0]:
            continue
        m = _MAKE_TARGET.match(line)
        if not m or m.group(2).lstrip().startswith("="):
            continue
        recipe = []
        for nxt in lines[i + 1:]:
            if nxt.startswith("\t"):
                recipe.append(nxt.strip().lstrip("@-+"))
            elif nxt.strip() and not nxt.startswith("#"):
                break
        for target in m.group(1).split():
            if target.startswith(".") or "%" in target or "$" in target or target in seen:
                continue
            seen.add(target)
            out.append(_gate("make", rel, target, f"make {target}",
                             recipe=" && ".join(recipe)[:300]))
    return out


def _pm_run(pm: str, name: str) -> str:
    return f"{pm} run {name}" if pm != "npm" or name not in ("test", "start") else f"npm {name}"


def _package_scripts(root: Path, pm: str, members: list[str]) -> list[dict]:
    out = []
    for rel_dir in [""] + members:
        pj = _json(root / rel_dir / "package.json") if (root / rel_dir / "package.json"
                                                         ).is_file() else None
        if not isinstance(pj, dict):
            continue
        src = f"{rel_dir}/package.json" if rel_dir else "package.json"
        for name, body in (pj.get("scripts") or {}).items():
            if not GATE_NAMES.search(name):
                continue
            cmd = _pm_run(pm, name)
            if rel_dir:
                cmd = (f"pnpm --filter {pj.get('name') or rel_dir} run {name}" if pm == "pnpm"
                       else f"{pm} --cwd {rel_dir} run {name}" if pm == "yarn"
                       else f"npm --prefix {rel_dir} run {name}")
            out.append(_gate("package.json", src, name, cmd, script=_cmd(body),
                             package=pj.get("name")))
    return out


_JUST = re.compile(r"^@?([A-Za-z_][\w-]*)(?:\s+[^:=]*)?:(?![=])")


def _justfile(root: Path) -> list[dict]:
    rel = _first_present(root, ("justfile", "Justfile", ".justfile"))
    if not rel:
        return []
    out = []
    for line in (_read(root / rel) or "").splitlines():
        if not line or line[0] in " \t#[":
            continue
        if re.match(r"^(set|alias|export|import|mod)\b", line):
            continue
        m = _JUST.match(line)
        if m:
            out.append(_gate("just", rel, m.group(1), f"just {m.group(1)}"))
    return out


def _taskfile(root: Path) -> list[dict]:
    for rel in ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"):
        data = _yaml(root / rel)
        if isinstance(data, dict):
            out = []
            for name, t in (data.get("tasks") or {}).items():
                cmds = t.get("cmds") if isinstance(t, dict) else t
                cmds = [c.get("cmd", c.get("task", "")) if isinstance(c, dict) else c
                        for c in (cmds or [])] if isinstance(cmds, list) else cmds
                out.append(_gate("task", rel, str(name), f"task {name}", recipe=_cmd(cmds)))
            return out
    return []


def _tox(root: Path, pyproject: dict) -> list[dict]:
    envs: list[tuple[str, str]] = []
    ini = root / "tox.ini"
    if ini.is_file():
        p = ConfigParser(interpolation=None)
        try:
            p.read_string(_read(ini) or "")
        except Exception:
            p = None
        if p is not None:
            listed = p.get("tox", "env_list", fallback=p.get("tox", "envlist", fallback=""))
            names = [e.strip() for e in re.split(r"[,\n]", listed) if e.strip()]
            names += [s.split(":", 1)[1] for s in p.sections() if s.startswith("testenv:")]
            envs += [(n, "tox.ini") for n in dict.fromkeys(names)]
    tt = root / "tox.toml"
    for data, src in ((_toml(tt) if tt.is_file() else {}, "tox.toml"),
                      (_get(pyproject, "tool", "tox") or {}, "pyproject.toml")):
        names = list(data.get("env_list") or []) + list((data.get("env") or {}).keys())
        envs += [(str(n), src) for n in dict.fromkeys(names)]
    return [_gate("tox", src, n, f"tox -e {n}") for n, src in envs]


def _nox(root: Path) -> list[dict]:
    f = root / "noxfile.py"
    if not f.is_file():
        return []
    try:
        tree = ast.parse(_read(f) or "")
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            label = ast.unparse(target)
            if label in ("nox.session", "session"):
                name = node.name
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(_literal(kw.value), str):
                            name = _literal(kw.value)
                out.append(_gate("nox", "noxfile.py", name, f"nox -s {name}"))
                break
    return out


def _pyproject_tasks(pyproject: dict, prefix: str) -> list[dict]:
    out = []
    tool = pyproject.get("tool") or {}
    for name, body in (_get(tool, "poe", "tasks") or {}).items():
        cmd = body if isinstance(body, str) else (
            (body.get("cmd") or body.get("shell") or body.get("script") or body.get("sequence"))
            if isinstance(body, dict) else body)
        out.append(_gate("poe", "pyproject.toml", name, f"{prefix}poe {name}", recipe=_cmd(cmd)))
    for env, spec in (_get(tool, "hatch", "envs") or {}).items():
        for name, body in (spec.get("scripts") or {}).items() if isinstance(spec, dict) else []:
            ref = name if env == "default" else f"{env}:{name}"
            out.append(_gate("hatch", "pyproject.toml", ref, f"hatch run {ref}",
                             recipe=_cmd(body)))
    for name, body in (_get(tool, "pdm", "scripts") or {}).items():
        if name.startswith("_"):
            continue
        cmd = body.get("cmd") or body.get("shell") or body.get("composite") \
            if isinstance(body, dict) else body
        out.append(_gate("pdm", "pyproject.toml", name, f"pdm run {name}", recipe=_cmd(cmd)))
    for name, body in (_get(tool, "taskipy", "tasks") or {}).items():
        cmd = body.get("cmd") if isinstance(body, dict) else body
        out.append(_gate("taskipy", "pyproject.toml", name, f"{prefix}task {name}",
                         recipe=_cmd(cmd)))
    return out


def task_gates(root: Path, *, _scan_: _Scan | None = None) -> list[dict]:
    """CI gates other than GitHub Actions plus local hooks and task runners.

    Each entry: {system, source, name, command, gate: bool, ...extras}."""
    root = Path(root)
    pyproject = _pyproject(root)
    js = _js_manager(root)
    py = _py_manager(root, pyproject)
    prefix = _PY_PREFIX.get(py["name"] if py else "", "")
    members = [m for m in _workspaces(root, pyproject).get("js", {}).get("members", [])]
    out: list[dict] = []
    calls = [lambda: _gitlab(root), lambda: _circleci(root), lambda: _azure(root),
             lambda: _bitbucket(root), lambda: _precommit(root), lambda: _git_hooks(root),
             lambda: _makefile(root), lambda: _justfile(root), lambda: _taskfile(root),
             lambda: _package_scripts(root, js["name"] if js else "npm", members),
             lambda: _tox(root, pyproject), lambda: _nox(root),
             lambda: _pyproject_tasks(pyproject, prefix)]
    for call in calls:
        try:
            out.extend(call())
        except Exception as exc:
            out.append({"system": "error", "source": None, "name": "detector failed",
                        "command": "", "gate": False, "warnings": [str(exc)]})
    return out


# ================================================================ toolchain
_JS_LOCKS = [("pnpm-lock.yaml", "pnpm"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
             ("yarn.lock", "yarn"), ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm")]
_PY_LOCKS = [("uv.lock", "uv"), ("poetry.lock", "poetry"), ("pdm.lock", "pdm"),
             ("Pipfile.lock", "pipenv"), ("pixi.lock", "pixi")]
_PY_PREFIX = {"uv": "uv run ", "poetry": "poetry run ", "pdm": "pdm run ",
              "pipenv": "pipenv run ", "pixi": "pixi run ", "hatch": "hatch run "}
_OTHER = [("Cargo.toml", "cargo", "Cargo.lock"), ("go.mod", "go", "go.sum"),
          ("Gemfile", "bundler", "Gemfile.lock"), ("composer.json", "composer", "composer.lock"),
          ("build.gradle", "gradle", None), ("build.gradle.kts", "gradle", None),
          ("pom.xml", "maven", None), ("mix.exs", "mix", "mix.lock"),
          ("pubspec.yaml", "pub", "pubspec.lock"), ("Package.swift", "swiftpm", None)]


def _js_manager(root: Path) -> dict | None:
    pkg = _package_json(root)
    locks = [(f, pm) for f, pm in _JS_LOCKS if (root / f).is_file()]
    declared = str(pkg.get("packageManager") or "")
    decl_name = declared.split("@", 1)[0] or None
    if not locks and not decl_name and not pkg:
        return None
    warnings = []
    if len({pm for _, pm in locks}) > 1:
        warnings.append(f"several lockfiles: {[f for f, _ in locks]}")
    if locks:
        name, source = locks[0][1], locks[0][0]
        if decl_name and decl_name != name:
            warnings.append(f"packageManager says {declared} but lockfile is {source}")
    elif decl_name:
        name, source = decl_name, "package.json"
    else:
        name, source = "npm", "package.json"
        warnings.append("no lockfile; npm assumed")
    out = {"name": name, "source": source, "warnings": warnings}
    if declared:
        out["declared"] = declared
    return out


def _py_manager(root: Path, pyproject: dict) -> dict | None:
    for f, name in _PY_LOCKS:
        if (root / f).is_file():
            return {"name": name, "source": f}
    tool = pyproject.get("tool") or {}
    for key in ("poetry", "pdm", "hatch", "uv"):
        if key in tool:
            return {"name": key, "source": "pyproject.toml", "warnings": ["no lockfile"]}
    if (root / "Pipfile").is_file():
        return {"name": "pipenv", "source": "Pipfile"}
    if (root / "requirements.txt").is_file():
        return {"name": "pip", "source": "requirements.txt"}
    if pyproject:
        return {"name": "pip", "source": "pyproject.toml"}
    return None


def _versions(root: Path, pyproject: dict) -> dict:
    out: dict[str, Any] = {}

    def put(lang: str, value: Any, source: str) -> None:
        if value and lang not in out:
            out[lang] = {"version": str(value).strip(), "source": source}

    pv = _read(root / ".python-version") if (root / ".python-version").is_file() else None
    put("python", (pv or "").splitlines()[0] if pv and pv.strip() else None, ".python-version")
    tv = root / ".tool-versions"
    if tv.is_file():
        for line in (_read(tv) or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                lang = {"nodejs": "node"}.get(parts[0], parts[0])
                put(lang, parts[1], ".tool-versions")
    for mise in ("mise.toml", ".mise.toml"):
        if (root / mise).is_file():
            for lang, v in (_toml(root / mise).get("tools") or {}).items():
                put(lang, v if isinstance(v, str) else (v[0] if isinstance(v, list) and v
                                                        else None), mise)
    put("python", _get(pyproject, "project", "requires-python"), "pyproject.toml")
    for f in (".nvmrc", ".node-version"):
        if (root / f).is_file():
            put("node", (_read(root / f) or "").strip().lstrip("v"), f)
    pkg = _package_json(root)
    put("node", _get(pkg, "volta", "node"), "package.json (volta)")
    put("node", _get(pkg, "engines", "node"), "package.json (engines)")
    rt = root / "rust-toolchain.toml"
    if rt.is_file():
        put("rust", _get(_toml(rt), "toolchain", "channel"), "rust-toolchain.toml")
    gm = _read(root / "go.mod") if (root / "go.mod").is_file() else None
    m = re.search(r"^go\s+([\d.]+)", gm or "", re.M)
    put("go", m.group(1) if m else None, "go.mod")
    return out


def _expand(root: Path, patterns: list[str], manifest: str) -> list[str]:
    found: list[str] = []
    for pat in patterns:
        if not isinstance(pat, str) or pat.startswith("!"):
            continue
        pat = pat.strip().rstrip("/").lstrip("./")
        if pat.count("/") > MAX_DEPTH or "**" in pat:
            pat = pat.replace("**", "*")
        try:
            hits = [pat] if not any(c in pat for c in "*?[") else [
                _rel(root, p) for p in root.glob(pat)]
        except (ValueError, OSError):
            continue
        for h in hits:
            parts = Path(h).parts
            if any(p in SKIP_DIRS for p in parts):
                continue
            if (root / h / manifest).is_file() and h not in found:
                found.append(h)
    return sorted(found)


def _workspaces(root: Path, pyproject: dict) -> dict:
    out: dict[str, Any] = {}
    pnpm = root / "pnpm-workspace.yaml"
    pkg = _package_json(root)
    if pnpm.is_file():
        pats = _get(_yaml(pnpm), "packages") or []
        out["js"] = {"tool": "pnpm", "source": "pnpm-workspace.yaml",
                     "members": _expand(root, pats, "package.json")}
    elif pkg.get("workspaces"):
        ws = pkg["workspaces"]
        pats = ws.get("packages", []) if isinstance(ws, dict) else ws
        out["js"] = {"tool": "package.json workspaces", "source": "package.json",
                     "members": _expand(root, pats, "package.json")}
    for f, tool in (("nx.json", "nx"), ("turbo.json", "turbo"), ("lerna.json", "lerna")):
        if (root / f).is_file():
            out.setdefault("monorepo_tools", []).append({"tool": tool, "source": f})
    uvws = _get(pyproject, "tool", "uv", "workspace")
    if isinstance(uvws, dict):
        members = _expand(root, uvws.get("members") or [], "pyproject.toml")
        excl = set(_expand(root, uvws.get("exclude") or [], "pyproject.toml"))
        out["python"] = {"tool": "uv", "source": "pyproject.toml",
                         "members": [m for m in members if m not in excl]}
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        cw = _get(_toml(cargo), "workspace")
        if isinstance(cw, dict):
            out["rust"] = {"tool": "cargo", "source": "Cargo.toml",
                           "members": _expand(root, cw.get("members") or [], "Cargo.toml")}
    gw = root / "go.work"
    if gw.is_file():
        text = _read(gw) or ""
        uses = re.findall(r"^\s*use\s+(\S+)", text, re.M)
        block = re.search(r"use\s*\((.*?)\)", text, re.S)
        if block:
            uses += [ln.strip() for ln in block.group(1).splitlines()
                     if ln.strip() and not ln.strip().startswith("//")]
        out["go"] = {"tool": "go.work", "source": "go.work",
                     "members": sorted({u.lstrip("./") or "." for u in uses if u != "("})}
    return out


def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_dir():
        return g
    if g.is_file():
        m = re.match(r"gitdir:\s*(.+)", (_read(g) or "").strip())
        if m:
            p = Path(m.group(1).strip())
            p = p if p.is_absolute() else (root / p).resolve()
            common = p / "commondir"   # worktree: refs live in the main repo's git dir
            if common.is_file():
                c = Path((_read(common) or "").strip())
                return c if c.is_absolute() else (p / c).resolve()
            return p
    return None


def _default_branch(root: Path) -> dict:
    gd = _git_dir(root)
    if gd is not None:
        head = gd / "refs" / "remotes" / "origin" / "HEAD"
        m = re.match(r"ref:\s*refs/remotes/origin/(.+)", (_read(head) or "").strip())
        if m:
            return {"name": m.group(1), "source": "refs/remotes/origin/HEAD"}
        cfg = _read(gd / "config") or ""
        m = re.search(r"defaultBranch\s*=\s*(\S+)", cfg)
        if m:
            return {"name": m.group(1), "source": ".git/config init.defaultBranch"}
        packed = _read(gd / "packed-refs") or ""
        for cand in ("main", "master"):
            if (gd / "refs" / "remotes" / "origin" / cand).is_file() or \
                    f"refs/remotes/origin/{cand}" in packed:
                return {"name": cand, "source": f"origin/{cand} exists"}
        for cand in ("main", "master"):
            if (gd / "refs" / "heads" / cand).is_file() or f"refs/heads/{cand}" in packed:
                return {"name": cand, "source": f"local {cand} exists",
                        "warnings": ["no origin/HEAD; guessed from local branches"]}
    return {"name": "main", "source": None, "warnings": ["no git metadata; assumed main"]}


def _deps_text(pyproject: dict) -> str:
    bits = list(_get(pyproject, "project", "dependencies") or [])
    for group in (_get(pyproject, "project", "optional-dependencies") or {}).values():
        bits += list(group or [])
    for group in (pyproject.get("dependency-groups") or {}).values():
        bits += [b for b in group or [] if isinstance(b, str)]
    bits += list(_get(pyproject, "tool", "uv", "dev-dependencies") or [])
    bits += list((_get(pyproject, "tool", "poetry", "dependencies") or {}).keys())
    for g in (_get(pyproject, "tool", "poetry", "group") or {}).values():
        bits += list((g.get("dependencies") or {}).keys()) if isinstance(g, dict) else []
    return " ".join(str(b) for b in bits).lower()


def _commands(root: Path, pyproject: dict, js: dict | None, py: dict | None,
              gates: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {k: [] for k in ("test", "lint", "typecheck", "format")}
    aliases = {"test": ("test", "tests"), "lint": ("lint",),
               "typecheck": ("typecheck", "type-check", "types", "tsc", "mypy"),
               "format": ("format", "fmt")}

    def add(kind: str, command: str, source: str | None, how: str) -> None:
        if command not in (c["command"] for c in out[kind]):
            out[kind].append({"command": command, "source": source, "how": how})

    # explicit scripts first: make, just, task, root package.json, poe/pdm/taskipy
    for system in ("make", "just", "task", "package.json", "poe", "pdm", "taskipy", "hatch"):
        for g in gates:
            if g["system"] != system or g["source"] not in (
                    "Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", ".justfile",
                    "Taskfile.yml", "Taskfile.yaml", "package.json", "pyproject.toml"):
                continue
            for kind, names in aliases.items():
                if g["name"] in names:
                    add(kind, g["command"], g["source"], "script")
    # inferred from tool configuration
    if py:
        pre = _PY_PREFIX.get(py["name"], "python -m " if py["name"] == "pip" else "")
        deps = _deps_text(pyproject)
        tool = pyproject.get("tool") or {}
        top = set(os.listdir(root))
        if "pytest" in tool or "pytest.ini" in top or "pytest" in deps or "conftest.py" in top:
            add("test", f"{pre}pytest", "pyproject.toml", "inferred")
        if "ruff" in tool or "ruff.toml" in top or ".ruff.toml" in top or "ruff" in deps:
            add("lint", f"{pre}ruff check .", "pyproject.toml", "inferred")
            add("format", f"{pre}ruff format .", "pyproject.toml", "inferred")
        elif ".flake8" in top or "flake8" in deps:
            add("lint", f"{pre}flake8", "pyproject.toml", "inferred")
        if "black" in tool or "black" in deps:
            add("format", f"{pre}black .", "pyproject.toml", "inferred")
        if "mypy" in tool or "mypy.ini" in top or "mypy" in deps:
            add("typecheck", f"{pre}mypy .", "pyproject.toml", "inferred")
        if "pyright" in tool or "pyrightconfig.json" in top or "pyright" in deps:
            add("typecheck", f"{pre}pyright", "pyproject.toml", "inferred")
    if js and (root / "tsconfig.json").is_file() and not out["typecheck"]:
        x = {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(js["name"], "npx")
        add("typecheck", f"{x} tsc --noEmit", "tsconfig.json", "inferred")
    if (root / "Cargo.toml").is_file():
        add("test", "cargo test", "Cargo.toml", "inferred")
        add("lint", "cargo clippy -- -D warnings", "Cargo.toml", "inferred")
        add("format", "cargo fmt --check", "Cargo.toml", "inferred")
    if (root / "go.mod").is_file():
        add("test", "go test ./...", "go.mod", "inferred")
        add("lint", "go vet ./...", "go.mod", "inferred")
    return out


def toolchain(root: Path, *, _gates_: list[dict] | None = None) -> dict:
    """Package managers, language versions, workspaces, default branch, and
    the test / lint / typecheck / format commands (explicit scripts first)."""
    root = Path(root)
    pyproject = _pyproject(root)
    js = _js_manager(root)
    py = _py_manager(root, pyproject)
    others = []
    for manifest, name, lock in _OTHER:
        if (root / manifest).is_file() and name not in (o["name"] for o in others):
            entry = {"name": name, "source": manifest}
            if lock:
                entry["lockfile"] = lock if (root / lock).is_file() else None
            others.append(entry)
    gates = _gates_ if _gates_ is not None else task_gates(root)
    return {
        "package_managers": {"js": js, "python": py, "other": others},
        "versions": _versions(root, pyproject),
        "workspaces": _workspaces(root, pyproject),
        "default_branch": _default_branch(root),
        "commands": _commands(root, pyproject, js, py, gates),
    }


# ============================================================= id sequences
_ADR_DIRS = ("doc/adr", "docs/adr", "docs/adrs", "adr", "docs/decisions",
             "docs/architecture/decisions", "doc/architecture/decisions", "decisions",
             "rfcs", "rfcs/text", "proposals", "docs/rfcs")
_ADR_FILE = re.compile(r"^(?:([A-Za-z]{2,6})-)?(\d{1,8})[-_].+\.(md|markdown|rst|txt)$", re.I)
_ADR_SKIP = re.compile(r"template|readme|index", re.I)
_DEF = re.compile(r"^(?:\|\s*\**\s*|#{1,6}\s+\**|\s{0,3}[-*+]\s+\**)\[?([A-Z]{1,6})-(\d{1,5})\b")
_ID_BLACKLIST = {"CVE", "ISO", "RFC", "UTF", "SHA", "HTTP", "TLS", "ES", "CWE", "GHSA", "PEP",
                 "IEC", "IEEE", "WCAG", "RGB", "UTC", "GMT", "MD", "HMAC", "AES", "RSA", "ECMA",
                 "OWASP", "NIST", "AWS", "GCP", "TCP", "UDP", "IP", "IPV", "X", "PR", "MR",
                 "HTML", "CSS", "JSON", "API", "SQL", "PDF", "PNG", "JPG", "GPT", "LLM", "UTF8",
                 "COVID", "SARS", "PCI", "SOC", "GDPR", "BS", "EN", "DIN", "ASCII", "ANSI"}
_MD_SUFFIX = (".md", ".mdx", ".markdown")


def _adr_sequences(root: Path, scan: _Scan) -> dict[str, dict]:
    dirs = list(_ADR_DIRS)
    marker = root / ".adr-dir"
    if marker.is_file():
        custom = (_read(marker) or "").strip().strip("/")
        if custom:
            dirs.insert(0, custom)
    out: dict[str, dict] = {}
    for d in dict.fromkeys(dirs):
        names = scan.dirs.get(d)
        if names is None and (root / d).is_dir():
            try:
                names = os.listdir(root / d)
            except OSError:
                names = None
        if not names:
            continue
        nums, prefixes = [], Counter()
        for n in names:
            m = _ADR_FILE.match(n)
            if not m or _ADR_SKIP.search(n):
                continue
            prefixes[m.group(1) or ""] += 1
            num = m.group(2)
            if len(num) == 8 and num[:2] in ("19", "20"):
                continue   # a date-stamped file, not a sequence number
            nums.append(num)
        if len(nums) < 1:
            continue
        width = Counter(len(n) for n in nums).most_common(1)[0][0]
        top = max(int(n) for n in nums)
        key = "ADR" if "ADR" not in out else f"ADR:{d}"
        if d.startswith("rfcs") or "rfcs" in d:
            key = "RFC" if "RFC" not in out else f"RFC:{d}"
        pre = prefixes.most_common(1)[0][0]
        lead = rf"{re.escape(pre)}-" if pre else ""
        fmt = (f"{pre}-" if pre else "") + (f"{{n:0{width}d}}" if width > 1 else "{n}")
        out[key] = {"kind": "adr", "source": d, "match": "filename",
                    "pattern": rf"^{lead}(\d{{{width}}})[-_]", "paths": [d], "format": fmt,
                    "max": top, "next": fmt.format(n=top + 1), "count": len(nums),
                    "warnings": [] if Counter(nums).most_common(1)[0][1] == 1 else
                    ["duplicate ADR numbers"]}
    return out


def _inline_sequences(root: Path, scan: _Scan) -> dict[str, dict]:
    defs: dict[str, Counter] = defaultdict(Counter)   # prefix -> file -> count
    nums: dict[str, list[str]] = defaultdict(list)
    for rel in scan.files():
        if not rel.lower().endswith(_MD_SUFFIX):
            continue
        text = _read(root / rel)
        if not text or "-" not in text:
            continue
        for line in text.splitlines():
            m = _DEF.match(line)
            if m and m.group(1) not in _ID_BLACKLIST:
                defs[m.group(1)][rel] += 1
                nums[m.group(1)].append(m.group(2))
    out: dict[str, dict] = {}
    for prefix, files in sorted(defs.items(), key=lambda kv: -sum(kv[1].values())):
        values = nums[prefix]
        distinct = len(set(int(v) for v in values))
        if sum(files.values()) < 3 or distinct < 3:
            continue
        # a lone letter in one file is usually a local list label, not a project sequence
        confidence = "high" if distinct >= 10 and len(files) >= 2 else (
            "low" if len(prefix) == 1 and len(files) == 1 or distinct < 5 else "medium")
        padded = [v for v in values if len(v) > 1 and v.startswith("0")]
        width = Counter(len(v) for v in values).most_common(1)[0][0] if padded else 0
        fmt = f"{prefix}-{{n:0{width}d}}" if width else f"{prefix}-{{n}}"
        # restrict to where definitions live; collapse busy dirs to the dir
        by_dir: dict[str, list[str]] = defaultdict(list)
        for f in files:
            by_dir[f.rsplit("/", 1)[0] if "/" in f else ""].append(f)
        paths: list[str] = []
        for d, fs in sorted(by_dir.items()):
            paths.extend([d] if d and len(fs) > 3 else sorted(fs))
        top = max(int(v) for v in values)
        dupes = sorted({int(v) for v, c in Counter(int(x) for x in values).items() if c > 1})
        warnings = [f"{prefix} numbers defined more than once: {dupes[:5]}"] if dupes else []
        out[prefix] = {"kind": "inline", "source": files.most_common(1)[0][0],
                       "pattern": rf"\b{prefix}-(\d+)\b", "paths": paths, "format": fmt,
                       "max": top, "next": fmt.format(n=top + 1),
                       "definitions": sum(files.values()), "confidence": confidence,
                       "warnings": warnings}
    return out


def id_sequences(root: Path, *, _scan_: _Scan | None = None) -> dict[str, dict]:
    """Auto-detected sequences, each a spec facts.max_in_sequence accepts
    ({pattern, paths, format}) plus kind/source/max/next/warnings.

    kind "adr" specs carry match="filename": the number lives in file names,
    so use max_in_filenames() for them, not facts.max_in_sequence."""
    root = Path(root)
    scan = _scan(root, _scan_)
    out = _adr_sequences(root, scan)
    for k, v in _inline_sequences(root, scan).items():
        out.setdefault(k, v)
    return out


def max_in_filenames(root: Path, spec: dict) -> tuple[int, str | None]:
    """Like facts.max_in_sequence but matches the pattern against file names
    in spec["paths"] (for ADR-style sequences)."""
    pattern = re.compile(spec.get("pattern") or r"^(\d+)[-_]")
    best, where = 0, None
    for rel in spec.get("paths") or []:
        base = Path(root) / rel
        if not base.is_dir():
            continue
        for name in os.listdir(base):
            m = pattern.search(name)
            if m and not _ADR_SKIP.search(name):
                try:
                    n = int(m.group(1))
                except (IndexError, ValueError):
                    continue
                if len(m.group(1)) == 8 and m.group(1)[:2] in ("19", "20"):
                    continue
                if n > best:
                    best, where = n, f"{rel}/{name}"
    return best, where


# ======================================================================= all
def all(root: Path) -> dict:  # noqa: A001 -- the requested public name
    """Every detector, sharing one walk. Never raises for a readable root."""
    root = Path(root)
    t0 = time.perf_counter()
    scan = _Scan(root)
    out: dict[str, Any] = {"root": str(root), "warnings": []}
    if scan.truncated:
        out["warnings"].append(f"walk stopped after {MAX_ENTRIES} entries; results partial")
    steps = (("migrations", lambda: migrations(root, _scan_=scan)),
             ("task_gates", lambda: task_gates(root, _scan_=scan)),
             ("toolchain", lambda: toolchain(root, _gates_=out.get("task_gates") or [])),
             ("id_sequences", lambda: id_sequences(root, _scan_=scan)))
    for key, fn in steps:
        try:
            out[key] = fn()
        except Exception as exc:
            out[key] = None
            out["warnings"].append(f"{key} failed: {type(exc).__name__}: {exc}")
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out
