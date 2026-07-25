# Convenience wrapper around docker compose so you never have to remember the
# path or the flags. Usage from anywhere:
#
#   D:\memory-system\scripts\qdrant.ps1 up
#   D:\memory-system\scripts\qdrant.ps1 status
#   D:\memory-system\scripts\qdrant.ps1 logs
#   D:\memory-system\scripts\qdrant.ps1 down
#   D:\memory-system\scripts\qdrant.ps1 snapshot

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('up', 'down', 'status', 'logs', 'snapshot')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$ComposeDir = Join-Path $PSScriptRoot '..\docker' | Resolve-Path

switch ($Action) {
    'up' {
        docker compose -f "$ComposeDir\compose.yml" up -d
    }
    'down' {
        # Stops and removes the container. Data on D: is untouched — it lives in
        # a bind mount, not in the container's writable layer.
        docker compose -f "$ComposeDir\compose.yml" down
    }
    'status' {
        docker compose -f "$ComposeDir\compose.yml" ps
        Write-Host "`n--- API ---"
        try {
            Invoke-RestMethod 'http://127.0.0.1:6333/' | Format-List
            Invoke-RestMethod 'http://127.0.0.1:6333/collections' |
                Select-Object -ExpandProperty result |
                Select-Object -ExpandProperty collections
        }
        catch {
            Write-Warning "Qdrant is not answering on 127.0.0.1:6333 - is it started?"
        }
    }
    'logs' {
        docker compose -f "$ComposeDir\compose.yml" logs -f --tail 100
    }
    'snapshot' {
        # A full-storage snapshot: a point-in-time backup of every collection.
        # Lands in D:\memory-system\data\qdrant\snapshots.
        Invoke-RestMethod -Method Post 'http://127.0.0.1:6333/snapshots' | Format-List
    }
}
