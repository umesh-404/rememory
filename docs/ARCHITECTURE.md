# rememory architecture

Every load-bearing decision, with the alternatives that were rejected and why.
Written so a contributor can predict what a change will break.

## The stack, and why each piece

| Piece | Why this one |
|---|---|
| **Qdrant** (Docker, pinned tag) | First-class payload filtering *combined* with vector search, native hybrid search + server-side RRF fusion, sparse-vector IDF (`modifier: idf`) so BM25-style scoring needs no client-side corpus stats. Chroma is weaker on filtering at scale; pgvector needs hand-built hybrid; Milvus/Weaviate are heavier than one workstation warrants; FAISS is a library, not a database. The tag is pinned because Qdrant persists an on-disk format — `latest` could upgrade the engine under your data. |
| **Ollama** | Shared local model runtime: holds models resident on the GPU across processes. In-process `sentence-transformers` would mean a CUDA stack inside our env, model load on every process start, and VRAM competition invisible to other apps. |
| **qwen3-embedding:0.6b** (1024-d, cosine) | Chosen by benchmark, not reputation: on a no-shared-vocabulary gold set it beat `nomic-embed-text` 72.7% vs 54.5% Recall@1. ~3× slower, but batching at 32 (the measured knee) makes a 50k-chunk full index ~16 min, and query latency (~60 ms) is what users feel. `scripts/benchmark_embeddings.py` re-runs the comparison for any candidate model. |
| **Qwen3-Reranker-0.6B** | Cross-encoder second stage. Ollama has no rerank API, so scoring uses the official prompt template in raw mode, one generated token, and softmax over the yes/no logprobs. Measured: relevant ≈0.9–1.0, related-but-wrong ≈0.02–0.08. |
| **uv** | One-command reproducible env; pins Python 3.12 (`.python-version`) independent of the system Python. |
| **tree-sitter** (`tree-sitter-language-pack`) | Real parse trees from prebuilt wheels, error-tolerant (half-written code still parses). Fixed windows sever functions; regex breaks on decorators/generics; Python `ast` is Python-only; LangChain splitters wrap tree-sitter anyway. |
| **pathspec** | `.gitignore` semantics are subtle (negation, anchoring, `**`); this is the reference implementation. |
| **No BM25 library** | Sparse vectors are ~40 lines of stdlib: CRC32-hashed tokens (deterministic across processes — Python's `hash()` is salted and would never match) with term frequencies; Qdrant applies IDF server-side. |

## Collections: partitioned by durability, not by project

`code` and `docs` are **derived** — rebuildable from files at any time.
`memory` is **authored** — irreplaceable. They are separate collections so
that "re-index project X", an operation run hundreds of times, is structurally
incapable of destroying knowledge: the re-index path has no write access to
`memory`, and tooling refuses to bulk-delete it.

Projects are a `project` payload field with `is_tenant: true` (Qdrant
physically co-locates each tenant's points), not per-project collections:
cross-project memory search is one query, and collections carry per-collection
overhead that would grow with every project. The accepted trade-off — filter
bugs could leak across projects — is mitigated by making `project` a
*mandatory* argument on code/docs search and covered by a tenant-isolation
test.

Vector config: named dense vector (`dense`, 1024, cosine) + declared sparse
(`lexical`, IDF) — declared at creation even before use, because vector config
is immutable afterwards. HNSW keeps the default global graph (`m=16`) rather
than the multitenancy-guide `m=0`+`payload_m`, because unfiltered
cross-project search is a feature, and `m=0` would degrade it to a scan.

## Indexing pipeline

`discover → chunk → embed (batched) → upsert`, one file at a time so an
interrupted run is partially indexed but consistent.

- **Discovery**: cheapest-first filters (dir prune → name → extension → size →
  content probe); binary detection by content (NUL byte / UTF-8 failure), not
  extension; `.gitignore` respected — including nested files, with git's own
  precedence (patterns relative to their directory, deeper files override
  shallower ones) and gitignored directories pruned from the walk; skips
  counted by reason and explorable via `cli explain <path>`.
- **Chunking = the quality lever.** One idea per vector: functions, methods
  (qualified `Class.method`), classes (header includes the docstring — it
  lives syntactically in the body), doc sections with full heading trails.
  Decorated definitions resolve name/kind from the inner node (a decorated
  class is a container; a decorated function is not). Symbols too small to
  stand alone fall into grouped module-leftover chunks rather than being
  dropped. Anything unparseable degrades to overlapping line windows — a file
  is never lost to a syntax error.
- **Contextual headers**: the *embedded* text is prefixed with
  `path :: symbol` / `path :: heading trail` (supplies the vocabulary a bare
  chunk lacks); the *stored* payload is verbatim file content. Consequence:
  renames must re-embed — vectors are path-contaminated by design.
- **Deterministic ids**: `uuid5(NAMESPACE, "project:path:chunk_index")` —
  re-indexing overwrites instead of duplicating; neighbour expansion is an
  O(1) retrieve. Files are delete-then-write so a shrunk file leaves no
  orphan tail chunks.
- **Incremental sync**: the index itself is the ledger. Walk, scroll
  `source_path`+`content_hash`, reconcile: unchanged → skip; changed → rewrite;
  in-index-but-not-on-disk (per collection, so reclassified files don't
  strand copies) → purge. Renames are detected (same hash, new path) but
  re-embedded, never vector-copied. `--limit` runs skip deletion — a partial
  walk would misread absent files as deleted.

## Retrieval pipeline

```
query ── dense embed (cached) ──┐
     └── sparse tokens ─────────┤ RRF fusion (server-side, over-fetch ~10)
                                ├── cross-encoder rerank (top candidates)
                                ├── recency bias (memory collection ONLY)
                                ├── per-file diversity cap (max 2)
                                └── optional neighbour expansion
```

- Both stages fail soft: sparse failure → dense-only; rerank failure → RRF
  order (a 404 disables reranking for the process, with the fix in stderr).
  Search must never fail because an enhancement did.
- The sparse query builder is the *same imported function* used at index time;
  a copy would drift and silently break exact-identifier matching.
- Rerank scores (0–1) ride on every result; tool docstrings tell Claude to
  distrust <0.3 — a calibrated answerhood signal, not an opaque rank.
- Recency bias (×0.85–1.0, ~90-day constant) applies to `memory` only. Old
  code that matches best IS the right answer; aging it would be wrong.
- Reranking runs in a dedicated thread+loop when the caller is already async
  (`asyncio.run` inside a running loop would otherwise silently degrade).

## The memory collection's write policy

- Validation: registered project, known type, 40–8000 chars (below = a note
  to self; above = a transcript dump — distill it).
- **Updates supersede, never overwrite**: old version stays with
  `status: superseded` + `superseded_by` link. The history of what you
  believed and when is most of an ADR's value.
- **Near-duplicate guard**: a new memory whose nearest active neighbour
  exceeds 0.85 cosine is rejected with that memory's id (threshold calibrated
  against measured distributions: same-fact paraphrase ≈0.91, nearest
  distinct ≈0.54). `allow_duplicate=true` overrides; superseding writes skip
  the check.
- Deletes take exactly one id. No filter-delete path exists.
- Daily backups are **payload-only JSON** — no vectors — because vectors are
  derivable and payloads are not. Restore re-embeds with the current model,
  which makes the backup double as the embedding-model migration path.

## Cross-file invariants (enforced in code, not comments)

1. `embedding.yaml` dims/distance == collection vector config ==
   `schema_version` across all config files — asserted at startup and by
   `create_collections.py`, because a mismatch produces no error, only
   silently meaningless search.
2. Embedder validates returned dimensionality on every batch.
3. Over-long text is truncated client-side before embedding (the model
   truncates silently otherwise).
4. All MCP diagnostics go to stderr — stdout carries JSON-RPC, and one stray
   print corrupts the session. (This is also why the package is `memory_mcp`:
   a local package named `mcp` shadows the SDK.)

## Evaluation

`config/eval.yaml` holds a golden set of paraphrased queries with
known-correct files; `tests/eval_retrieval.py` runs them through the live
pipeline with and without reranking, reports Recall@1/@3 and MRR, and fails
below 80% reranked Recall@3. When a real search disappoints: add the query to
the golden set, then tune against a scoreboard.

## Ingestion security

Search results enter the assistant's context, and conversations may leave the
machine -- that is the one hole in the local-only story, and it is closed at
ingestion (`indexer/redact.py`): high-confidence token formats (AWS, GitHub,
OpenAI, Anthropic, Slack, Stripe, GCP, JWT), private-key blocks, and
secret-named quoted assignments are replaced with `[REDACTED]` before
embedding or storage. Replacements never change line counts (citations stay
correct) and keep a short prefix (`ghp_[REDACTED]`) so the *location* of a
credential stays searchable. Credential files (`.env*`, `id_rsa`, `*.pem`,
`.netrc`, ...) are excluded from indexing entirely, by explicit rule. Details
and residual risks: SECURITY.md.

## Session continuity

`session` is a memory type with a one-active-per-project rule: `save_session`
(summary, next steps, files to read first) supersedes the previous handoff.

**The briefing is an index, not an archive** (`memory_mcp/briefing.py`). It is
built to a hard character budget (default 5,000; it used to inline every
decision in full and reached ~121k characters on a long-lived project, so
the tool meant to start a session was the one agents avoided). Sections get a
fixed share of the budget in priority order: the handoff (summary, open tasks,
files to read first), what changed since that handoff (decisions added,
memories retired), trust warnings (index behind the last commit, suspect /
probably-replaced memories, other agents' file claims), the migration head and
next decision number, the ten newest decisions as excerpts sized to fit, then
titles of verified recipes. Detail stays behind `get_memory` / search.

**Delivered without being asked.** `scripts/agent_hook.py` is one hook entry
point for every agentic IDE/CLI with lifecycle hooks; it accepts each client's
stdin field names and prints each client's output shape
(`hookSpecificOutput.additionalContext` for Claude Code, Codex, VS Code
Copilot and Gemini CLI; `additional_context` for Cursor; plain stdout for
Kiro). Claude Code and Codex also fire it after **context compaction** -- the
moment an agent loses its working picture mid-session. Injected text is phrased
as facts, never instructions, so prompt-injection defences do not trip.

## Memory staleness

A stale memory is worse than none: it reads as authoritative. Four mechanisms,
none of which ever deletes anything:

- **Dependencies.** `store_memory(depends_on=[path | path::Symbol])` records a
  content hash per dependency (a symbol hashes only its own lines, so edits
  elsewhere in the file do not raise alarms). After every sync,
  `memory_mcp/aftersync.py` re-hashes the dependencies of memories touching
  changed paths and marks drifted ones `suspect`; search labels them SUSPECT,
  briefings list them. `confirm_memory` re-baselines; `update_memory` /
  `retire_memory` resolve.
- **Supersession by reference.** "supersedes D-123" (any `ID-123` form) in a
  new memory retires the active memory whose title carries that id.
- **Related-on-write.** `store_memory` returns active memories in the
  0.62-0.85 similarity band -- the same subject but not a duplicate, exactly
  where a new fact silently contradicts an old one -- so the writer fixes them
  in the same step. `review_memories` does NOT treat topic similarity as
  staleness (parts of one series, or a feature and its follow-up, share a
  topic without either being wrong). It flags only evidence: a newer memory
  says it supersedes/replaces/reverses/overrides an id in this one's title,
  this one's own title says SUPERSEDED/OBSOLETE while it is still active, a
  newer memory restates it almost verbatim (similarity >= 0.92, numbered
  series excepted), changed dependencies, passed recheck dates, and repo files
  it names that are gone (checked against `git ls-files`, so paths relative to
  a sub-app count, and text recording the deletion is not flagged).
- **Short ids.** Every tool taking a memory id accepts the 8-character form
  that briefings and reviews print (any unique prefix of 6+ characters).
- **Evidence and expiry.** `evidence` (tested / read-at-source / reported /
  inferred) and `verified_at` travel with every result. `vendor` memories
  require a source URL and expire after 30 days (`recheck_after`); search
  labels them EXPIRED until re-confirmed.

## Index freshness

- Git hooks (`post-commit`, `post-merge`, `post-checkout`, `post-rewrite`) and
  agent post-edit hooks run `scripts/scheduled.py sync <project>`: each event
  only marks the project dirty; one worker per project syncs until clean
  (debounced 4 s, waits politely on the global index lock). A rebase of
  twenty commits costs one or two syncs.
- Searches verify the files they return (mtime first, hash only on mismatch);
  any drift triggers a project sync and a re-run of the search before the
  answer goes back. Agents no longer need to remember `sync_index`.
- Discovery keeps a stat cache (`data/statcache/`): unchanged files cost one
  `stat()`, not a read and a SHA-256 -- the same trade-off git's index makes.

## Repo facts, test map, symbols, claims

- `memory_mcp/facts.py` + `memory_mcp/detect.py`: facts computed from the
  working tree at call time (never stored, so they cannot rot) -- migration
  heads across the common ORMs, CI and local gates, toolchain and commands,
  ID sequences (ADR folders, inline decision ids). Optional per-project
  `facts:` config adds sequences, Python constants and cross-file set checks
  (e.g. every alarm code needs a severity row and an index row).
- `memory_mcp/testmap.py`: snapshots a coverage.py data file recorded with
  per-test contexts (`--cov-context=test` or `dynamic_context`) whenever it is
  newer than the last snapshot; `tests_for` answers by numbits/arc lookup,
  else a labelled heuristic. A generated rcfile adds contexts to a project's
  own coverage settings without changing any count.
- `memory_mcp/symbols.py`: definitions located through the index, re-anchored
  in the live file; callers from `git grep`, attributed to their enclosing
  function.
- `memory_mcp/localdb.py`: SQLite (WAL, `BEGIN IMMEDIATE`) for file claims and
  ID reservations -- atomic across the separate server processes that parallel
  agents run.

## Resource use

- int8 scalar quantization on the derived `code`/`docs` vectors (originals on
  disk, rescored with 2x oversampling; measured 99.5% top-10 overlap and an
  identical top hit on 100% of test queries). `memory` stays exact.
- Ollama `keep_alive`: 15 min embedder, 10 min reranker.
- The stale-memory review is cached per project, invalidated on any write.

## Concurrency and the MCP surface

- **Single-writer lock** (`indexer/lockfile.py`): every indexing path -- CLI,
  scheduled sync, and the MCP `sync_index` tool -- goes through one atomic
  O_EXCL lockfile (heartbeat-refreshed; stale after 5 min). Busy callers get a clear message
  instead of racing delete-then-write on the same points.
- **MCP hygiene**: all tools carry spec annotations (`readOnlyHint` on reads,
  `destructiveHint` only on `delete_memory`, `openWorldHint: false`
  everywhere -- the server talks only to localhost). Caller-supplied sizes
  are clamped (search limit <= 25, list limit <= 100) so a bad argument
  cannot flood the model's context; `list_memories` is paginated with
  `total`/`has_more`/`offset`. Tool names are deliberately *unprefixed*
  (`search_code`, not `rememory_search_code`): every major client namespaces
  tools by server name already, and prefixing would render as
  `mcp__rememory__rememory_search_code`.
- **Long-lived process discipline**: the MCP server reuses one Qdrant client
  and one pipeline across `sync_index` calls (no per-call connection pools),
  and the query-embedding cache is bounded.

## Ports

No port is hardcoded anywhere except as a default in `indexer/runtime.py`.
Setup probes for a free port, writes the choice to `config/runtime.json`
(gitignored), and every consumer reads it from `runtime()`. Docker receives it
through environment substitution in `compose.yml`
(`${REMEMORY_QDRANT_PORT:-6333}`), which is why every `docker compose`
invocation must pass `compose_env()` -- otherwise the container would publish
the default port while Python talked to the chosen one. `port_is_ours()`
exists so a re-run of setup recognises its own healthy Qdrant on 6333 as
"already mine" rather than as a conflict. The desktop app's single-instance
lock scans a range (49517-49526) for the same reason: one squatted port must
not be mistaken for "already running".

## Desktop app (`app/`)

Two processes, because pystray and pywebview each demand the main thread:
`main.py` owns the tray on the main thread and launches `window.py` as a
child process for the dashboard. They share no state -- `backend.py` is the
single implementation of every action, and every action is a subprocess call
(docker/uv/git) or a localhost HTTP call, so either process can perform it
independently. `backend.Api` never raises into the UI: everything returns
`{ok, message}` and the front-end shows a toast. The UI (`app/ui/`) is plain
HTML/CSS/JS with no build step and no external assets, so `git pull` updates
the interface exactly like it updates the Python.

## Start/Stop scoping

Both are deliberately narrow, because every dependency is shared with the rest
of the machine. **Stop** stops the container through compose (which by
definition only knows our own service, so other containers cannot be affected)
and unloads our two models by NAME via Ollama's `keep_alive: 0` -- other
models stay resident and the Ollama process is never touched. Model names come
from `config/embedding.yaml`, so this can never drift from what the indexer
and reranker actually use. **Start** reverses both and pre-warms the models.
Unloading is asynchronous in Ollama (`/api/ps` briefly still lists a freed
model), so the result is confirmed by polling rather than a single check.

## Deliberately not included

- **LLM auto-extraction of memories** — Claude is the extractor; explicit
  `store_memory` keeps the collection curated, not scraped.
- **Smart forgetting/eviction** — at a few hundred curated memories, silent
  deletion is a bug. Staleness flags in briefings surface age; humans decide.
- **Knowledge-graph memory** — the supersede chain + payload filters answer
  every query pattern a single developer's project knowledge actually has.
- **Server-side query rewriting** — the query writer is already an LLM.
- **Quantization** — trades accuracy for RAM we aren't short of.
