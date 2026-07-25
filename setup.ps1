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

$TotalSteps = 11
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
    # Two install paths: winget (preferred) with the official installer script
    # as fallback -- winget is missing or broken on plenty of machines
    # (older Windows 10, corporate images, fresh accounts).
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info "uv not found -- installing via winget..."
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements --disable-interactivity
        $env:Path += ";$env:LOCALAPPDATA\Microsoft\WinGet\Links"
        $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    }
    if (-not $Uv) {
        Info "installing uv via the official installer..."
        try {
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        } catch { Fail "Could not download the uv installer. Check your internet connection and re-run." }
        $env:Path += ";$env:USERPROFILE\.local\bin"
        $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if (-not $Uv -and (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")) { $Uv = "$env:USERPROFILE\.local\bin\uv.exe" }
    }
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
Step "Choosing ports (avoids clashes with anything already running)"
$portJson = & $Uv run --directory $Root python -c @"
import json,sys
sys.path.insert(0, r'$Root')
from indexer.runtime import pick_free_port, save_runtime, port_is_ours
http = pick_free_port(6333); grpc = pick_free_port(6334)
if http is None or grpc is None:
    print('{\"error\":\"no free port\"}')
else:
    save_runtime(qdrant_port=http, qdrant_grpc_port=grpc)
    print(json.dumps({'http':http,'grpc':grpc,'reused':port_is_ours(http)}))
"@
try { $ports = $portJson | ConvertFrom-Json } catch { $ports = $null }
if ($null -eq $ports -or $ports.error) { Fail "Could not find a free port in 6333-6353. Free one up and re-run." }
$env:REMEMORY_QDRANT_PORT = "$($ports.http)"
$env:REMEMORY_QDRANT_GRPC_PORT = "$($ports.grpc)"
if ($ports.http -eq 6333) { Ok "using the default port 6333" }
else { Ok "port 6333 was taken -- using $($ports.http) instead (saved to config/runtime.json)" }

# ---------------------------------------------------------------------------
Step "Starting the vector database (Qdrant in Docker, data stays in $Root\data)"
New-Item -ItemType Directory -Force "$Root\data\qdrant\storage", "$Root\data\qdrant\snapshots", "$Root\data\logs", "$Root\data\backups" | Out-Null
# `docker compose` (plugin) with `docker-compose` (standalone) as fallback --
# older Docker installs only have the hyphenated binary.
docker compose -f "$Root\docker\compose.yml" up -d
if ($LASTEXITCODE -ne 0) {
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        Info "falling back to docker-compose..."
        docker-compose -f "$Root\docker\compose.yml" up -d
    }
    if ($LASTEXITCODE -ne 0) { Fail "docker compose failed -- see output above." }
}
Info "waiting for Qdrant to become ready..."
$ready = $false
foreach ($i in 1..30) {
    try { Invoke-RestMethod "http://127.0.0.1:$($ports.http)/readyz" -TimeoutSec 2 | Out-Null; $ready = $true; break }
    catch { Start-Sleep 2 }
}
if (-not $ready) { Fail "Qdrant did not become ready. Check: docker logs rememory-qdrant" }
Ok "qdrant ready on 127.0.0.1:$($ports.http) (loopback only -- unreachable from the network)"

# ---------------------------------------------------------------------------
Step "Building the Python environment (uv sync -- pinned Python 3.12, locked deps)"
& $Uv sync
if ($LASTEXITCODE -ne 0) { Fail "uv sync failed." }
# The desktop app (tray + dashboard) is an optional extra: if its GUI
# dependencies fail on this machine, the core system must still install.
& $Uv sync --extra app
if ($LASTEXITCODE -eq 0) { Ok "environment ready (including the desktop app)" }
else { Ok "environment ready (desktop app extra unavailable -- CLI and MCP work fine)" }

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
$syncOk = ($LASTEXITCODE -eq 0)
schtasks /Create /TN "RememoryBackup" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\backup.ps1`"" /SC DAILY /ST 12:00 /F | Out-Null
if ($syncOk -and $LASTEXITCODE -eq 0) {
    Ok "index built; RememorySync + RememoryBackup registered"
}
else {
    # Non-fatal: some locked-down machines block Task Scheduler. rememory
    # still works -- the assistant's sync_index tool and manual `uv run -m
    # indexer.cli sync` cover freshness; only the automation is missing.
    Ok "index built"
    Write-Host "      [!] could not register scheduled tasks (blocked on this machine?)." -ForegroundColor Yellow
    Write-Host "      rememory still works; run 'uv run -m indexer.cli sync' occasionally," -ForegroundColor Yellow
    Write-Host "      or let the assistant call sync_index after it writes files." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
Step "Verifying the installation (test suite)"
& $Uv run tests/test_unit.py
if ($LASTEXITCODE -ne 0) { Fail "unit tests failed." }
& $Uv run tests/test_roundtrip.py
if ($LASTEXITCODE -ne 0) { Fail "end-to-end round trip failed." }
Ok "all verification tests passed"

# ---------------------------------------------------------------------------
Step "Creating Start-menu shortcuts (Start / Stop / Status -- no terminal needed)"
$MenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) "Programs\rememory"
New-Item -ItemType Directory -Force $MenuDir | Out-Null
$Shell = New-Object -ComObject WScript.Shell
$shortcuts = @(
    @{ Name = "Start rememory";  Script = "start-rememory.ps1" },
    @{ Name = "Stop rememory";   Script = "stop-rememory.ps1" },
    @{ Name = "rememory Status"; Script = "rememory-status.ps1" },
    @{ Name = "Repair rememory"; Script = "repair-rememory.ps1" }
)
# The main app shortcut launches the tray + dashboard (not a .ps1 wrapper, so
# no console window flashes when it starts).
$appLnk = $Shell.CreateShortcut((Join-Path $MenuDir "rememory.lnk"))
$appLnk.TargetPath = $Uv
$appLnk.Arguments = "run --extra app --directory `"$Root`" -m app.main"
$appLnk.WorkingDirectory = $Root
$appLnk.WindowStyle = 7
$appLnk.Description = "rememory - open the dashboard and tray controls"
$appLnk.Save()
foreach ($s in $shortcuts) {
    $lnk = $Shell.CreateShortcut((Join-Path $MenuDir "$($s.Name).lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\$($s.Script)`""
    $lnk.WorkingDirectory = $Root
    $lnk.Save()
}
Ok "Start-menu shortcuts created (rememory, Start, Stop, Status, Repair)"

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
& $Uv run scripts/connect.py
