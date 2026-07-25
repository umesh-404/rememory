# "rememory Status" -- double-click friendly health overview in plain language.

$Root = Split-Path $PSScriptRoot -Parent
$Host.UI.RawUI.WindowTitle = "rememory Status"
Write-Host ""
Write-Host "  rememory status" -ForegroundColor Cyan
Write-Host "  ---------------"

function Row([string]$name, [bool]$up, [string]$fix) {
    if ($up) { Write-Host ("  [OK]  {0}" -f $name) -ForegroundColor Green }
    else { Write-Host ("  [--]  {0}  -> {1}" -f $name, $fix) -ForegroundColor Yellow }
}

docker info 2>$null | Out-Null
$dockerUp = ($LASTEXITCODE -eq 0)
Row "Docker" $dockerUp "start Docker Desktop"

$qdrantUp = $false
try { Invoke-RestMethod http://127.0.0.1:6333/readyz -TimeoutSec 2 | Out-Null; $qdrantUp = $true } catch {}
Row "Memory database (Qdrant)" $qdrantUp "run 'Start rememory'"

$ollamaUp = $false
try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 2 | Out-Null; $ollamaUp = $true } catch {}
Row "AI models (Ollama)" $ollamaUp "start the Ollama app"

$sync = schtasks /Query /TN "RememorySync" 2>$null
Row "Background sync (every 30 min)" ($LASTEXITCODE -eq 0) "re-run setup.ps1"
$bk = schtasks /Query /TN "RememoryBackup" 2>$null
Row "Daily backup (12:00)" ($LASTEXITCODE -eq 0) "re-run setup.ps1"

if ($qdrantUp) {
    Write-Host ""
    Write-Host "  Indexed content:" -ForegroundColor Cyan
    try {
        foreach ($coll in @("code", "docs", "memory")) {
            $r = Invoke-RestMethod "http://127.0.0.1:6333/collections/$coll" -TimeoutSec 3
            Write-Host ("    {0,-8} {1} chunks" -f $coll, $r.result.points_count)
        }
    } catch { Write-Host "    (could not read collection stats)" }
}

Write-Host ""
if ($dockerUp -and $qdrantUp -and $ollamaUp) {
    Write-Host "  Everything is running. Your assistant's memory is fully available." -ForegroundColor Green
} else {
    Write-Host "  Something is down -- follow the arrows above, or run 'Start rememory'." -ForegroundColor Yellow
}
Write-Host ""
Read-Host "  Press Enter to close"
