# "Stop rememory" -- double-click friendly (a Start-menu shortcut points here).
# Stops rememory's own pieces WITHOUT touching Docker Desktop or Ollama --
# those are shared applications the user may be using for other things.

$Root = Split-Path $PSScriptRoot -Parent
$Host.UI.RawUI.WindowTitle = "Stop rememory"
Write-Host ""
Write-Host "  Stopping rememory..." -ForegroundColor Cyan
Write-Host ""

docker compose -f "$Root\docker\compose.yml" stop 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Host "  [OK] memory database stopped" -ForegroundColor Green }
else { Write-Host "  [!] could not stop the database (is Docker running?)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  rememory is stopped. Memory tools will report the database as offline"
Write-Host "  until you start it again ('Start rememory', or it self-starts with Docker)."
Write-Host ""
Write-Host "  Note: Docker Desktop and Ollama were left running -- other apps may use them."
Write-Host ""
Read-Host "  Press Enter to close"
