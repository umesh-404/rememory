# rememory

**Local, private, zero-cloud development memory for AI coding assistants.**

rememory gives your AI assistant long-term memory across sessions and
projects: it indexes your source code and documentation, stores the knowledge
the assistant produces while working (architecture decisions, bug root causes,
session handoffs), and serves all of it back through fast, precise search.

It speaks standard **MCP over stdio**, so it works with **any MCP-capable
client** — Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, or anything
else. Everything runs on your machine: no cloud, no API keys, no telemetry,
nothing leaves your computer.

```
your MCP client ── stdio ── rememory ──┬── Qdrant  (vector DB, Docker, 127.0.0.1 only)
(Claude Code, Cursor, ...)             └── Ollama  (embedding + reranker models, local)

your repos ── indexer ── tree-sitter symbol chunks ── hybrid search ── reranked results
```

---

## Why

AI coding assistants forget everything between sessions. Every new session
re-reads the same files, re-derives the same architecture, and occasionally
re-makes a decision you already rejected months ago. rememory fixes that:

- **"Have we decided this before?"** → one search across every project.
- **"Continue where we left off yesterday"** → the assistant saves a handoff
  at the end of a session and resumes from it at the start of the next,
  reading only the files that matter instead of the whole repo.
- **"How does X work in this codebase?"** → answered with `file:line`
  citations from an index that stays current automatically.

---

## What your assistant gets

**14 tools + 1 prompt**, all annotated per the MCP spec (read-only tools
declare `readOnlyHint`; the single destructive tool declares
`destructiveHint`):

| Tool | What it does |
|---|---|
| `search_code` | Hybrid (semantic + exact-identifier) search over source code, cross-encoder reranked, `file:line` citations, per-result relevance score |
| `search_docs` | Same, over documentation — results carry the heading trail (`Guide > Setup > Docker`) |
| `search_memory` | Knowledge from past sessions, cross-project by default, recency-aware |
| `store_memory` | Save a decision / bug root cause / implementation note (validated; near-duplicates rejected with the existing memory's id) |
| `update_memory` | Revise a memory — the old version is kept as history, never overwritten |
| `delete_memory` | Remove exactly one memory by id (the only destructive tool; no bulk delete exists) |
| `get_memory` / `list_memories` | Fetch full content by id / browse newest-first with pagination |
| `get_briefing` | One-call project context block: **where you left off**, all active decisions in full, recent memories, staleness flags |
| `save_session` | Session handoff: summary + next steps + files to read first; each save supersedes the previous |
| `find_project` | Directory → knowledge base: at session start the assistant resolves the working directory to its registered project and links up automatically — no re-registering |
| `register_project` | Create or repair a project's knowledge base from a prompt ("create a knowledge base for this project") — validates, registers, and indexes in one call |
| `sync_index` | Re-index a project on demand so freshly written files are searchable immediately |
| `memory_system_status` | Inventory + last-indexed times — distinguishes "nothing indexed" from "nothing matched" |
| `/kickoff` (prompt) | In Claude Code: `/mcp__rememory__kickoff <project>` loads the briefing and resumes work |

## Retrieval quality

The pipeline follows current production-RAG practice, fully locally:
**tree-sitter symbol-aware chunking** (a chunk is a whole function, class, or
doc section — never a severed fragment) → contextual breadcrumb headers →
**hybrid dense + sparse retrieval** fused with RRF → **local cross-encoder
reranking** → per-file diversity. On the built-in golden-set eval, reranking
lifts Recall@1 from 50% to 92% and Recall@3 to 100% — and
`uv run tests/eval_retrieval.py` measures it on *your* machine, not ours.

Secrets are **redacted at ingestion**: known token formats (AWS, GitHub,
OpenAI, Slack, …), private-key blocks and secret-named assignments are
replaced with `[REDACTED]` before anything is embedded or stored, and
credential files (`.env*`, `id_rsa`, `*.pem`, …) are never indexed at all.
See [SECURITY.md](SECURITY.md) for the full model.

---

## Installation

### 1. Prerequisites (one-time, normal installers)

| | Why rememory needs it |
|---|---|
| [Docker](https://docs.docker.com/get-docker/) | Runs Qdrant, the vector database (bound to 127.0.0.1 only) |
| [Ollama](https://ollama.com/download) | Runs the two small local AI models (embedding + reranker) |
| git | To clone this repo |

Make sure Docker and Ollama are **running** before setup.

### 2. Clone and run the master setup

```bash
git clone https://github.com/umesh-404/rememory
cd rememory
```

**Windows:**

```bash
powershell -ExecutionPolicy Bypass -File setup.ps1
```

**macOS / Linux:**

```bash
./setup.sh
```

Setup shows progress for every step (`[1/9] … [9/9]`) so you always know
what's happening. What it does:

1. **Checks prerequisites** — fails immediately with the fix if Docker or
   Ollama isn't running.
2. **Installs `uv`** (the Python toolchain manager) if missing — it manages a
   private, pinned Python 3.12 for rememory; your system Python is untouched.
3. **Pulls the two models** into Ollama (~1.9 GB, first run only; Ollama
   shows its own download progress).
4. **Starts Qdrant** in Docker, with all data stored inside this repo's
   `data/` folder — loopback-only, unreachable from your network.
5. **Builds the Python environment** from the lockfile (reproducible).
6. **Creates the vector collections** (`code`, `docs`, `memory`).
7. **Creates your project registry** (`config/projects.yaml`) from the
   example.
8. **Indexes rememory itself** (proving the whole pipeline), seeds example
   memories, and registers background automation (Windows: scheduled tasks
   for 30-minute incremental sync and daily backup; Unix: a suggested cron
   line is printed).
9. **Runs the verification test suite.**

Setup is **idempotent** — if anything fails, fix the cause and re-run;
completed steps become fast no-ops.

### 3. The one manual step: connect your client

At the end, setup prints (and saves to `mcp-config.json`) the exact
connection config **with your machine's real paths filled in** — nothing to
hand-edit. Re-print it anytime with:

```bash
uv run scripts/connect.py
```

Pick your client:

- **Claude Code** — one CLI command (printed for you):
  `claude mcp add --scope user rememory -- <uv> run --directory <repo> -m memory_mcp.server`
- **Claude Desktop** — paste the JSON into `claude_desktop_config.json`
  (Settings → Developer → Edit Config), then fully quit and reopen the app.
  Note: Claude Code and Claude Desktop keep **separate** MCP registries —
  registering one does not register the other.
- **Cursor** — paste into `~/.cursor/mcp.json` (or a project's
  `.cursor/mcp.json`).
- **Windsurf** — paste into `~/.codeium/windsurf/mcp_config.json`.
- **VS Code (Copilot agent mode)** — add to `.vscode/mcp.json` with
  `"type": "stdio"`.
- **Anything else** — point it at the printed command; stdio transport, no
  env vars, no keys.

Multiple clients can be connected at once — each spawns its own server
process, all share the same local data, and writes are serialized by an
index lock.

### 4. Register your projects — just ask your assistant

The easiest way: open your project in your connected client and say

> **"Create a knowledge base for this project in rememory."**

The assistant calls the `register_project` tool, which validates the folder,
writes the registry entry, and runs the first index — the project is
searchable seconds later, no restart needed. In every later session, the
assistant resolves your working directory to the existing knowledge base
automatically (`find_project`) — you register once, ever; a directory can
never be registered twice under different names. The same one prompt is also the
**repair path**: if a registration ever breaks (folder moved, registry
mangled), asking again re-registers and re-indexes it. Your assistant will
also *offer* to create a knowledge base when it notices the project you're
working on isn't registered yet.

Prefer doing it by hand? The equivalent is:

```yaml
# config/projects.yaml  (yours; gitignored)
projects:
  my-app:
    root: C:\code\my-app        # or /home/you/code/my-app
    description: >-
      One or two sentences about what this project is.
```

```bash
uv run -m indexer.cli index --project my-app
```

From then on the background sync keeps it current (changed files re-indexed
by content hash, deleted files purged, renames handled) — and the assistant
can also call `sync_index` itself right after writing files.

---

## Automatic updates

Every time the server starts (i.e. whenever a client session begins),
rememory quietly checks this repo's GitHub origin. If new commits exist you
see, in the client's server log:

```
rememory: new update available (a1b2c3d)
rememory: updating [############################] 100%
rememory: updated successfully (a1b2c3d)
```

…and the session continues on the fresh version. The details that keep this
safe:

- **Never blocks startup** — offline or unreachable GitHub is a silent skip.
- **Never touches your changes** — the pull is fast-forward-only, and if you
  have modified tracked files the updater stands down and tells you how to
  update manually.
- **Throttled** — at most one check per 15 minutes even if several clients
  launch servers at once.
- **Opt-out** — set the environment variable `REMEMORY_AUTO_UPDATE=0` in
  your MCP client's server config to disable it entirely.

---

## Use-case scenarios

### Scenario 1 — continuing yesterday's work in a fresh session

*Monday evening, wrapping up:*

> **You:** That's it for today.
> **Assistant:** *(calls `save_session`)* Saved a handoff: we migrated the
> auth middleware to the new token format; `refresh_flow.py` passes tests but
> `legacy_shim.py` still fails two cases. Next steps and the 3 relevant files
> are recorded.

*Tuesday morning, brand-new session with zero context:*

> **You:** `/mcp__rememory__kickoff my-app`
> **Assistant:** *(calls `get_briefing`)* Resuming: the auth-middleware
> migration. `legacy_shim.py` has two failing cases around expiry handling —
> I'll read the three files from the handoff and continue from there.

The new session reads **3 files instead of 30**, and starts with every
architecture decision the project has accumulated — not just yesterday's
diff.

### Scenario 2 — the decision you made four months ago, on another project

> **You:** How should tenant data be isolated in this new service?
> **Assistant:** *(calls `search_memory` with no project filter)* On your
> `acme-crm` project you decided to enforce isolation in the database with
> forced row-level security rather than in application code — the recorded
> rationale was that one missed WHERE clause would otherwise leak client
> data. Recommend the same pattern here; want me to apply it?

The decision — *with its why* — outlives the session, the project, and your
own recollection of having made it.

---

## Starting and stopping — no terminal needed

Normally there is nothing to start: Qdrant restarts with Docker, Ollama
starts at login, and the memory server itself is spawned by your client on
demand. Two layers cover the exceptions:

- **Self-healing** — if the database container is stopped when a session
  begins (reboot, Docker restarted without it), the server quietly starts it
  itself: you just see `rememory: starting the local database... ready.` in
  the log. Most "it isn't running" cases fix themselves this way.
- **Start-menu shortcuts** (Windows, created by setup): search the Start
  menu for **"Start rememory"**, **"Stop rememory"**, or **"rememory
  Status"**. Each opens a small plain-language window — Start brings up
  Docker and the whole stack, Stop shuts down rememory's own pieces (leaving
  Docker Desktop and Ollama alone, since other apps may use them), Status
  shows a green/yellow health list and what's indexed.

On macOS/Linux the equivalents are `docker compose -f docker/compose.yml
up -d` / `stop`, and `uv run -m indexer.cli status`.

## Day-to-day commands

Mostly you need none — sync and backup run on their own. When you want them:

```bash
uv run -m indexer.cli status                      # what's indexed, per project
uv run -m indexer.cli sync                        # incremental sync, all projects
uv run -m indexer.cli search "query" --project X  # search from the terminal
uv run -m indexer.cli explain <path> --project X  # why isn't this file indexed?
uv run -m indexer.cli chunks <path> --project X   # preview how a file is chunked
uv run tests/eval_retrieval.py                    # retrieval-quality scoreboard
uv run scripts/export_memory.py                   # manual backup now
uv run scripts/import_memory.py --dry-run         # preview a restore
uv run scripts/connect.py                         # re-print client config
```

Backups: the `memory` collection (the only irreplaceable data) is exported
daily to plain JSON in `data/backups/` — payload-only, so it can be restored
into any future version *or any future embedding model* (vectors are
recomputed on import).

---

## Design, security, contributing

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — every design decision with
  the alternatives that were rejected and why.
- [SECURITY.md](SECURITY.md) — threat model, defenses, and honest residual
  risks.
- [CONTRIBUTING.md](CONTRIBUTING.md) — test matrix and the invariants you
  must not break.

One deliberate deviation from MCP naming guidance, documented here for
reviewers: tool names are unprefixed (`search_code`, not
`rememory_search_code`) because every major client already namespaces tools
by server name — prefixing would render as `mcp__rememory__rememory_search_code`.

## Requirements

- Windows 10/11, macOS, or Linux
- Docker, Ollama, ~3 GB disk (models + index), git
- A GPU helps (the two models total ~2.7 GB VRAM) but CPU works

## Uninstall

```bash
docker compose -f docker/compose.yml down
claude mcp remove --scope user rememory       # if you registered Claude Code
# Windows also:
schtasks /Delete /TN RememorySync /F
schtasks /Delete /TN RememoryBackup /F
# then delete the repo folder (data/ holds your memories -- export first!)
```

## License

MIT — see [LICENSE](LICENSE).
