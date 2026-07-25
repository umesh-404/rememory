# Security model

## Threat model in one paragraph

rememory is a single-user, single-machine tool. Everything — vectors, payloads,
memories, models — lives and runs locally. The one channel through which
indexed content can leave the machine is a **search result entering Claude's
context**, because Claude Code conversations are processed by Anthropic. The
design treats that channel as the boundary to defend.

## Defenses in place

1. **Network exposure**: Qdrant binds to `127.0.0.1` only (both REST and
   gRPC). Nothing on your LAN can reach it. It runs without authentication
   *because of* that bind — if you ever publish the port beyond loopback, add
   a Qdrant API key first.
2. **Credential files are never indexed**: `.env*`, `.netrc`, `.npmrc`,
   `id_rsa`/`id_ed25519`, `credentials*.json`, and key-material extensions
   (`.pem`, `.key`, `.pfx`, `.p12`, `.jks`, …) are excluded by explicit
   rule, not by luck.
3. **Secrets inside normal files are redacted at ingestion**
   (`indexer/redact.py`): high-confidence token formats (AWS, GitHub, OpenAI,
   Anthropic, Slack, Stripe, GCP, JWTs), private-key blocks, and quoted
   secret-named assignments are replaced with `[REDACTED]` *before* embedding
   and storage. A credential that never enters the store cannot be retrieved.
   Redaction preserves line counts, so citations stay correct, and keeps a
   short prefix (`ghp_[REDACTED]`) so the *location* of a token remains
   searchable.
4. **Prompt-injection stance**: retrieved chunks are file content and should
   be treated by the consuming model as data, not instructions. rememory
   returns verbatim text with provenance (`file:line`) so the consumer can
   attribute and verify.
5. **No telemetry**: Qdrant telemetry is disabled in `config/qdrant.yaml`;
   rememory itself phones nowhere.

6. **Auto-update provenance**: on startup rememory fast-forwards to the
   latest commit of *your configured git origin* -- it trusts that remote
   completely, which is the same trust you expressed by cloning it. It never
   switches remotes or branches, never overwrites local modifications, and
   can be disabled with `REMEMORY_AUTO_UPDATE=0`.

## Residual risks you should know about

- Redaction is pattern-based. A secret in an unusual format (or split across
  lines) can slip through. Don't index repos you wouldn't paste from.
- `data/` contains your full index and memories in plaintext-equivalent form.
  Disk encryption (BitLocker/FileVault) is your friend; the daily JSON export
  in `data/backups/` is equally sensitive.
- MCP tools are available to any Claude Code session on your machine; there
  is no per-project ACL. This matches the single-user threat model.

## Reporting

Open a GitHub issue for non-sensitive reports. For anything sensitive, use
GitHub's private vulnerability reporting on the repository.
