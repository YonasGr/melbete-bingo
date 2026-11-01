$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

$envFile = Join-Path (Get-Location) ".env"
if (-not (Test-Path $envFile) -or -not (Select-String -Path $envFile -Pattern "^\s*CLOUDFLARE_TUNNEL_TOKEN\s*=\s*\S+" -Quiet)) {
    throw "Set CLOUDFLARE_TUNNEL_TOKEN in .env before starting the public profile."
}

docker compose --profile public up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Melbete Bingo failed to start. Check Docker Desktop, .env, and CLOUDFLARE_TUNNEL_TOKEN."
}

Write-Host "Melbete Bingo is running with the Cloudflare Tunnel profile."
Write-Host "Use the hostname configured for the tunnel as PUBLIC_BASE_URL in .env."
