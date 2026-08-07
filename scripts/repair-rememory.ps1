# "Repair rememory" -- double-click friendly (Start-menu shortcut points here).
#
# The repair strategy is setup itself: every setup step is idempotent and
# self-verifying, so re-running it rebuilds whatever is broken -- a stopped
# container, a deleted venv, missing collections, unregistered scheduled
# tasks, a half-finished update -- while leaving healthy pieces (and ALL of
# your data: memories, index, backups, project registry) untouched.
# This wrapper adds plain-language framing and a data-safety check first.

$Root = Split-Path $PSScriptRoot -Parent
$Host.UI.RawUI.WindowTitle = "Repair rememory"
Write-Host ""
Write-Host "  Repairing rememory..." -ForegroundColor Cyan
Write-Host "  This re-verifies and rebuilds every component. Your memories,"
Write-Host "  index and settings are not touched -- broken pieces are fixed,"
Write-Host "  healthy pieces are skipped."
Write-Host ""

# Data safety first: if the memory collection is reachable, snapshot it
# before doing anything else, so even a repair-gone-wrong loses nothing.
# The port comes from config\runtime.json (setup may have moved Qdrant off
# 6333), and the probe bypasses the system proxy like everything else does.
$QdrantPort = 6333
$RuntimeFile = Join-Path $Root 'config\runtime.json'
if (Test-Path $RuntimeFile) {
    try {
        $rt = Get-Content $RuntimeFile -Raw | ConvertFrom-Json
        if ($rt.qdrant_port) { $QdrantPort = [int]$rt.qdrant_port }
    } catch {}
}
if ($env:REMEMORY_QDRANT_PORT -match '^\d+$') { $QdrantPort = [int]$env:REMEMORY_QDRANT_PORT }
try {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$QdrantPort/readyz")
    $req.Timeout = 3000
    $req.Proxy = $null
    $req.GetResponse().Close()
    Write-Host "  Taking a safety backup of your memories first..." -ForegroundColor Cyan
    $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $Uv) { $Uv = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe" }
    & $Uv run --directory $Root scripts/export_memory.py 2>$null | Select-String "exported"
}
catch {
    Write-Host "  (database not reachable yet -- repair will bring it back)" -ForegroundColor Yellow
}

Write-Host ""
powershell -NoProfile -ExecutionPolicy Bypass -File "$Root\setup.ps1"
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "  Repair finished -- everything verified working." -ForegroundColor Green
}
else {
    Write-Host "  Repair stopped at a step that needs your attention (see above)." -ForegroundColor Yellow
    Write-Host "  Fix that one thing (usually: start Docker Desktop or Ollama) and run Repair again."
}
Write-Host ""
Read-Host "  Press Enter to close"
