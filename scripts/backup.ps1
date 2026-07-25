# Daily backup of the `memory` collection (the only irreplaceable data).
# Registered as scheduled task `RememoryBackup` by setup.ps1; safe by hand.
# Exports payload-only JSON to data\backups (30 kept); restore with
# scripts/import_memory.py. Paths derive from the script location.

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) { $Uv = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe" }
$Log = Join-Path $Root 'data\logs\backup.log'
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

try {
    Invoke-RestMethod 'http://127.0.0.1:6333/readyz' -TimeoutSec 3 | Out-Null
}
catch {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  skipped: qdrant not reachable" |
        Add-Content $Log -Encoding utf8
    exit 0
}

try {
    # See sync.ps1: redirected native stderr becomes a terminating error under
    # $ErrorActionPreference = 'Stop' in PowerShell 5.1.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Uv run --directory $Root scripts/export_memory.py 2>&1 |
            Where-Object { $_ -match 'exported|pruned' }
    }
    finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "export_memory.py exited $LASTEXITCODE" }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $($out -join ' | ')" |
        Add-Content $Log -Encoding utf8
}
catch {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  FAILED: $_" | Add-Content $Log -Encoding utf8
    exit 1
}
