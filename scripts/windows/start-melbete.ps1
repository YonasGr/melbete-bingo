$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Melbete Bingo failed to start. Check Docker Desktop and .env."
}

Write-Host "Melbete Bingo is running at http://localhost:8001"
Write-Host "Health: http://localhost:8001/health"
