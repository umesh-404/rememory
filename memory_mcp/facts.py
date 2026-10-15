"""Repo facts that agents keep guessing -- read from the source of truth.

A guessed fact fails late and expensively: a migration written against the
wrong alembic head, a decision number two parallel agents both took, a new
alarm missing the severity row a CI check requires. Every fact here is
computed from the files at call time (never cached, never stored as a memory
that could rot), so the answer is exactly as current as the working tree.

Built-in, auto-detected (zero config; detection rules live in detect.py):
  alembic    -- current head revision(s), from alembic.ini's script_location.
  migrations -- latest migration for Django, Prisma, Rails, Drizzle, Laravel,
                EF Core, Flyway, golang-migrate/goose/dbmate/knex/sequelize.
  ci         -- gate-like steps of each GitHub workflow job, plus GitLab,
                CircleCI, Azure, Bitbucket, pre-commit, Makefile, package.json
                scripts, just, Taskfile, tox, nox, poe/hatch/pdm/taskipy.
  toolchain  -- package managers, language versions, workspaces, default
                branch, and the exact test/lint/typecheck commands.
  ids        -- ADR folders and inline decision ids (D-123) as sequences.

Configured per project in config/projects.yaml under `facts:`:

  facts:
    id_sequences:            # next free number, reservable atomically
      D:
        pattern: '\\bD-(\\d+)\\b'
        paths: [docs, CLAUDE.md]
        format: 'D-{n}'
    constants:               # a Python literal: list/tuple/set items or dict keys
      append_only_tables: {file: apps/api/db/registry.py, symbol: APPEND_ONLY_TABLES}
    checks:                  # every code must appear in every set
      alarm_wiring:
        description: what a new alarm needs
        sets:
          severity: {file: apps/api/core/alarm_severity.py, symbol: ALARM_SEVERITY}
          index: {file: runbooks/alarm-index.md, heading: '## Alarm codes',
                  pattern: '^\\|\\s*`([a-z][a-z0-9_]*)`\\s*\\|'}
        verify: uv run python -m scripts.check_alarm_wiring
    notes:                   # short static facts worth one lookup
      - 'Workspace packages come from uv sync --all-packages'
"""

from __future__ import annotations

import ast
import re
from configparser import ConfigParser
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".claude", "dist",
             "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".md", ".mdx", ".txt", ".rst", ".py", ".ts", ".tsx", ".js", ".yml",
                 ".yaml", ".toml", ".json", ".sql", ".sh"}
GATE_WORDS = re.compile(r"guardrail|gate|check|lint|test|format|typecheck|mypy|ruff|"
                        r"pytest|audit|verify|conformance|smoke|scan", re.I)
MAX_FILE_BYTES = 8_000_000


# ------------------------------------------------------------------ alembic
def _alembic_versions_dir(root: Path) -> Path | None:
    ini = root / "alembic.ini"
    if ini.exists():
        parser = ConfigParser(interpolation=None)
        try:
            parser.read(ini, encoding="utf-8")
            loc = parser.get("alembic", "script_location", fallback="alembic")
        except Exception:
            loc = "alembic"
        loc = loc.replace("%(here)s", str(root))
        candidate = Path(loc) if Path(loc).is_absolute() else root / loc
        if (candidate / "versions").is_dir():
            return candidate / "versions"
    for guess in (root / "alembic" / "versions", root / "migrations" / "versions"):
        if guess.is_dir():
            return guess
    return None


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def alembic_heads(root: Path) -> dict[str, Any] | None:
    versions = _alembic_versions_dir(root)
    if versions is None:
        return None
    revs: dict[str, dict[str, Any]] = {}
    downs: set[str] = set()
    for path in sorted(versions.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rev = down = None
        for node in tree.body:
            target, value = None, None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            if target == "revision":
                rev = _literal(value)
            elif target == "down_revision":
                down = _literal(value)
        if not isinstance(rev, str):
            continue
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        revs[rev] = {"revision": rev, "file": path.relative_to(root).as_posix(),
                     "summary": doc[0][:160] if doc else ""}
        if isinstance(down, str):
            downs.add(down)
        elif isinstance(down, (tuple, list)):
            downs.update(d for d in down if isinstance(d, str))
    heads = [info for rev, info in revs.items() if rev not in downs]
    return {
        "heads": heads,
        "multiple_heads": len(heads) > 1,
        "revisions": len(revs),
        "note": ("Use this as down_revision for a new migration."
                 if len(heads) == 1 else
                 "More than one head: a merge migration is needed before adding another."),
    }


# ----------------------------------------------------------------------- ci
def ci_steps(root: Path) -> list[dict[str, Any]] | None:
    import yaml

    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return None
    out = []
    for wf in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        try:
            data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            out.append({"workflow": wf.name, "error": f"unparseable: {exc}"})
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            gates = []
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                name = step.get("name") or ""
                run = " ".join(str(step.get("run") or "").split())
                if not run or not (GATE_WORDS.search(name) or GATE_WORDS.search(run)):
                    continue
                gates.append({"name": name or run[:60], "run": run[:240]})
            out.append({"workflow": wf.name, "job": job_id,
                        "name": job.get("name") or job_id, "gates": gates})
    return out


# -------------------------------------------------------------- id sequences
def _iter_text_files(root: Path, rels: list[str]):
    for rel in rels:
        base = root / rel
        if base.is_file():
            yield base
            continue
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def max_in_sequence(root: Path, spec: dict) -> tuple[int, str | None]:
    """Highest number the pattern matches anywhere in the configured paths,
    and where it was found."""
    pattern = re.compile(spec.get("pattern") or r"\bD-(\d+)\b")
    best, where = 0, None
    for path in _iter_text_files(root, list(spec.get("paths") or ["docs"])):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in pattern.finditer(text):
            try:
                n = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if n > best:
                best, where = n, path.relative_to(root).as_posix()
    return best, where


# ---------------------------------------------------------------- constants
def python_constant(root: Path, file: str, symbol: str) -> list[str] | None:
    """Items of a module-level list/tuple/set (or keys of a dict) literal,
    unwrapping frozenset(...)/set(...)/tuple(...)/list(...) calls."""
    path = root / file
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        value = None
        assign = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == symbol for t in node.targets)
        annotated = (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                     and node.target.id == symbol)
        if assign or annotated:
            value = node.value
        if value is None:
            continue
        while isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in ("frozenset", "set", "tuple", "list", "dict",
                                      "MappingProxyType") and value.args:
            value = value.args[0]
        if isinstance(value, ast.Dict):
            return [k.value for k in value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return [e.value for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return None
    return None


def markdown_rows(root: Path, file: str, heading: str | None, pattern: str) -> list[str] | None:
    path = root / file
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if heading:
        start = text.find(heading)
        if start < 0:
            return []
        level = len(heading) - len(heading.lstrip("#"))
        rest = text[start + len(heading):]
        nxt = re.search(rf"^#{{1,{max(level, 1)}}} ", rest, re.M)
        text = rest[: nxt.start()] if nxt else rest
    return list(dict.fromkeys(re.findall(pattern, text, re.M)))


def _resolve_set(root: Path, spec: dict) -> list[str] | None:
    if "symbol" in spec:
        return python_constant(root, spec["file"], spec["symbol"])
    if "pattern" in spec:
        return markdown_rows(root, spec["file"], spec.get("heading"), spec["pattern"])
    return None


def run_check(root: Path, name: str, spec: dict) -> dict[str, Any]:
    sets: dict[str, set[str]] = {}
    prefixes: dict[str, tuple[str, ...]] = {}
    where: dict[str, str] = {}
    errors = []
    for label, sspec in (spec.get("sets") or {}).items():
        values = _resolve_set(root, sspec)
        if values is None:
            errors.append(f"{label}: could not read {sspec.get('file')}"
                          f"{'::' + sspec['symbol'] if 'symbol' in sspec else ''}")
            continue
        sets[label] = set(values)
        # `families`: a second literal of PREFIXES that also satisfy the set
        # (e.g. one severity row covering every `http_*` code).
        fam = sspec.get("families")
        if fam:
            prefixes[label] = tuple(_resolve_set(root, fam) or ())
        where[label] = sspec.get("file", "?") + (
            f"::{sspec['symbol']}" if "symbol" in sspec
            else f" ({sspec.get('heading', 'table')})")
    universe = set().union(*sets.values()) if sets else set()
    gaps = {}
    for code in sorted(universe):
        missing = [
            label for label, vals in sets.items()
            if code not in vals
            and not (prefixes.get(label) and code.startswith(prefixes[label]))
        ]
        if missing:
            gaps[code] = missing
    return {
        "check": name,
        "description": spec.get("description", ""),
        "every_item_must_appear_in": where,
        "counts": {label: len(v) for label, v in sets.items()},
        "inconsistent": dict(list(gaps.items())[:60]),
        "inconsistent_total": len(gaps),
        "verify_with": spec.get("verify"),
        "errors": errors,
    }


# ------------------------------------------------------------- sequences
_SEQ_CACHE: dict[str, tuple[float, dict]] = {}
_SEQ_TTL = 300.0  # detection walks the docs; numbers change rarely


def sequences(project) -> dict[str, dict]:
    """Configured id_sequences, plus confidently auto-detected ones
    (ADR folders, inline 'D-123'-style decision ids) for zero-config repos.
    Configuration always wins for a name it defines."""
    import time

    configured = dict((project.facts or {}).get("id_sequences") or {})
    if (project.facts or {}).get("auto_detect") is False:
        return configured
    key = str(project.root)
    hit = _SEQ_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _SEQ_TTL:
        auto = hit[1]
    else:
        try:
            from . import detect

            auto = {name: spec for name, spec in detect.id_sequences(Path(project.root)).items()
                    if spec.get("kind") == "adr" or (
                        spec.get("confidence") == "high"
                        # A single-letter prefix is often a local list
                        # (F-1..F-27 repeated per doc), not a project-wide
                        # sequence; demand a long run before trusting it.
                        and (len(name) > 1 or int(spec.get("max") or 0) >= 50))}
        except Exception:
            auto = {}
        _SEQ_CACHE[key] = (time.monotonic(), auto)
    merged = {**auto, **configured}
    return merged


def _sequence_max(root: Path, spec: dict) -> tuple[int, str | None]:
    if spec.get("match") == "filename":
        from . import detect

        return detect.max_in_filenames(root, spec)
    return max_in_sequence(root, spec)


# ------------------------------------------------------------------- facade
def collect(project, topic: str | None = None, reservations_fn=None) -> dict[str, Any]:
    root = Path(project.root)
    cfg = project.facts or {}
    out: dict[str, Any] = {"project": project.name}
    want = (topic or "all").lower()

    if want in ("all", "alembic"):
        heads = alembic_heads(root)
        if heads is not None:
            out["alembic"] = heads
    if want in ("all", "ci", "gates"):
        steps = ci_steps(root)
        if steps is not None:
            out["ci"] = steps
        try:
            from . import detect

            other = [g for g in detect.task_gates(root) if g.get("gate", True)]
            if other:
                out["local_and_other_ci_gates"] = other[:80]
        except Exception as exc:
            out.setdefault("warnings", []).append(f"gate detection failed: {exc}")
    if want in ("all", "migrations", "alembic"):
        try:
            from . import detect

            found = detect.migrations(root)
            if found:
                out["migrations"] = found
        except Exception as exc:
            out.setdefault("warnings", []).append(f"migration detection failed: {exc}")
    if want in ("all", "toolchain", "commands"):
        try:
            from . import detect

            out["toolchain"] = detect.toolchain(root)
        except Exception as exc:
            out.setdefault("warnings", []).append(f"toolchain detection failed: {exc}")
    if want in ("all", "ids", "id_sequences"):
        seqs = {}
        for name, spec in sequences(project).items():
            top, where = _sequence_max(root, spec)
            pending = reservations_fn(name, top) if reservations_fn else []
            nxt = max([top] + [r["number"] for r in pending]) + 1
            fmt = spec.get("format") or (name + "-{n}")
            seqs[name] = {"source": "configured" if name in (cfg.get("id_sequences") or {})
                          else f"auto-detected ({spec.get('kind', 'inline')})",
                          "highest_in_repo": fmt.format(n=top) if top else None,
                          "found_in": where,
                          "reserved_not_yet_in_repo": pending,
                          "next_free": fmt.format(n=nxt),
                          "how_to_take_it": f"reserve_id(project, '{name}') -- never "
                                            f"just use next_free; parallel agents race."}
        if seqs:
            out["id_sequences"] = seqs
    if want in ("all", "constants"):
        consts = {}
        for name, spec in (cfg.get("constants") or {}).items():
            vals = _resolve_set(root, spec)
            consts[name] = {"source": f"{spec.get('file')}::{spec.get('symbol', '')}",
                            "values": vals if vals is not None else "UNREADABLE"}
        if consts:
            out["constants"] = consts
    if want in ("all", "checks"):
        checks = [run_check(root, name, spec) for name, spec in (cfg.get("checks") or {}).items()]
        if checks:
            out["checks"] = checks
    if want in ("all", "notes") and cfg.get("notes"):
        out["notes"] = list(cfg["notes"])
    if topic and want not in ("all", "alembic", "ci", "gates", "ids", "id_sequences",
                              "constants", "checks", "notes", "migrations", "toolchain",
                              "commands"):
        # A named constant or check.
        if topic in (cfg.get("constants") or {}):
            spec = cfg["constants"][topic]
            out[topic] = _resolve_set(root, spec)
        elif topic in (cfg.get("checks") or {}):
            out[topic] = run_check(root, topic, cfg["checks"][topic])
        else:
            named = list(cfg.get("constants") or {}) + list(cfg.get("checks") or {})
            out["error"] = (f"unknown topic {topic!r}; use alembic, migrations, ci, toolchain, "
                            f"ids, constants, checks, notes, or a configured name: "
                            f"{', '.join(named)}")
    return out


def sequence_floor(project, sequence: str) -> int:
    spec = sequences(project).get(sequence)
    if spec is None:
        raise KeyError(sequence)
    return _sequence_max(Path(project.root), spec)[0]


def sequence_format(project, sequence: str) -> str:
    spec = sequences(project).get(sequence) or {}
    return spec.get("format") or (sequence + "-{n}")
