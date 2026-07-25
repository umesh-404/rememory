# "Start rememory" -- double-click friendly (a Start-menu shortcut points here).
# Brings the whole stack up in plain language, no terminal knowledge needed.

$Root = Split-Path $PSScriptRoot -Parent
$Host.UI.RawUI.WindowTitle = "Start rememory"
Write-Host ""
Write-Host "  Starting rememory..." -ForegroundColor Cyan
Write-Host ""

function Done([string]$m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Note([string]$m) { Write-Host "  $m" }
function Bad([string]$m) { Write-Host "  [!] $m" -ForegroundColor Yellow }

# 1. Docker daemon (launch Docker Desktop if needed -- this is the deliberate,
#    user-initiated context where doing so is appropriate).
docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $dd = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
        Note "Docker is not running -- starting Docker Desktop (can take a minute)..."
        Start-Process $dd
        foreach ($i in 1..60) {
            Start-Sleep 3
            docker info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
        }
    }
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Bad "Docker did not come up. Start Docker Desktop yourself, then run this again."
        Read-Host "  Press Enter to close"; exit 1
    }
}
Done "Docker running"

# 2. Qdrant
docker compose -f "$Root\docker\compose.yml" up -d 2>$null | Out-Null
$ready = $false
foreach ($i in 1..30) {
    try { Invoke-RestMethod http://127.0.0.1:6333/readyz -TimeoutSec 2 | Out-Null; $ready = $true; break }
    catch { Start-Sleep 2 }
}
if ($ready) { Done "memory database running" } else { Bad "database did not become ready (docker logs rememory-qdrant)" }

# 3. Ollama
try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 3 | Out-Null; Done "AI models available" }
catch {
    $ol = "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
    if (Test-Path $ol) { Start-Process $ol; Start-Sleep 5 }
    try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 3 | Out-Null; Done "AI models available" }
    catch { Bad "Ollama is not running -- start the Ollama app for search to work." }
}

Write-Host ""
Write-Host "  rememory is up. Your AI assistant's memory tools will work in any session." -ForegroundColor Green
Write-Host ""
Read-Host "  Press Enter to close"
