"""Zero-config repo detection (memory_mcp.detect) against tiny fake repos.

No services, no network, no real repos. Runs under pytest (tmp_path) or
standalone:

    uv run tests/test_detect.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_mcp import detect  # noqa: E402
from memory_mcp.facts import max_in_sequence  # noqa: E402


def w(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def by_tool(root: Path) -> dict:
    return {m["tool"]: m for m in detect.migrations(root)}


# --------------------------------------------------------------- migrations
DJ = "from django.db import migrations\n\nclass Migration(migrations.Migration):\n"


def test_django_leaves_conflict_and_squash(tmp_path: Path) -> None:
    w(tmp_path, "shop/migrations/__init__.py")
    w(tmp_path, "shop/migrations/0001_initial.py", DJ + "    dependencies = []\n")
    w(tmp_path, "shop/migrations/0002_a.py",
      DJ + "    dependencies = [('shop', '0001_initial'), "
           "migrations.swappable_dependency('auth.User')]\n")
    w(tmp_path, "shop/migrations/0002_b.py", DJ + "    dependencies = [('shop', '0001_initial')]\n")
    w(tmp_path, "blog/migrations/__init__.py")
    w(tmp_path, "blog/migrations/0001_initial.py", DJ + "    dependencies = []\n")
    w(tmp_path, "blog/migrations/0002_x.py", DJ + "    dependencies = [('blog', '0001_initial')]\n")
    w(tmp_path, "blog/migrations/0001_squashed_0002_x.py",
      DJ + "    replaces = [('blog', '0001_initial'), ('blog', '0002_x')]\n"
           "    dependencies = []\n")
    dj = by_tool(tmp_path)["django"]
    assert dj["apps"]["shop"]["leaves"] == ["0002_a", "0002_b"]
    assert dj["apps"]["shop"]["next_number"] == "0003"
    assert any("conflict" in x for x in dj["warnings"])
    assert dj["apps"]["blog"]["leaves"] == ["0001_squashed_0002_x"]
    assert dj["apps"]["blog"]["squashed"] == ["0001_squashed_0002_x"]


def test_prisma(tmp_path: Path) -> None:
    w(tmp_path, "prisma/migrations/migration_lock.toml", 'provider = "postgresql"\n')
    w(tmp_path, "prisma/migrations/20240101000000_init/migration.sql", "x")
    w(tmp_path, "prisma/migrations/20240202000000_more/migration.sql", "x")
    p = by_tool(tmp_path)["prisma"]
    assert p["latest"] == "20240202000000_more" and p["count"] == 2
    assert p["provider"] == "postgresql"


def test_rails_schema_behind(tmp_path: Path) -> None:
    w(tmp_path, "db/migrate/20240101000000_a.rb")
    w(tmp_path, "db/migrate/20240301000000_b.rb")
    w(tmp_path, "db/schema.rb", "ActiveRecord::Schema[7.1].define(version: 2024_01_01_000000) do")
    r = by_tool(tmp_path)["rails"]
    assert r["latest_version"] == "20240301000000"
    assert r["schema_version"] == "20240101000000" and r["warnings"]


def test_drizzle_custom_out(tmp_path: Path) -> None:
    w(tmp_path, "drizzle.config.ts", "export default { out: './db/out', dialect: 'pg' }")
    w(tmp_path, "db/out/meta/_journal.json",
      '{"entries": [{"idx": 0, "tag": "0000_a"}, {"idx": 1, "tag": "0001_b"}]}')
    d = by_tool(tmp_path)["drizzle"]
    assert d["latest"] == "0001_b" and d["count"] == 2


def test_laravel_and_efcore(tmp_path: Path) -> None:
    w(tmp_path, "database/migrations/2024_01_01_000000_create_users_table.php")
    w(tmp_path, "database/migrations/2024_02_01_000000_add_x.php")
    w(tmp_path, "src/Data/Migrations/20240101000000_Init.cs")
    w(tmp_path, "src/Data/Migrations/20240101000000_Init.Designer.cs",
      "[DbContext(typeof(AppDbContext))]")
    w(tmp_path, "src/Data/Migrations/20240201000000_Two.cs")
    w(tmp_path, "src/Data/Migrations/20240201000000_Two.Designer.cs",
      "[DbContext(typeof(AppDbContext))]")
    t = by_tool(tmp_path)
    assert t["laravel"]["latest"] == "2024_02_01_000000_add_x"
    assert t["efcore"]["contexts"]["AppDbContext"]["latest"] == "20240201000000_Two"
    assert t["efcore"]["count"] == 2


def test_flyway_tuple_order(tmp_path: Path) -> None:
    for n in ("V1__a.sql", "V2__b.sql", "V10__c.sql", "V2_1__d.sql", "R__view.sql", "U3__x.sql"):
        w(tmp_path, f"src/main/resources/db/migration/{n}")
    f = by_tool(tmp_path)["flyway"]
    assert f["latest_version"] == "10" and f["next_version"] == "11" and f["count"] == 4


def test_generic_sequential_and_timestamp(tmp_path: Path) -> None:
    w(tmp_path, "migrations/000001_init.up.sql")
    w(tmp_path, "migrations/000001_init.down.sql")
    w(tmp_path, "migrations/000002_users.up.sql")
    w(tmp_path, "migrations/000002_users.down.sql")
    w(tmp_path, "db/migrations/20240101120000_a.sql", "-- migrate:up\n")
    t = by_tool(tmp_path)
    gm = t["golang-migrate"]
    assert gm["style"] == "sequential" and gm["next"] == "000003" and gm["width"] == 6
    db = t["dbmate"]
    assert db["style"] == "timestamp" and len(db["next"]) == 14


def test_knex_detected(tmp_path: Path) -> None:
    w(tmp_path, "knexfile.js", "module.exports = {}")
    w(tmp_path, "migrations/20240101000000_init.js")
    assert "knex" in by_tool(tmp_path)


# -------------------------------------------------------------------- gates
def gates(root: Path, system: str) -> list[dict]:
    return [g for g in detect.task_gates(root) if g["system"] == system]


def test_gitlab(tmp_path: Path) -> None:
    w(tmp_path, ".gitlab-ci.yml",
      "stages: [test]\ninclude:\n  - local: ci/extra.yml\nvariables: {A: 1}\n"
      ".hidden: {script: [x]}\n"
      "lint:\n  stage: test\n  script: [ruff check .]\n"
      "flaky:\n  script:\n    - !reference [.hidden, script]\n    - pytest\n"
      "  allow_failure: true\n")
    w(tmp_path, "ci/extra.yml", "e2e:\n  script: npm run e2e\n")
    g = {x["name"]: x for x in gates(tmp_path, "gitlab-ci")}
    assert set(g) == {"lint", "flaky", "e2e"}
    assert g["lint"]["gating"] and not g["flaky"]["gating"]
    assert g["e2e"]["source"] == "ci/extra.yml"


def test_circleci_azure_bitbucket(tmp_path: Path) -> None:
    w(tmp_path, ".circleci/config.yml",
      "jobs:\n  build:\n    steps:\n      - checkout\n      - run: make test\n"
      "      - run: {name: Lint, command: make lint}\n"
      "workflows:\n  main:\n    jobs: [build]\n")
    w(tmp_path, "azure-pipelines.yml",
      "stages:\n- stage: CI\n  jobs:\n  - job: t\n    steps:\n"
      "    - script: pytest\n      displayName: Tests\n")
    w(tmp_path, "bitbucket-pipelines.yml",
      "pipelines:\n  default:\n    - step: {name: Test, script: [npm test]}\n"
      "    - parallel:\n        - step: {name: Lint, script: [npm run lint]}\n")
    assert [g["command"] for g in gates(tmp_path, "circleci")] == ["make test", "make lint"]
    assert gates(tmp_path, "azure-pipelines")[0]["command"] == "pytest"
    assert {g["name"] for g in gates(tmp_path, "bitbucket")} == {"default: Test",
                                                                  "default: Lint"}


def test_hooks(tmp_path: Path) -> None:
    w(tmp_path, ".pre-commit-config.yaml",
      "repos:\n- repo: https://github.com/astral-sh/ruff-pre-commit\n  rev: v0.6.0\n"
      "  hooks: [{id: ruff}, {id: ruff-format}]\n")
    w(tmp_path, ".husky/pre-commit", "#!/bin/sh\nnpx lint-staged\n")
    w(tmp_path, "lefthook.yml", "pre-push:\n  commands:\n    test: {run: npm test}\n")
    pc = gates(tmp_path, "pre-commit")
    assert [g["name"] for g in pc] == ["ruff", "ruff-format"]
    assert pc[0]["repo"] == "ruff-pre-commit@v0.6.0"
    assert gates(tmp_path, "husky")[0]["command"] == "npx lint-staged"
    assert gates(tmp_path, "lefthook")[0]["command"] == "npm test"


def test_makefile_just_taskfile(tmp_path: Path) -> None:
    w(tmp_path, "Makefile",
      ".PHONY: test lint\nVAR := 1\nX ?= 2\n%.o: %.c\n\tcc\n"
      "test: deps\n\t@uv run pytest\nlint check:\n\truff .\n")
    w(tmp_path, "justfile", "set shell := ['bash']\nalias t := test\n"
      "test *args:\n    pytest {{args}}\n@fmt:\n    ruff format\n")
    w(tmp_path, "Taskfile.yml", "version: '3'\ntasks:\n  build:\n    cmds: [go build]\n")
    mk = {g["name"]: g for g in gates(tmp_path, "make")}
    assert set(mk) == {"test", "lint", "check"}
    assert mk["test"]["recipe"] == "uv run pytest"
    assert [g["name"] for g in gates(tmp_path, "just")] == ["test", "fmt"]
    assert gates(tmp_path, "task")[0]["recipe"] == "go build"


def test_package_json_scripts_and_workspaces(tmp_path: Path) -> None:
    w(tmp_path, "pnpm-lock.yaml")
    w(tmp_path, "pnpm-workspace.yaml", "packages: ['apps/*']\n")
    w(tmp_path, "package.json",
      '{"scripts": {"test": "vitest", "dev": "vite", "lint:fix": "eslint --fix"}}')
    w(tmp_path, "apps/web/package.json", '{"name": "web", "scripts": {"typecheck": "tsc"}}')
    pj = {(g["source"], g["name"]): g for g in gates(tmp_path, "package.json")}
    assert ("package.json", "dev") not in pj
    assert pj[("package.json", "test")]["command"] == "pnpm run test"
    assert pj[("apps/web/package.json", "typecheck")]["command"] == \
        "pnpm --filter web run typecheck"


def test_python_runners(tmp_path: Path) -> None:
    w(tmp_path, "uv.lock")
    w(tmp_path, "tox.ini", "[tox]\nenvlist = py312, lint\n[testenv:docs]\n")
    w(tmp_path, "noxfile.py", "import nox\n@nox.session\ndef tests(s): ...\n"
      "@nox.session(name='lint-all')\ndef lint(s): ...\ndef helper(): ...\n")
    w(tmp_path, "pyproject.toml",
      "[tool.poe.tasks]\ntest = 'pytest'\n[tool.hatch.envs.default.scripts]\ncov = 'x'\n"
      "[tool.hatch.envs.lint.scripts]\nstyle = 'ruff'\n[tool.pdm.scripts]\n_ = {}\n"
      "check = 'mypy'\n[tool.taskipy.tasks]\nfmt = 'black .'\n")
    assert [g["name"] for g in gates(tmp_path, "tox")] == ["py312", "lint", "docs"]
    assert [g["command"] for g in gates(tmp_path, "nox")] == ["nox -s tests", "nox -s lint-all"]
    assert gates(tmp_path, "poe")[0]["command"] == "uv run poe test"
    assert {g["command"] for g in gates(tmp_path, "hatch")} == {"hatch run cov",
                                                                 "hatch run lint:style"}
    assert [g["name"] for g in gates(tmp_path, "pdm")] == ["check"]
    assert gates(tmp_path, "taskipy")[0]["command"] == "uv run task fmt"


# ---------------------------------------------------------------- toolchain
def test_toolchain(tmp_path: Path) -> None:
    w(tmp_path, "package-lock.json", "{}")
    w(tmp_path, "package.json",
      '{"packageManager": "pnpm@9.0.0", "engines": {"node": ">=20"}, '
      '"workspaces": ["packages/*"], "scripts": {"lint": "eslint ."}}')
    w(tmp_path, "packages/a/package.json", "{}")
    w(tmp_path, "uv.lock")
    w(tmp_path, ".python-version", "3.12\n")
    w(tmp_path, "pyproject.toml",
      "[project]\nrequires-python = '>=3.11'\n[tool.ruff]\n[tool.pytest.ini_options]\n"
      "[tool.uv.workspace]\nmembers = ['libs/*']\n")
    w(tmp_path, "libs/core/pyproject.toml")
    w(tmp_path, "Cargo.toml", "[package]\nname='x'\n")
    w(tmp_path, ".git/refs/remotes/origin/HEAD", "ref: refs/remotes/origin/trunk\n")
    t = detect.toolchain(tmp_path)
    js = t["package_managers"]["js"]
    assert js["name"] == "npm" and any("packageManager" in x for x in js["warnings"])
    assert t["package_managers"]["python"]["name"] == "uv"
    assert t["package_managers"]["other"][0]["name"] == "cargo"
    assert t["versions"]["python"]["version"] == "3.12"
    assert t["versions"]["node"]["version"] == ">=20"
    assert t["workspaces"]["js"]["members"] == ["packages/a"]
    assert t["workspaces"]["python"]["members"] == ["libs/core"]
    assert t["default_branch"]["name"] == "trunk"
    assert t["commands"]["test"][0]["command"] == "uv run pytest"
    assert t["commands"]["lint"][0] == {"command": "npm run lint", "source": "package.json",
                                        "how": "script"}


def test_default_branch_worktree_file(tmp_path: Path) -> None:
    main = tmp_path / "main"
    w(main, ".git/refs/remotes/origin/HEAD", "ref: refs/remotes/origin/develop\n")
    w(main, ".git/worktrees/wt/commondir", "../..\n")
    wt = tmp_path / "wt"
    w(wt, ".git", f"gitdir: {(main / '.git' / 'worktrees' / 'wt').as_posix()}\n")
    assert detect.toolchain(wt)["default_branch"]["name"] == "develop"
    (tmp_path / "bare").mkdir()
    assert detect.toolchain(tmp_path / "bare")["default_branch"]["name"] == "main"


# ------------------------------------------------------------- id sequences
def test_adr_dir(tmp_path: Path) -> None:
    for n in ("0001-use-x.md", "0002-use-y.md", "0013-z.md", "template.md", "README.md",
              "20240101-dated.md"):
        w(tmp_path, f"docs/adr/{n}", "# x")
    s = detect.id_sequences(tmp_path)["ADR"]
    assert s["kind"] == "adr" and s["max"] == 13 and s["next"] == "0014"
    assert detect.max_in_filenames(tmp_path, s) == (13, "docs/adr/0013-z.md")


def test_inline_ids(tmp_path: Path) -> None:
    w(tmp_path, "docs/decisions.md",
      "| ID | Decision |\n|---|---|\n| **D-1** | a |\n| D-2 | b |\n| D-7 | c |\n"
      "See CVE-2021-44228 and ISO-8601.\n")
    w(tmp_path, "docs/more.md", "## D-9 something\n- REQ-001 x\n")
    w(tmp_path, "README.md", "mentions D-50 in prose only\n| CVE-1 |\n| CVE-2 |\n| CVE-3 |\n")
    seqs = detect.id_sequences(tmp_path)
    assert "CVE" not in seqs and "REQ" not in seqs
    d = seqs["D"]
    assert d["max"] == 9 and d["next"] == "D-10" and d["format"] == "D-{n}"
    assert set(d["paths"]) == {"docs/decisions.md", "docs/more.md"}
    assert max_in_sequence(tmp_path, d) == (9, "docs/more.md")


def test_inline_padded_format(tmp_path: Path) -> None:
    w(tmp_path, "spec.md", "- REQ-001 a\n- REQ-002 b\n- REQ-010 c\n")
    s = detect.id_sequences(tmp_path)["REQ"]
    assert s["format"] == "REQ-{n:03d}" and s["next"] == "REQ-011"


def test_all_never_raises(tmp_path: Path) -> None:
    w(tmp_path, ".gitlab-ci.yml", "::: not yaml [")
    w(tmp_path, "package.json", "{broken")
    w(tmp_path, "pyproject.toml", "= nope")
    out = detect.all(tmp_path)
    assert {"migrations", "task_gates", "toolchain", "id_sequences", "elapsed_ms"} <= set(out)
    assert out["warnings"] == []


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d))
                    print(f"  [PASS] {name}")
                except Exception as exc:
                    failed += 1
                    print(f"  [FAIL] {name} -- {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
