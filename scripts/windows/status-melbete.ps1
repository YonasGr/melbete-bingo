$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

docker compose --profile public ps
try {
    Invoke-WebRequest -UseBasicParsing http://localhost:8001/health | Select-Object -ExpandProperty Content
} catch {
    Write-Warning "The API health check failed: $($_.Exception.Message)"
}
