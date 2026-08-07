# Incremental sync of every registered project. Registered as scheduled task
# `RememorySync` by setup.ps1, and safe to run by hand any time.
#
# Exits quietly if the stack is not up (machine just booted, Docker still
# starting): a sync that cannot reach Qdrant should be a no-op, not an error
# notification. The next scheduled run catches up.
#
# All paths derive from the script's own location, so the repo can live
# anywhere on any machine.

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) { $Uv = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe" }
$LogDir = Join-Path $Root 'data\logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir 'sync.log'

function Write-Log([string]$msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $Log -Encoding utf8
}

# Ports come from config\runtime.json (written by setup), REMEMORY_* env vars
# taking precedence -- mirroring indexer/runtime.py. Hardcoding 6333 here made
# this script permanently log "skipped" on any machine where setup had moved
# Qdrant to another port: a silently dead sync.
$QdrantPort = 6333; $OllamaPort = 11434
$RuntimeFile = Join-Path $Root 'config\runtime.json'
if (Test-Path $RuntimeFile) {
    try {
        $rt = Get-Content $RuntimeFile -Raw | ConvertFrom-Json
        if ($rt.qdrant_port) { $QdrantPort = [int]$rt.qdrant_port }
        if ($rt.ollama_port) { $OllamaPort = [int]$rt.ollama_port }
    } catch {}
}
if ($env:REMEMORY_QDRANT_PORT -match '^\d+$') { $QdrantPort = [int]$env:REMEMORY_QDRANT_PORT }
if ($env:REMEMORY_OLLAMA_PORT -match '^\d+$') { $OllamaPort = [int]$env:REMEMORY_OLLAMA_PORT }

function Test-Local([string]$url) {
    # HttpWebRequest with Proxy = $null, not Invoke-RestMethod: the latter
    # uses the system proxy, and a corporate proxy swallowing loopback made
    # healthy services probe as offline (same fix as setup.ps1).
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Timeout = 3000
        $req.Proxy = $null
        $req.GetResponse().Close()
        return $true
    } catch { return $false }
}

if (-not (Test-Local "http://127.0.0.1:$QdrantPort/readyz") -or
    -not (Test-Local "http://127.0.0.1:$OllamaPort/api/tags")) {
    Write-Log "skipped: qdrant or ollama not reachable"
    exit 0
}

try {
    # 'Continue' while the native command runs: PowerShell 5.1 wraps a native
    # exe's stderr lines in ErrorRecords when redirected, and under 'Stop' a
    # harmless progress line from uv would be reported as a failed sync.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Uv run --directory $Root -m indexer.cli sync 2>&1 }
    finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "indexer.cli sync exited $LASTEXITCODE" }
    $summary = ($out | Where-Object { $_ -match 'files ->|seen=' }) -join ' | '
    Write-Log "ok: $summary"
}
catch {
    Write-Log "FAILED: $_"
    exit 1
}

if ((Get-Item $Log).Length -gt 1MB) {
    Move-Item $Log "$Log.1" -Force
}
