[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TaskName = "Human OS Production Supervisor"
$LegacyTaskNames = "Human OS Read-Only Runtime", "Human OS Cloudflare Named Tunnel"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($Legacy in $LegacyTaskNames) {
        Unregister-ScheduledTask -TaskName $Legacy -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "Human OS production scheduled task removed."
    exit 0
}

$TunnelState = Join-Path $ProjectRoot "private\cloudflare\state.json"
if (-not (Test-Path -LiteralPath $TunnelState -PathType Leaf)) {
    throw "Initialize the named tunnel before registering production tasks."
}

$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$SupervisorScript = Join-Path $ProjectRoot "scripts\cloudflare\Start-HumanOSProduction.ps1"
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$SupervisorScript`""
) -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Delay = "PT30S"
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RunOnlyIfNetworkAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

foreach ($Legacy in $LegacyTaskNames) {
    Unregister-ScheduledTask -TaskName $Legacy -Confirm:$false -ErrorAction SilentlyContinue
}
Register-ScheduledTask -TaskName $TaskName -Action $Action `
    -Trigger $Trigger -Settings $Settings -Principal $Principal `
    -Description "Ordered Human OS startup: Internet, AmneziaVPN, loopback APIs, Named Tunnel and HTTPS watchdog" -Force | Out-Null

Write-Host "Production supervisor task registered for the current Windows user."
Write-Host "No token or credential was placed in task arguments."
