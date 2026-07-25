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

try {
    Invoke-RestMethod 'http://127.0.0.1:6333/readyz' -TimeoutSec 3 | Out-Null
    Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null
}
catch {
    Write-Log "skipped: qdrant or ollama not reachable"
    exit 0
}

try {
    $out = & $Uv run --directory $Root -m indexer.cli sync 2>&1
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
