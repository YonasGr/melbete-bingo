$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

docker compose --profile public stop
if ($LASTEXITCODE -ne 0) {
    throw "Melbete Bingo could not be stopped."
}
