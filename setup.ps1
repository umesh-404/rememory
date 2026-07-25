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
#
#     -AllowCloudFolder   install into a OneDrive/Dropbox/Drive folder anyway

param([switch]$AllowCloudFolder)

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

# Run a native command, discard ALL of its output, keep only $LASTEXITCODE.
#
# Why this exists: in Windows PowerShell 5.1, redirecting a native exe's stderr
# wraps every stderr line in an ErrorRecord (NativeCommandError). With
# $ErrorActionPreference = 'Stop' that turns a purely informational message into
# a fatal error -- e.g. Docker Desktop on some machines prints
# "WARNING: No blkio throttle.read_bps_device support" from `docker info`, which
# killed setup at step 1 even though Docker was running perfectly.
# Dropping to 'Continue' for the duration makes the wrapped stderr harmless;
# the exit code, which is what we actually test, is unaffected.
# Run a native command but give up after $TimeoutSec, killing it and its
# children. Used for steps that are optional: a dependency that compiles from
# source can stall on a slow disk or a wedged network, and an optional extra
# must never be able to hold the whole installer hostage.
# Returns the exit code, or -1 if it timed out.
function Invoke-WithTimeout([string]$File, [string[]]$Arguments, [int]$TimeoutSec) {
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -NoNewWindow -PassThru
    # Touching .Handle forces PowerShell to cache the process handle. Without
    # this, .ExitCode reads back empty once the process has exited, and a
    # successful install would be misreported as a failed one.
    $null = $p.Handle
    if ($p.WaitForExit($TimeoutSec * 1000)) { return $p.ExitCode }
    # taskkill /T reaches the child build processes uv spawned, which a plain
    # Stop-Process on the parent would orphan.
    Invoke-Quiet { taskkill /PID $p.Id /T /F }
    return -1
}

function Invoke-Quiet([scriptblock]$Command) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>&1 | Out-Null }
    finally { $ErrorActionPreference = $prev }
}

Write-Host "rememory setup -- local, private development memory (Qdrant + Ollama + MCP)"
Write-Host "Everything runs on this machine. Nothing is sent anywhere."

# ---------------------------------------------------------------------------
# Refuse to install inside a cloud-synced folder.
#
# `uv sync` writes tens of thousands of small files into .venv, and some
# dependencies (proxy-tools, required by the desktop app's pywebview) ship
# only as a source archive, so they compile locally in a throwaway build
# environment. A file-sync client intercepts every one of those writes to
# upload it. The install does not fail -- it crawls, appearing to hang
# forever on whichever package it happened to reach.
#
# The database and index are also unsafe to sync: Qdrant memory-maps its
# storage, and a sync client copying those files mid-write corrupts them.
$cloudRoots = @()
foreach ($var in 'OneDrive', 'OneDriveConsumer', 'OneDriveCommercial') {
    $val = [Environment]::GetEnvironmentVariable($var)
    if ($val) { $cloudRoots += $val }
}
$cloudNames = 'OneDrive', 'Dropbox', 'Google Drive', 'GoogleDrive', 'iCloudDrive', 'Creative Cloud Files'
$syncedBy = $null
foreach ($c in $cloudRoots) {
    if ($Root.StartsWith($c, [StringComparison]::OrdinalIgnoreCase)) { $syncedBy = $c; break }
}
if (-not $syncedBy) {
    foreach ($n in $cloudNames) {
        if ($Root -like "*\$n\*" -or $Root -like "*\$n") { $syncedBy = $n; break }
    }
}
if ($syncedBy -and -not $AllowCloudFolder) {
    Write-Host ""
    Write-Host "CANNOT INSTALL HERE -- this folder is synced to the cloud" -ForegroundColor Red
    Write-Host "  $Root" -ForegroundColor Red
    Write-Host "  (matched: $syncedBy)"
    Write-Host ""
    Write-Host "  Installing here makes setup crawl to a near-halt, because every file"
    Write-Host "  written into .venv gets uploaded, and it risks corrupting the vector"
    Write-Host "  database, which is memory-mapped and must not be synced."
    Write-Host ""
    Write-Host "  Move this folder somewhere local and re-run, for example:" -ForegroundColor Cyan
    Write-Host "      C:\rememory"
    Write-Host ""
    Write-Host "  If setup is running right now, press Ctrl+C first, then delete the"
    Write-Host "  partly-built .venv folder before moving it."
    Write-Host ""
    Write-Host "  To install here anyway (not recommended):"
    Write-Host "      powershell -ExecutionPolicy Bypass -File setup.ps1 -AllowCloudFolder"
    exit 1
}
if ($syncedBy) {
    Write-Host "  WARNING: installing into a synced folder ($syncedBy) -- expect a slow install." -ForegroundColor Yellow
}
if ($Root -match '\s') {
    Write-Host "  NOTE: this path contains spaces; that is supported, but a path without" -ForegroundColor Yellow
    Write-Host "        them (C:\rememory) avoids quoting problems in some MCP clients." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
Step "Checking prerequisites (Docker, Ollama)"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker not found. Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
}
Invoke-Quiet { docker info }
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
from indexer.runtime import pick_free_port, save_runtime, port_is_ours, runtime
http = pick_free_port(6333)
# If our own Qdrant already holds the HTTP port, its gRPC port belongs to the
# same container -- probing it separately would see a busy non-HTTP port and
# needlessly move gRPC on every re-run.
grpc = runtime()['qdrant_grpc_port'] if (http and port_is_ours(http)) else pick_free_port(6334)
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
# It is also the slowest thing here -- pywebview pulls in proxy-tools, which
# is source-only and has to compile -- so it gets a hard time limit rather
# than being allowed to stall the installer indefinitely.
Info "adding the optional desktop app (compiles one small package; up to 10 min)..."
$appCode = Invoke-WithTimeout $Uv @('sync', '--extra', 'app') 600
if ($appCode -eq 0) {
    Ok "environment ready (including the desktop app)"
} elseif ($appCode -eq -1) {
    Ok "environment ready (desktop app timed out and was skipped -- CLI and MCP work fine)"
    Info "retry it later with:  uv sync --extra app"
} else {
    Ok "environment ready (desktop app extra unavailable -- CLI and MCP work fine)"
}

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
Step "Background automation"
# rememory deliberately does not index its own source. It exists to remember
# YOUR projects; indexing the tool itself only lengthens setup and puts
# rememory's internals into your search results. The end-to-end pipeline is
# still proven -- by the round-trip test in the next step, which embeds,
# upserts, searches and cleans up without touching this repo.
Info "seeding example memories (skips quietly if already seeded)..."
& $Uv run scripts/seed_memories.py
Info "registering background tasks (sync every 30 min, backup daily 12:00)..."
schtasks /Create /TN "RememorySync" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\sync.ps1`"" /SC MINUTE /MO 30 /F | Out-Null
$syncOk = ($LASTEXITCODE -eq 0)
schtasks /Create /TN "RememoryBackup" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\scripts\backup.ps1`"" /SC DAILY /ST 12:00 /F | Out-Null
if ($syncOk -and $LASTEXITCODE -eq 0) {
    Ok "RememorySync + RememoryBackup registered"
}
else {
    # Non-fatal: some locked-down machines block Task Scheduler. rememory
    # still works -- the assistant's sync_index tool and manual `uv run -m
    # indexer.cli sync` cover freshness; only the automation is missing.
    Ok "memories seeded"
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
Step "Creating the Start-menu shortcut"
# ONE shortcut: the app itself. Start / Stop / Status / Repair all live
# inside it (tray menu and dashboard), so separate script shortcuts would
# just be clutter wearing a PowerShell icon.
$MenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) "Programs"
New-Item -ItemType Directory -Force $MenuDir | Out-Null

# Remove shortcuts created by older versions of this installer.
$legacy = Join-Path $MenuDir "rememory"
if (Test-Path $legacy) { Remove-Item $legacy -Recurse -Force -ErrorAction SilentlyContinue }

# A real icon, generated from the same code that draws the tray icon --
# otherwise the shortcut inherits uv.exe's icon.
$IconPath = Join-Path (Join-Path $Root "data") "rememory.ico"
Invoke-Quiet { & $Uv run --extra app --directory $Root python -c "from app.icon import write_ico; write_ico(r'$IconPath')" }

# Launch through the venv's pythonw.exe, NOT uv.exe.
#
# uv.exe is a console-subsystem program, so Windows creates a console window
# for it no matter what. WindowStyle=7 only minimises that console -- it still
# exists, and it can surface as a black window titled "rememory" that looks
# like a broken dashboard. Worse, if the user clicks in it, Windows console
# QuickEdit puts it in selection mode (the title becomes "Select rememory")
# and the app freezes the moment it next writes to stdout.
#
# pythonw.exe is a GUI-subsystem binary: no console is ever created, so none
# of that can happen. The venv already exists by this step, and skipping
# `uv run` also makes the app start noticeably faster.
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Shell = New-Object -ComObject WScript.Shell
$lnk = $Shell.CreateShortcut((Join-Path $MenuDir "rememory.lnk"))
if (Test-Path $Pythonw) {
    $lnk.TargetPath = $Pythonw
    $lnk.Arguments = "-m app.main"
} else {
    # Desktop extra unavailable or venv missing: fall back to uv so the
    # shortcut still does something sensible.
    $lnk.TargetPath = $Uv
    $lnk.Arguments = "run --extra app --directory `"$Root`" -m app.main"
}
$lnk.WorkingDirectory = $Root
$lnk.WindowStyle = 7
$lnk.Description = "rememory - local memory for AI coding assistants"
if (Test-Path $IconPath) { $lnk.IconLocation = $IconPath }
$lnk.Save()
if (Test-Path $Pythonw) { Ok "Start-menu shortcut created (search: rememory)" }
else { Ok "Start-menu shortcut created (via uv -- desktop extra was not installed)" }
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
& $Uv run scripts/connect.py
