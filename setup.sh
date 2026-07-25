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

step "Checking prerequisites (Docker, Ollama)"
command -v docker >/dev/null || fail "Docker not found: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || fail "Docker daemon not running -- start Docker and re-run."
ok "docker running"
command -v ollama >/dev/null || fail "Ollama not found: https://ollama.com/download"
curl -sf http://127.0.0.1:11434/api/tags >/dev/null || fail "Ollama not responding -- start it ('ollama serve' or the app) and re-run."
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
docker compose -f docker/compose.yml up -d || fail "docker compose failed."
info "waiting for Qdrant to become ready..."
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:6333/readyz >/dev/null && break
  sleep 2
done
curl -sf http://127.0.0.1:6333/readyz >/dev/null || fail "Qdrant not ready: docker logs rememory-qdrant"
ok "qdrant ready on 127.0.0.1:6333 (loopback only -- unreachable from the network)"

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

step "First index + background automation"
info "indexing rememory itself (proves the whole pipeline end to end)..."
uv run -m indexer.cli index --project rememory || fail "indexing failed."
info "seeding example memories (skips quietly if already seeded)..."
uv run scripts/seed_memories.py || true
info "background sync: cron is yours to configure on Unix -- suggested line:"
info "  */30 * * * *  cd $ROOT && $(command -v uv) run -m indexer.cli sync"
info "  0 12 * * *    cd $ROOT && $(command -v uv) run scripts/export_memory.py"
ok "index built"

step "Verifying the installation (test suite)"
uv run tests/test_unit.py || fail "unit tests failed."
uv run tests/test_roundtrip.py || fail "end-to-end round trip failed."
ok "all verification tests passed"

printf '\n\033[32mSetup complete.\033[0m\n'
uv run scripts/connect.py
