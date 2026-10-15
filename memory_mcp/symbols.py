"""Exactly the code needed: one symbol's body, its callers, what it raises.

Files in real projects are long and comment-heavy; reading 600 lines to edit
10 burns context on every task. This module answers "show me X" with X's
own lines -- read LIVE from disk, so an edit made a minute ago is reflected
even before the index syncs -- plus who calls it and what it raises, which
are the two questions usually asked next.

Definitions are located through the code index (tree-sitter symbol chunks),
then re-anchored in the current file by the definition line, so stale line
numbers in the index never produce the wrong lines. Callers come from
`git grep` (fast, honours .gitignore) with each hit attributed to the
function that contains it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
DEF_KEYWORDS = r"(?:async\s+def|def|class|function\*?|interface|type|enum|struct|fn|func)"
PY_EXT = {".py", ".pyi"}
KEYWORDS = {"if", "for", "while", "return", "print", "len", "str", "int", "dict", "list",
            "set", "tuple", "isinstance", "super", "range", "enumerate", "getattr", "hasattr",
            "setattr", "sorted", "min", "max", "any", "all", "bool", "float", "zip", "map",
            "filter", "repr", "type", "open", "iter", "next", "await", "elif", "not", "and",
            "or", "with", "except", "lambda", "yield", "assert", "del", "in", "is", "function",
            "switch", "catch", "typeof", "new", "throw", "raise", "sum", "abs", "round"}


def _def_regex(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(
        rf"^(?P<indent>\s*)(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|"
        rf"static\s+|async\s+|pub\s+)*(?:{DEF_KEYWORDS}\s+{n}\b|(?:const|let|var)\s+{n}\s*[:=]|"
        rf"{n}\s*[:=]\s*(?:async\s*)?\(|{n}\s*\([^)]*\)\s*\{{)"
    )


def _block_end(lines: list[str], start: int, python: bool) -> int:
    """0-based inclusive end line of the block starting at `start`."""
    if python:
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start
        i = start + 1
        # Multi-line signature: continue until the line ending with ':'.
        while i < len(lines) and not lines[end].rstrip().endswith(":") and i - start < 30:
            end = i
            i += 1
        for j in range(i, len(lines)):
            text = lines[j]
            if not text.strip():
                continue
            ind = len(text) - len(text.lstrip())
            if ind <= indent and not text.lstrip().startswith((")", "]", "}")):
                break
            end = j
        return end
    depth, seen = 0, False
    for j in range(start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return j
        if not seen and j > start and lines[j].rstrip().endswith(";"):
            return j  # a one-liner (type alias, arrow fn without braces)
    return min(len(lines) - 1, start + 200)


def inside(root: Path, rel: str) -> bool:
    """root/rel stays inside root (no '..' escape, no absolute path)."""
    try:
        return (Path(root) / rel).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _locate_python(text: str, name: str, hint_line: int | None) -> tuple[int, int] | None:
    """Exact span from the AST: `end_lineno` cannot overrun into the next
    function (one-liners, trailing comments and odd signatures included), and
    `Class.method` resolves inside that class rather than to the first
    `def method` in the file."""
    import ast

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    parts = name.split(".")
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found: list[ast.AST] = []

    def walk(body, path: list[str]) -> None:
        for node in body:
            if isinstance(node, defs):
                here = path + [node.name]
                if here[-len(parts):] == parts:
                    found.append(node)
                walk(node.body, here)

    walk(tree.body, [])
    if not found:
        return None
    node = min(found, key=lambda n: abs(n.lineno - (hint_line or n.lineno)))
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return start, node.end_lineno or node.lineno


def locate(root: Path, rel: str, name: str, hint_line: int | None) -> tuple[int, int] | None:
    """1-based (start, end) of `name`'s definition in the CURRENT file,
    preferring the occurrence nearest the index's (possibly stale) line."""
    if not inside(root, rel):
        return None
    path = Path(root) / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if path.suffix in PY_EXT:
        span = _locate_python(text, name, hint_line)
        if span is not None:
            return span
    lines = text.splitlines()
    last = name.split(".")[-1]
    rx = _def_regex(last)
    starts = [i for i, ln in enumerate(lines) if rx.match(ln)]
    if not starts:
        return None
    best = min(starts, key=lambda i: abs(i + 1 - (hint_line or 1)))
    # Include decorators directly above.
    top = best
    while top > 0 and lines[top - 1].lstrip().startswith("@"):
        top -= 1
    end = _block_end(lines, best, path.suffix in PY_EXT)
    return top + 1, end + 1


def _enclosing(lines: list[str], idx: int, python: bool) -> str | None:
    """Name of the function/class containing 0-based line idx."""
    rx = re.compile(rf"^(\s*)(?:export\s+)?(?:async\s+)?{DEF_KEYWORDS}\s+([A-Za-z_$][\w$]*)")
    cur_indent = len(lines[idx]) - len(lines[idx].lstrip())
    names = []
    for j in range(idx, -1, -1):
        m = rx.match(lines[j])
        if not m:
            continue
        ind = len(m.group(1))
        if python and ind >= cur_indent and j != idx:
            continue
        names.append(m.group(2))
        cur_indent = ind
        if ind == 0:
            break
    return ".".join(reversed(names)) or None


def callers(root: Path, name: str, exclude: tuple[str, int, int] | None = None,
            limit: int = 40) -> dict[str, Any]:
    last = name.split(".")[-1]
    pattern = rf"(^|[^A-Za-z0-9_$]){re.escape(last)}[[:space:]]*\("
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "grep", "-n", "-E", "-I", pattern],
            capture_output=True, text=True, timeout=30, check=False,
            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", **NO_WINDOW,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return {"error": "git grep unavailable", "callers": []}
    rx_def = _def_regex(last)
    cache: dict[str, list[str]] = {}
    hits = []
    total = 0
    for line in out:
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        rel, lno, text = parts[0], parts[1], parts[2]
        try:
            n = int(lno)
        except ValueError:
            continue
        if rx_def.match(text):
            continue
        if exclude and rel == exclude[0] and exclude[1] <= n <= exclude[2]:
            continue  # recursion / the body itself
        total += 1
        if len(hits) >= limit:
            continue
        if rel not in cache:
            try:
                cache[rel] = (Path(root) / rel).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[rel] = []
        lines = cache[rel]
        where = _enclosing(lines, n - 1, rel.endswith(".py")) if 0 < n <= len(lines) else None
        hits.append({"at": f"{rel}:{n}", "in": where or "<module>",
                     "code": text.strip()[:160]})
    res: dict[str, Any] = {"total": total, "callers": hits}
    if total > limit:
        res["truncated"] = f"{total - limit} more call sites not shown"
    return res


def analyse_body(body: str, python: bool, name: str = "") -> dict[str, list[str]]:
    raises: list[str] = []
    rx = (re.compile(r"\braise\s+([A-Za-z_][\w.]*)") if python
          else re.compile(r"\bthrow\s+(?:new\s+)?([A-Za-z_$][\w.$]*)"))
    for m in rx.finditer(body):
        if m.group(1) not in raises:
            raises.append(m.group(1))
    calls: list[str] = []
    own = name.split(".")[-1]
    for m in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(", body):
        called = m.group(1)
        last = called.split(".")[-1]
        if last in KEYWORDS or called in calls or (last == own and m.start() < 200):
            continue  # skip keywords, repeats, and the definition line itself
        calls.append(called)
    return {"raises": raises, "calls": calls[:40]}
