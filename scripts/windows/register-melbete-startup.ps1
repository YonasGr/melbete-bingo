$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $repo "scripts\windows\start-melbete.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "Melbete Bingo" -Action $action -Trigger $trigger -Description "Starts the local Melbete Bingo Docker stack after Windows sign-in." -Force | Out-Null

Write-Host "Startup task registered. Docker Desktop must be configured to start with Windows."
