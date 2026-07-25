#!/usr/bin/env bash
# rememory master setup (macOS / Linux). Run once after cloning:
#
#     ./setup.sh
#
# Installs and verifies everything automatically with step-by-step progress.
# The only thing left at the end is pasting one config snippet into the MCP
# client of your choice -- the script prints exactly what to paste, with your
# machine's real paths filled in.
#
# Idempotent: safe to re-run; completed steps are fast no-ops.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

TOTAL=9
STEP=0
step() { STEP=$((STEP+1)); printf '\n\033[36m[%d/%d] %s\033[0m\n' "$STEP" "$TOTAL" "$1"; }
ok()   { printf '      \033[32m%s\033[0m\n' "$1"; }
info() { printf '      %s\n' "$1"; }
fail() {
  printf '\n\033[31mSETUP FAILED at step %d/%d\n  %s\033[0m\n' "$STEP" "$TOTAL" "$1"
  echo "  Fix the issue and re-run ./setup.sh -- completed steps are skipped fast."
  exit 1
}

echo "rememory setup -- local, private development memory (Qdrant + Ollama + MCP)"
echo "Everything runs on this machine. Nothing is sent anywhere."

# Refuse to install inside a cloud-synced folder. `uv sync` writes tens of
# thousands of small files into .venv and some dependencies compile from
# source; a sync client intercepts every write, so the install does not fail,
# it crawls and looks hung. Qdrant's storage is memory-mapped and is actively
# corrupted by a sync client copying it mid-write.
case "$ROOT" in
  */OneDrive/*|*/Dropbox/*|*/"Google Drive"/*|*/GoogleDrive/*|*/"Library/Mobile Documents"/*)
    if [ "${REMEMORY_ALLOW_CLOUD_FOLDER:-0}" != "1" ]; then
      printf '\n\033[31mCANNOT INSTALL HERE -- this folder is synced to the cloud\n  %s\033[0m\n\n' "$ROOT"
      echo "  Installing here makes setup crawl to a near-halt and risks corrupting"
      echo "  the vector database, which is memory-mapped and must not be synced."
      echo ""
      echo "  Move this folder somewhere local and re-run, for example ~/rememory"
      echo "  (delete any partly-built .venv first)."
      echo ""
      echo "  To install here anyway: REMEMORY_ALLOW_CLOUD_FOLDER=1 ./setup.sh"
      exit 1
    fi
    printf '\033[33m  WARNING: installing into a synced folder -- expect a slow install.\033[0m\n'
    ;;
esac

step "Checking prerequisites (Docker, Ollama)"
command -v docker >/dev/null || fail "Docker not found: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || fail "Docker daemon not running -- start Docker and re-run."
ok "docker running"
command -v ollama >/dev/null || fail "Ollama not found: https://ollama.com/download"
ollama_up() { curl -sf --noproxy '*' http://127.0.0.1:11434/api/tags >/dev/null; }
if ! ollama_up; then
  # Start it rather than telling the user to: after a reboot it may simply
  # not be running yet. macOS has the app bundle; elsewhere, a background
  # `ollama serve` (logged, disowned) covers CLI installs.
  info "Ollama is not running -- starting it..."
  if [ "$(uname)" = "Darwin" ] && open -a Ollama 2>/dev/null; then :; else
    mkdir -p data/logs
    nohup ollama serve >> data/logs/ollama.log 2>&1 &
    disown 2>/dev/null || true
  fi
  started=""
  for i in $(seq 1 20); do
    if ollama_up; then started=1; break; fi
    sleep 3
  done
  [ -n "$started" ] || fail "Ollama did not come up after being started -- start it manually and re-run."
fi
ok "ollama running"

step "Ensuring uv (Python toolchain manager)"
if ! command -v uv >/dev/null; then
  info "uv not found -- installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "uv installed but not on PATH -- open a new shell and re-run."
fi
ok "uv at $(command -v uv)"

step "Pulling local AI models into Ollama (first run downloads ~1.9 GB; shows its own progress)"
ollama pull qwen3-embedding:0.6b || fail "Could not pull the embedding model."
ok "embedding model ready (qwen3-embedding:0.6b)"
ollama pull dengcao/Qwen3-Reranker-0.6B:F16 || fail "Could not pull the reranker model."
ok "reranker model ready (Qwen3-Reranker-0.6B)"

step "Starting the vector database (Qdrant in Docker, data stays in $ROOT/data)"
mkdir -p data/qdrant/storage data/qdrant/snapshots data/logs data/backups
# Pre-pull with retries: a still-settling Docker engine intermittently fails
# image requests (500); a pause-and-retry rides it out.
pulled=""
for attempt in 1 2 3; do
  if docker pull qdrant/qdrant:v1.18.3; then pulled=1; break; fi
  [ "$attempt" -lt 3 ] && { info "image pull failed (attempt $attempt/3) -- retrying in 15s..."; sleep 15; }
done
[ -n "$pulled" ] || fail "Could not pull qdrant/qdrant:v1.18.3 -- if the error mentions a 500, restart Docker and re-run."
# Remove any stale container from a failed run: it may hold the port and
# crash-loop; all data lives in the bind mount, so this loses nothing.
docker rm -f rememory-qdrant >/dev/null 2>&1 || true
if ! docker compose -f docker/compose.yml up -d; then
  command -v docker-compose >/dev/null && docker-compose -f docker/compose.yml up -d || fail "docker compose failed."
fi
# Read the chosen port rather than assuming 6333: config/runtime.json is
# written when a machine has to move Qdrant off a busy port, and hardcoding
# the default made this script probe the wrong port and declare a healthy
# database dead. --noproxy matters for the same reason it does in Python:
# curl honours http_proxy, and a proxied loopback request always fails.
QPORT=6333
if [ -f config/runtime.json ]; then
  QPORT=$(sed -n 's/.*"qdrant_port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' config/runtime.json | head -1)
  [ -n "$QPORT" ] || QPORT=6333
fi
ready=""
info "waiting for Qdrant to become ready (up to 60s)..."
for i in $(seq 1 30); do
  if curl -sf --noproxy '*' "http://127.0.0.1:$QPORT/readyz" >/dev/null; then ready=1; break; fi
  # A container that has already stopped is never going to answer.
  if [ "$i" -ge 3 ]; then
    state=$(docker inspect -f '{{.State.Status}}' rememory-qdrant 2>/dev/null || echo unknown)
    case "$state" in running|created) ;; *) break ;; esac
  fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo ""
  printf '\033[33m      Qdrant did not answer. Container state and last log lines:\033[0m\n'
  docker inspect -f '      state={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}}' rememory-qdrant || true
  docker logs --tail 25 rememory-qdrant 2>&1 || true
  echo ""
  logs=$(docker logs --tail 400 rememory-qdrant 2>&1 || true)
  case "$logs" in
    *"Failed to load local shard"*)
      case "$logs" in
        *storage/collections/memory/*)
          echo "      The 'memory' collection is corrupt and cannot be rebuilt by"
          echo "      re-indexing. Restore it with:  uv run scripts/import_memory.py"
          fail "Qdrant cannot start: the memory collection is damaged."
          ;;
      esac
      printf '\033[36m      Corrupt index data in a rebuildable collection -- recovering...\033[0m\n'
      uv run --no-project scripts/recover_storage.py --yes || true
      for i in $(seq 1 30); do
        if curl -sf --noproxy '*' "http://127.0.0.1:$QPORT/readyz" >/dev/null; then ready=1; break; fi
        sleep 2
      done
      ;;
  esac
fi
[ -n "$ready" ] || fail "Qdrant did not become ready -- the container output above says why."
ok "qdrant ready on 127.0.0.1:$QPORT (loopback only -- unreachable from the network)"

step "Building the Python environment (uv sync -- pinned Python 3.12, locked deps)"
uv sync || fail "uv sync failed."
ok "environment ready"

step "Creating vector collections (code / docs / memory)"
uv run scripts/create_collections.py || fail "collection creation failed."
ok "collections verified"

step "Preparing your project registry"
if [ ! -f config/projects.yaml ]; then
  cp config/projects.example.yaml config/projects.yaml
  ok "created config/projects.yaml -- add your own projects there after setup"
else
  ok "config/projects.yaml already exists -- keeping yours"
fi

step "Background automation"
# rememory deliberately does not index its own source -- see setup.ps1 for the
# reasoning. The round-trip test in the next step still proves the pipeline.
info "seeding example memories (skips quietly if already seeded)..."
uv run scripts/seed_memories.py || true
info "background sync: cron is yours to configure on Unix -- suggested line:"
info "  */30 * * * *  cd $ROOT && $(command -v uv) run -m indexer.cli sync"
info "  0 12 * * *    cd $ROOT && $(command -v uv) run scripts/export_memory.py"
ok "automation configured"

step "Verifying the installation (test suite)"
uv run tests/test_unit.py || fail "unit tests failed."
uv run tests/test_roundtrip.py || fail "end-to-end round trip failed."
ok "all verification tests passed"

printf '\n\033[32mSetup complete.\033[0m\n'
uv run scripts/connect.py
