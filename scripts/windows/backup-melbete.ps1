$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

$backupDir = Join-Path (Get-Location) "backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$file = Join-Path $backupDir ("melbete_{0}.sql" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

docker compose exec -T db pg_dump -U bingo -d melbete_bingo --no-owner --no-privileges | Out-File -Encoding utf8 $file
if ($LASTEXITCODE -ne 0) {
    Remove-Item -Force $file -ErrorAction SilentlyContinue
    throw "Database backup failed."
}

Write-Host "Database backup written to $file"
