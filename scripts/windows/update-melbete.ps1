$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. Resolve repository changes, then run this script again."
}

docker compose --profile public up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Melbete Bingo failed to rebuild after the update."
}
