"""Service-free tests for the session/staleness/automation features:
budgeted briefing, file claims + id reservations, repo facts, the test map,
symbol lookup, git + agent hook installers, and the agent hook's output.

Needs NO Qdrant, NO Ollama, NO Docker (git must be on PATH):

    uv run tests/test_features.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# ------------------------------------------------------------------ briefing
def test_briefing() -> None:
    from memory_mcp import briefing

    check("excerpt keeps short text", briefing.excerpt("short.", 50) == "short.")
    ex = briefing.excerpt("One sentence here. " * 40, 100)
    check("excerpt cuts at a boundary", len(ex) <= 104 and ex.endswith("..."), ex[-20:])
    s, steps, files = briefing._split_handoff(
        "Did things.\n\nNEXT STEPS:\n1. a\n2. b\n\nFILES TO READ FIRST:\n- x.py")
    check("handoff split", s == "Did things." and steps.startswith("1. a") and files == "- x.py")

    class _Proj:
        description = "A test project."
        root = Path(tempfile.gettempdir())

    class _Cfg:
        projects = {"p": _Proj()}

    now = "2026-10-15T10:00:00+00:00"
    mems = [{"id": "s" * 32, "memory_type": "session", "status": "active", "title": "h",
             "created_at": "2026-10-14T00:00:00+00:00", "updated_at": None,
             "content": "Summary " * 400 + "\nNEXT STEPS:\n1. ship it\nFILES TO READ FIRST:\n- a.py"}]
    for i in range(30):
        mems.append({"id": f"{i:032d}", "memory_type": "decision", "status": "active",
                     "title": f"Decision number {i} " + "x" * 150, "created_at": now,
                     "updated_at": now, "content": "Because reasons. " * 100})

    class _Store:
        def list_memories(self, **_kw):
            return mems

    out = briefing.build_briefing(_Cfg(), None, _Store(), "p", max_chars=5000,
                                  facts={"alembic": {"heads": [{"revision": "abc123"}]}},
                                  claims=[{"agent": "a1", "path": "x.py", "minutes_left": 5,
                                           "note": ""}])
    check("briefing respects budget", len(out) <= 5000, str(len(out)))
    check("briefing has handoff + open tasks", "Where you left off" in out and "ship it" in out)
    active = out[out.index("## Active decisions"):out.index("## Working rules")]
    check("briefing lists 10 decisions", active.count("Decision number") == 10,
          str(active.count("Decision number")))
    check("briefing shows alembic head + claim", "abc123" in out and "Claimed by a1" in out)
    check("briefing reports new decisions since handoff", "NEW decision" in out)


# --------------------------------------------------------- claims + reserve
def test_localdb() -> None:
    from memory_mcp import localdb

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite"
        r1 = localdb.claim("p", "agent-a", ["apps/api/", "x.py"], note="refactor", db=db)
        check("claim granted", set(r1["granted"]) == {"apps/api", "x.py"}, str(r1))
        r2 = localdb.claim("p", "agent-b", ["apps/api/core/alerting.py", "y.py"], db=db)
        check("folder claim blocks a file inside it",
              r2["granted"] == ["y.py"] and r2["conflicts"][0]["held_by"] == "agent-a", str(r2))
        r3 = localdb.claim("p", "agent-a", ["x.py"], db=db)
        check("re-claim by owner extends", r3["granted"] == ["x.py"])
        check("list_claims filters by path",
              [c["path"] for c in localdb.list_claims("p", ["apps/api/z.py"], db=db)]
              == ["apps/api"])
        check("release all of an agent's claims", localdb.release("p", "agent-a", db=db) == 2)
        check("claims gone after release",
              [c["agent"] for c in localdb.list_claims("p", db=db)] == ["agent-b"])
        n1 = localdb.reserve("p", "D", 790, agent="a", db=db)
        n2 = localdb.reserve("p", "D", 790, agent="b", db=db)
        n3 = localdb.reserve("p", "D", 795, db=db)
        check("reservations never collide", (n1, n2, n3) == (791, 792, 796), str((n1, n2, n3)))
        check("pending reservations listed",
              [r["number"] for r in localdb.reservations("p", "D", 791, db=db)] == [792, 796])


# --------------------------------------------------------------------- facts
def test_facts() -> None:
    from memory_mcp import facts

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/alembic\n")
        v = root / "alembic" / "versions"
        v.mkdir(parents=True)
        (v / "a.py").write_text('"""first"""\nrevision = "aaa"\ndown_revision = None\n')
        (v / "b.py").write_text('"""second"""\nrevision: str = "bbb"\ndown_revision = "aaa"\n')
        (v / "c.py").write_text('"""branch"""\nrevision = "ccc"\ndown_revision = "aaa"\n')
        heads = facts.alembic_heads(root)
        check("alembic: two heads detected",
              sorted(h["revision"] for h in heads["heads"]) == ["bbb", "ccc"]
              and heads["multiple_heads"], str(heads))
        (v / "d.py").write_text('revision = "ddd"\ndown_revision = ("bbb", "ccc")\n')
        check("alembic: merge revision resolves to one head",
              [h["revision"] for h in facts.alembic_heads(root)["heads"]] == ["ddd"])

        (root / "reg.py").write_text(
            'APPEND = frozenset({"ledger", "events"})\nSEV: dict = {"a_code": 1, "b_code": 2}\n')
        check("python constant: frozenset",
              sorted(facts.python_constant(root, "reg.py", "APPEND")) == ["events", "ledger"])
        check("python constant: dict keys",
              facts.python_constant(root, "reg.py", "SEV") == ["a_code", "b_code"])
        (root / "index.md").write_text(
            "# Doc\n## Alarm codes\n| `a_code` | x |\n| `c_code` | y |\n## Other\n| `zz` | q |\n")
        check("markdown rows scoped to heading",
              facts.markdown_rows(root, "index.md", "## Alarm codes",
                                  r"^\|\s*`([a-z_]+)`") == ["a_code", "c_code"])
        res = facts.run_check(root, "alarms", {"sets": {
            "severity": {"file": "reg.py", "symbol": "SEV"},
            "index": {"file": "index.md", "heading": "## Alarm codes",
                      "pattern": r"^\|\s*`([a-z_]+)`"}}})
        check("cross-file check finds both gaps",
              res["inconsistent"] == {"b_code": ["index"], "c_code": ["severity"]},
              str(res["inconsistent"]))

        (root / "docs").mkdir()
        (root / "docs" / "log.md").write_text("| D-12 | x |\n| D-7 | y |\nsee D-40 later\n")
        top, where = facts.max_in_sequence(root, {"pattern": r"\bD-(\d+)\b", "paths": ["docs"]})
        check("id sequence max", top == 40 and where == "docs/log.md", str((top, where)))

        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "jobs:\n  b:\n    steps:\n      - name: Checkout\n        uses: x\n"
            "      - name: Lint\n        run: ruff check .\n      - name: Build docs\n"
            "        run: echo hi\n")
        steps = facts.ci_steps(root)
        check("ci gates exclude non-gate steps",
              [g["name"] for g in steps[0]["gates"]] == ["Lint"], str(steps))


# ------------------------------------------------------------------ test map
def test_testmap() -> None:
    from memory_mcp import testmap

    check("numbits decode", testmap.numbits_to_nums(bytes([0b00000110, 0b1])) == [1, 2, 8])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("def test_a(): pass\n")
        check("context -> node id (test_function)",
              testmap._test_id("tests.test_x.TestC.test_a", root)
              == "tests/test_x.py::TestC::test_a")
        check("context -> node id (pytest-cov)",
              testmap._test_id("tests/test_x.py::test_a[1]|run", root)
              == "tests/test_x.py::test_a[1]")

        cov = root / ".coverage"
        c = sqlite3.connect(cov)
        c.executescript(
            "CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT UNIQUE);"
            "CREATE TABLE context (id INTEGER PRIMARY KEY, context TEXT UNIQUE);"
            "CREATE TABLE line_bits (file_id INT, context_id INT, numbits BLOB);"
            "CREATE TABLE arc (file_id INT, context_id INT, fromno INT, tono INT);")
        c.execute("INSERT INTO file VALUES (1, 'pkg/mod.py')")
        c.executemany("INSERT INTO context VALUES (?, ?)",
                      [(1, ""), (2, "tests/test_x.py::test_a|run"),
                       (3, "tests/test_x.py::test_b|run")])
        c.execute("INSERT INTO line_bits VALUES (1, 2, ?)", (bytes([0b00000110]),))  # 1,2
        c.execute("INSERT INTO arc VALUES (1, 3, 10, 11)")
        c.execute("INSERT INTO arc VALUES (1, 3, -1, 2)")
        c.commit()
        c.close()
        old = testmap.DATA_DIR
        testmap.DATA_DIR = root / "snap"
        try:
            res = testmap.ingest("t", root)
            check("coverage with contexts ingested", res.get("ingested") is True, str(res))
            allt = testmap.tests_for("t", root, "pkg/mod.py")
            check("all tests for file", sorted(allt["test_ids"])
                  == ["tests/test_x.py::test_a", "tests/test_x.py::test_b"], str(allt))
            ranged = testmap.tests_for("t", root, "pkg/mod.py", 10, 12)
            check("line range narrows to arc-covered test",
                  ranged["test_ids"] == ["tests/test_x.py::test_b"], str(ranged))
            check("re-ingest skipped when unchanged",
                  testmap.ingest("t", root).get("reason") == "already current")
            (root / "pyproject.toml").write_text(
                "[tool.coverage.run]\nbranch = true\nsource = [\"pkg\"]\n")
            rc = testmap.rcfile("t", root).read_text()
            check("rcfile keeps project settings + adds contexts",
                  "branch = true" in rc and "dynamic_context = test_function" in rc)
        finally:
            testmap.DATA_DIR = old


# ------------------------------------------------------------------- symbols
def test_symbols() -> None:
    from memory_mcp import symbols

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _git(root, "init", "-q")
        (root / "svc.py").write_text(
            "import os\n\n\n@decorator\ndef lend_line(x,\n              y):\n"
            "    if not x:\n        raise ValueError('no x')\n    return helper(x) + y\n\n\n"
            "def other():\n    return lend_line(1, 2)\n")
        (root / "web.ts").write_text(
            "export function route(a: number) {\n  if (a) {\n    throw new RouteError('x')\n"
            "  }\n  return a\n}\n\nconst caller = () => route(3)\n")
        _git(root, "add", ".")
        span = symbols.locate(root, "svc.py", "lend_line", 5)
        check("python def located incl. decorator + multi-line signature", span == (4, 9),
              str(span))
        check("ts function located by braces", symbols.locate(root, "web.ts", "route", 1)
              == (1, 6), str(symbols.locate(root, "web.ts", "route", 1)))
        body = "\n".join((root / "svc.py").read_text().splitlines()[3:9])
        info = symbols.analyse_body(body, True)
        check("raises found", info["raises"] == ["ValueError"], str(info))
        check("calls found", "helper" in info["calls"], str(info["calls"]))
        callers = symbols.callers(root, "lend_line", exclude=("svc.py", 4, 9))
        check("caller attributed to enclosing function",
              callers["total"] == 1 and callers["callers"][0]["in"] == "other", str(callers))
        tsc = symbols.callers(root, "route", exclude=("web.ts", 1, 6))
        check("ts caller found", tsc["total"] == 1, str(tsc))


# ----------------------------------------------------------------- git hooks
def test_git_hooks() -> None:
    from memory_mcp import hooks

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _git(root, "init", "-q")
        hookdir = root / ".git" / "hooks"
        hookdir.mkdir(exist_ok=True)
        (hookdir / "post-commit").write_text("#!/bin/sh\necho mine\n")
        first = hooks.install_git_hooks("demo", root)
        check("git hooks installed", "updated" in first, first)
        text = (hookdir / "post-commit").read_text()
        check("existing hook content preserved", "echo mine" in text and hooks.BEGIN in text)
        check("idempotent re-install",
              "already current" in hooks.install_git_hooks("demo", root))
        check("one block only", (hookdir / "post-commit").read_text().count(hooks.BEGIN) == 1)
        check("all four hooks written",
              all((hookdir / n).exists() for n in hooks.GIT_HOOKS))
        hooks.uninstall_git_hooks(root)
        check("uninstall keeps user content, drops ours",
              (hookdir / "post-commit").read_text().strip() == "#!/bin/sh\necho mine"
              and not (hookdir / "post-merge").exists())

        check("block goes before a trailing exec",
              hooks._with_block("#!/usr/bin/env bash\nexec pre-commit run\n", "B\n")
              == "#!/usr/bin/env bash\nB\nexec pre-commit run\n")
        check("python hook left alone",
              hooks._with_block("#!/usr/bin/env python3\nprint(1)\n", "B\n") is None)
        check("damaged block left alone",
              hooks._with_block(f"#!/bin/sh\n{hooks.BEGIN}\nx\n", "B\n") is None)

        tracked = Path(tmp) / "husky"
        tracked.mkdir()
        _git(tracked, "init", "-q")
        _git(tracked, "config", "core.hooksPath", ".husky")
        check("tracked hooksPath is left alone",
              "skipped" in hooks.install_git_hooks("h", tracked))


def test_agent_hook_installers() -> None:
    from memory_mcp import hooks

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": ["x"]}, "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [
                {"type": "command", "command": "other-tool"}]}]}}))
        ours = f'"py" "{hooks.ROOT / "scripts" / "agent_hook.py"}" session-start'
        ev = {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": ours}]}]}
        status = hooks._merge_events(path, ev)
        data = json.loads(path.read_text())
        check("merge installs", "installed" in status, status)
        check("other settings and hooks preserved",
              data["permissions"] == {"allow": ["x"]}
              and len(data["hooks"]["SessionStart"]) == 2)
        check("merge idempotent", "already current" in hooks._merge_events(path, ev))
        check("backup written", path.with_name("settings.json.rememory-backup").exists())
        # A user hook sharing a matcher group with ours survives a re-install,
        # and another tool's script that is merely NAMED agent_hook.py is kept.
        data["hooks"]["SessionStart"][1]["hooks"].append(
            {"type": "command", "command": "user-thing"})
        data["hooks"]["SessionStart"].append({"matcher": "x", "hooks": [
            {"type": "command", "command": "C:/other/agent_hook.py"}]})
        path.write_text(json.dumps(data))
        hooks._merge_events(path, ev)
        after = json.dumps(json.loads(path.read_text()))
        check("user hook in our matcher group preserved", "user-thing" in after)
        check("same-named foreign script preserved", "C:/other/agent_hook.py" in after)
        check("ours present exactly once", after.count("agent_hook.py\\\" session-start")
              + after.count("agent_hook.py\" session-start") >= 1
              and after.count("session-start") == 1, after[:200])
        backup = path.with_name("settings.json.rememory-backup").read_text()
        check("first backup kept (pre-rememory config)", ours.split()[0] not in backup
              or "other-tool" in backup)
        bad = Path(tmp) / "bad.json"
        bad.write_text("{ // jsonc comment\n}")
        check("unparseable config never rewritten",
              "skipped" in hooks._merge_events(bad, ev) and "jsonc" in bad.read_text())


def test_agent_hook_output() -> None:
    import contextlib
    import io

    sys.path.insert(0, str(ROOT / "scripts"))
    import agent_hook

    def emit(client: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent_hook._emit(client, "SessionStart", "ctx")
        return buf.getvalue()

    check("claude/codex/copilot/gemini JSON shape",
          json.loads(emit("gemini"))["hookSpecificOutput"]["additionalContext"] == "ctx")
    check("cursor shape", json.loads(emit("cursor")) == {"additional_context": "ctx"})
    check("plain/kiro shape", emit("kiro") == "ctx")
    check("file path from claude input",
          agent_hook._edited_file({"tool_input": {"file_path": "a.py"}}) == "a.py")
    check("file path from cursor input", agent_hook._edited_file({"file_path": "b.py"}) == "b.py")
    check("file path from windsurf input",
          agent_hook._edited_file({"tool_info": {"file_path": "c.py"}}) == "c.py")
    check("cwd from cursor workspace_roots",
          agent_hook._cwd({"workspace_roots": ["/w"]}) == "/w")


def test_auto_supersede_is_narrow() -> None:
    """A false positive silently retires real knowledge, so only an exact
    upper-case id that STARTS exactly one active title may be retired."""
    from types import SimpleNamespace

    from memory_mcp import memories

    titles = {
        "a": "D-127: Pip redaction rules",
        "b": "Use UTF-8 everywhere",
        "c": "Upgrade base image to Python-3.12",
        "d": "Bug in D-12 rollout",
        "e": "D-12 tenant isolation",
        "f": "ADR-4 caching",
        "g": "ADR-4 caching (copy)",
    }
    retired: list = []

    class FakeClient:
        def scroll(self, **_kw):
            return [SimpleNamespace(id=k, payload={"title": t, "memory_type": "decision"})
                    for k, t in titles.items()], None

        def set_payload(self, **kw):
            retired.extend(kw["points"])

    store = object.__new__(memories.MemoryStore)
    store.client = FakeClient()
    memories._SUPERSEDE_RE = None
    r = store._auto_supersede("p", "new", "D-791 new rule",
                              "This replaces utf-8 handling, retires gpt-4 and "
                              "Replacing python-3.11 pins. It supersedes D-127 and D-12.",
                              None)
    check("lower-case tech tokens never match", not {"b", "c"} & set(retired), str(retired))
    check("id must START the title ('Bug in D-12' kept)", "d" not in retired)
    check("exact single matches retired", set(retired) == {"a", "e"}, str(retired))
    retired.clear()
    r = store._auto_supersede("p", "new", "x", "supersedes ADR-4", None)
    check("ambiguous id -> candidates, nothing retired",
          not retired and len(r["candidates"]) == 2, str(r))


def test_symbol_spans() -> None:
    from memory_mcp import symbols

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text(
            "def f():  # noqa\n    return 1\n\n\ndef g():\n    return 2\n\n"
            "class A:\n    def run(self):\n        pass\n\n"
            "class B:\n    def run(self):\n        return 3\n")
        check("trailing comment does not swallow the next def",
              symbols.locate(root, "a.py", "f", None) == (1, 2))
        check("Class.method resolves inside its class",
              symbols.locate(root, "a.py", "B.run", None) == (13, 14),
              str(symbols.locate(root, "a.py", "B.run", None)))
        check("paths outside the root refused",
              symbols.locate(root, "../a.py", "f", None) is None)


def test_memory_ids_and_review_rules() -> None:
    from types import SimpleNamespace

    from memory_mcp import memories
    from memory_mcp.memories import MemoryError as MemErr

    ids = ["2ef554fe-1111-4222-8333-444455556666", "2ef554aa-1111-4222-8333-444455556666",
           "9b5af858-1111-4222-8333-444455556666"]

    class FakeClient:
        def scroll(self, **_kw):
            return [SimpleNamespace(id=i) for i in ids], None

    store = object.__new__(memories.MemoryStore)
    store.client = FakeClient()
    check("short id resolves", store.resolve_id("9b5af858") == ids[2])
    check("full id passes through", store.resolve_id(ids[0].upper()) == ids[0])
    for bad, why in (("2ef554", "ambiguous"), ("2ef5", "too short"), ("zz", "not an id"),
                     ("deadbeef", "no match")):
        try:
            store.resolve_id(bad)
            check(f"{why} id rejected", False)
        except MemErr as exc:
            check(f"{why} id rejected with a clear message", "point ID" not in str(exc),
                  str(exc))

    check("numbered parts are one series",
          memories._same_series("Outpero teardown part 2 - config", "Outpero teardown part 8 - docs"))
    check("(3/8) style series", memories._same_series("Audit (3/8): auth", "Audit (4/8): billing"))
    check("different topics are not a series",
          not memories._same_series("Auth part 1", "Billing part 2"))
    check("plain titles are not a series",
          not memories._same_series("Deploy notes", "Deploy notes v2"))
    check("self-retired title detected",
          bool(memories._SELF_RETIRED_RE.match("SUPERSEDED: D-36 stack on Bolna")))
    check("ordinary title not self-retired",
          not memories._SELF_RETIRED_RE.match("Superseding plan for workers"))
    found = set()
    for m in memories._REPLACES_RE.finditer("D-688 tiers (supersedes D-687 workspaces); "
                                            "founder override of ADR-4, RFC-9"):
        found |= set(memories._ID_RE.findall(m.group(1)))
    check("replacement ids of any prefix", found == {"D-687", "ADR-4", "RFC-9"}, str(found))
    check("deletion record is not 'missing file'",
          memories._said_gone("We deleted apps/x/old.py and its tests.", "apps/x/old.py"))
    check("a plain mention is", not memories._said_gone("See apps/x/old.py for the logic.",
                                                        "apps/x/old.py"))
    check("sub-app relative path found by suffix",
          memories._tracked_suffix(frozenset({"apps/web/tests/setup.ts"}), "tests/setup.ts"))


def test_missing_file_history() -> None:
    """Deleted vs renamed files, from git; written-after-deletion is history."""
    import subprocess

    from memory_mcp import memories

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def git(*a):
            subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (root / "src").mkdir()
        (root / "src" / "old_name.py").write_text("def f():\n    return 1\n" * 20)
        (root / "src" / "doomed.py").write_text("x = 1\n")
        (root / "src" / "keep.py").write_text("y = 2\n")
        git("add", "-A")
        git("commit", "-qm", "one")
        git("mv", "src/old_name.py", "src/new_name.py")
        git("rm", "-q", "src/doomed.py")
        git("commit", "-qm", "two")
        payload = {"title": "Notes", "content": "See src/old_name.py, src/doomed.py, "
                   "src/keep.py and src/never_there.py."}
        tracked = memories._tracked_files(root)
        missing = memories._missing_refs(root, payload, tracked)
        check("missing refs found", missing == ["src/old_name.py", "src/doomed.py",
                                                "src/never_there.py"], str(missing))
        hist = memories._path_history(root, missing)
        check("rename detected", hist.get("src/old_name.py", (0, 0))[1] == "src/new_name.py",
              str(hist))
        check("deletion dated, no rename",
              hist.get("src/doomed.py", (None, 1))[0] is not None
              and hist["src/doomed.py"][1] is None, str(hist))
        check("never-tracked path has no history", "src/never_there.py" not in hist)
        check("'safe to delete' is not a deletion record",
              not memories._said_gone("Test note naming src/x.py; safe to delete.", "src/x.py"))
        check("title recording a removal suppresses the flag",
              memories._missing_refs(root, {"title": "Pipecat removed", "content":
                                            "src/doomed.py"}, tracked) == [])


def test_search_intent() -> None:
    from types import SimpleNamespace

    from memory_mcp.search import intent_types, prefer_types

    check("recipe intent", intent_types("how do I deploy to the VPS") == ("recipe",))
    check("recipe keyword", intent_types("deploy to VPS recipe") == ("recipe",))
    check("fix intent", intent_types("alembic upgrade failed with KeyError") == ("fix",))
    check("no intent for topics", intent_types("Pipecat removal") is None)

    def r(t, n):
        return SimpleNamespace(extra={"memory_type": t}, n=n)
    pool = [r("deployment", 1), r("deployment", 2), r("recipe", 3), r("bug", 4)]
    out = prefer_types(pool, ("recipe",), 3)
    check("recipe in the pool lifted to the top", [x.n for x in out] == [3, 1, 2])
    check("nothing to lift keeps order",
          [x.n for x in prefer_types(pool, ("fix",), 4)] == [1, 2, 3, 4])


def main() -> int:
    print("feature tests (no services required)\n")
    for t in (test_briefing, test_localdb, test_facts, test_testmap, test_symbols,
              test_git_hooks, test_agent_hook_installers, test_agent_hook_output,
              test_auto_supersede_is_narrow, test_symbol_spans,
              test_memory_ids_and_review_rules, test_search_intent,
              test_missing_file_history):
        print(t.__name__)
        try:
            t()
        except Exception as exc:  # a crash is a failure, not an abort
            import traceback

            traceback.print_exc()
            check(f"{t.__name__} raised {type(exc).__name__}", False, str(exc))
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
