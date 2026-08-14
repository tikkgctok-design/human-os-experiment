[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d{1,3}(?:\.\d{1,3}){3}$')]
    [string]$Gateway,

    [Parameter(Mandatory)]
    [ValidateRange(1, 65535)]
    [int]$InterfaceIndex,

    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell window (Run as administrator)."
}

$ParsedGateway = $null
if (-not [Net.IPAddress]::TryParse($Gateway, [ref]$ParsedGateway) -or
    $ParsedGateway.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw "Gateway must be an IPv4 address."
}

# Cloudflare's documented region1.v2.argotunnel.com and
# region2.v2.argotunnel.com IPv4 edge ranges. Only connector traffic to these
# two narrow networks bypasses a higher-priority VPN default route.
$Destinations = @("198.41.192.0", "198.41.200.0")
$Mask = "255.255.255.0"
foreach ($Destination in $Destinations) {
    if ($Remove) {
        if ($PSCmdlet.ShouldProcess("$Destination/24", "remove persistent route")) {
            & route.exe DELETE $Destination MASK $Mask $Gateway IF $InterfaceIndex | Out-Null
            if ($LASTEXITCODE -notin @(0, 1168)) {
                throw "Failed to remove Cloudflare edge route $Destination/24."
            }
        }
    } elseif ($PSCmdlet.ShouldProcess("$Destination/24", "add persistent route")) {
        & route.exe -p ADD $Destination MASK $Mask $Gateway METRIC 5 IF $InterfaceIndex | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to add Cloudflare edge route $Destination/24."
        }
    }
}

Write-Host "Cloudflare Tunnel edge routes configured. No inbound route was added."
