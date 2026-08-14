[CmdletBinding()]
param(
    [string]$ServiceName = 'AmneziaWGTunnel$AmneziaVPN'
)

$ErrorActionPreference = "Stop"
$Principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script once from PowerShell opened with Run as administrator."
}

$Service = Get-Service -Name $ServiceName -ErrorAction Stop
Set-Service -Name $ServiceName -StartupType Automatic
if ($Service.Status -ne "Running") {
    Start-Service -Name $ServiceName
}
$Verified = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" |
    Select-Object Name, State, StartMode
if ($Verified.StartMode -ne "Auto" -or $Verified.State -ne "Running") {
    throw "Amnezia tunnel service autostart verification failed."
}
$Verified
