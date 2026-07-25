# rememory master setup (Windows). Run once after cloning:
#
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Installs and verifies EVERYTHING automatically, showing progress for each
# step. The only thing left for you at the end is pasting one config snippet
# into the MCP client of your choice (Claude Code, Claude Desktop, Cursor,
# Windsurf, VS Code, ...) -- the script prints exactly what to paste, with
# your machine's real paths filled in.
#
# Idempotent: safe to re-run at any time; completed steps are fast no-ops.

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

$TotalSteps = 9
$script:StepNo = 0
function Step([string]$msg) {
    $script:StepNo++
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $script:StepNo, $TotalSteps, $msg) -ForegroundColor Cyan
}
function Ok([string]$msg) { Write-Host "      $msg" -ForegroundColor Green }
function Info([string]$msg) { Write-Host "      $msg" }
function Fail([string]$msg) {
    Write-Host ""
    Write-Host "SETUP FAILED at step $script:StepNo/$TotalSteps" -ForegroundColor Red
    Write-Host "  $msg" -ForegroundColor Red
    Write-Host "  Fix the issue and re-run setup.ps1 -- completed steps are skipped fast."
    exit 1
}

Write-Host "rememory setup -- local, private development memory (Qdrant + Ollama + MCP)"
Write-Host "Everything runs on this machine. Nothing is sent anywhere."

# ---------------------------------------------------------------------------
Step "Checking prerequisites (Docker, Ollama)"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker not found. Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
}
docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Docker daemon is not running. Start Docker Desktop, wait for it to say 'running', then re-run." }
Ok "docker running"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Fail "Ollama not found. Install it: https://ollama.com/download"
}
try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 5 | Out-Null }
catch { Fail "Ollama is installed but not responding. Start the Ollama app and re-run." }
Ok "ollama running"

# ---------------------------------------------------------------------------
Step "Ensuring uv (Python toolchain manager)"
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) {
    Info "uv not found -- installing via winget..."
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements --disable-interactivity
    $env:Path += ";$env:LOCALAPPDATA\Microsoft\WinGet\Links"
    $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $Uv) { Fail "uv installed but not yet on PATH. Open a NEW terminal and re-run setup.ps1." }
}
Ok "uv at $Uv"

# ---------------------------------------------------------------------------
Step "Pulling local AI models into Ollama (first run downloads ~1.9 GB; shows its own progress)"
ollama pull qwen3-embedding:0.6b
if ($LASTEXITCODE -ne 0) { Fail "Could not pull the embedding model." }
Ok "embedding model ready (qwen3-embedding:0.6b)"
ollama pull dengcao/Qwen3-Reranker-0.6B:F16
if ($LASTEXITCODE -ne 0) { Fail "Could not pull the reranker model." }
Ok "reranker model ready (Qwen3-Reranker-0.6B)"

# ---------------------------------------------------------------------------
Step "Starting the vector database (Qdrant in Docker, data stays in $Root\data)"
New-Item -ItemType Directory -Force "$Root\data\qdrant\storage", "$Root\data\qdrant\snapshots", "$Root\data\logs", "$Root\data\backups" | Out-Null
docker compose -f "$Root\docker\compose.yml" up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed -- see output above." }
Info "waiting for Qdrant to become ready..."
$ready = $false
foreach ($i in 1..30) {
    try { Invoke-RestMethod http://127.0.0.1:6333/readyz -TimeoutSec 2 | Out-Null; $ready = $true; break }
    catch { Start-Sleep 2 }
}
if (-not $ready) { Fail "Qdrant did not become ready. Check: docker logs rememory-qdrant" }
Ok "qdrant ready on 127.0.0.1:6333 (loopback only -- unreachable from the network)"

# ---------------------------------------------------------------------------
Step "Building the Python environment (uv sync -- pinned Python 3.12, locked deps)"
& $Uv sync
if ($LASTEXITCODE -ne 0) { Fail "uv sync failed." }
Ok "environment ready"

# ---------------------------------------------------------------------------
Step "Creating vector collections (code / docs / memory)"
& $Uv run scripts/create_collections.py
if ($LASTEXITCODE -ne 0) { Fail "collection creation failed." }
Ok "collections verified"

# ---------------------------------------------------------------------------
Step "Preparing your project registry"
if (-not (Test-Path "$Root\config\projects.yaml")) {
    Copy-Item "$Root\config\projects.example.yaml" "$Root\config\projects.yaml"
    Ok "created config\projects.yaml -- add your own projects there after setup"
}
else {
    Ok "config\projects.yaml already exists -- keeping yours"
}

# ---------------------------------------------------------------------------
Step "First index + background automation"
Info "indexing rememory itself (proves the whole pipeline end to end)..."
& $Uv run -m indexer.cli index --project rememory
if ($LASTEXITCODE -ne 0) { Fail "indexing failed." }
Info "seeding example memories (skips quietly if already seeded)..."
& $Uv run scripts/seed_memories.py
Info "registering background tasks (sync every 30 min, backup daily 12:00)..."
schtasks /Create /TN "RememorySync" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\sync.ps1`"" /SC MINUTE /MO 30 /F | Out-Null
schtasks /Create /TN "RememoryBackup" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\backup.ps1`"" /SC DAILY /ST 12:00 /F | Out-Null
Ok "index built; RememorySync + RememoryBackup registered"

# ---------------------------------------------------------------------------
Step "Verifying the installation (test suite)"
& $Uv run tests/test_unit.py
if ($LASTEXITCODE -ne 0) { Fail "unit tests failed." }
& $Uv run tests/test_roundtrip.py
if ($LASTEXITCODE -ne 0) { Fail "end-to-end round trip failed." }
Ok "all verification tests passed"

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
& $Uv run scripts/connect.py
