[CmdletBinding()]
param(
    [string]$PrivateDirectory = "private/cloudflare",
    [string]$EdgeBindAddress,
    [ValidateSet("http2", "quic")]
    [string]$Protocol = "http2",
    [ValidateRange(1, 4)]
    [int]$HAConnections = 4
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Cloudflare.Common.ps1")

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $ProjectRoot
$ResolvedPrivateDirectory = [System.IO.Path]::GetFullPath(
    (Join-Path $ProjectRoot $PrivateDirectory)
)
$ConfigPath = Join-Path $ResolvedPrivateDirectory "config.yml"
$StatePath = Join-Path $ResolvedPrivateDirectory "state.json"
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    throw "Named tunnel is not initialized. Run Initialize-HumanOSNamedTunnel.ps1 first."
}

$State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
$Cloudflared = Get-HumanOSCloudflared
& $Cloudflared tunnel --config $ConfigPath ingress validate
if ($LASTEXITCODE -ne 0) {
    throw "Cloudflare ingress validation failed."
}

Write-Host "Starting named tunnel '$($State.tunnel_name)' for https://$($State.hostname)"
# HTTP/2 is explicit for networks where outbound UDP/QUIC is unavailable. If a VPN
# captures the default route, bind the connector to a trusted physical-interface IP.
$ConnectorLog = Join-Path $ResolvedPrivateDirectory "connector.log"
$Arguments = @(
    "tunnel", "--protocol", $Protocol, "--edge-ip-version", "4",
    "--ha-connections", [string]$HAConnections,
    "--loglevel", "info", "--logfile", $ConnectorLog
)
if ($EdgeBindAddress) {
    $ParsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($EdgeBindAddress, [ref]$ParsedAddress) -or
        $ParsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "EdgeBindAddress must be an IPv4 address assigned to this host."
    }
    $LocalAddresses = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Select-Object -ExpandProperty IPAddress
    )
    if ($EdgeBindAddress -notin $LocalAddresses) {
        throw "EdgeBindAddress is not assigned to this host."
    }
    $Arguments += @("--edge-bind-address", $EdgeBindAddress)
}
$Arguments += @("--config", $ConfigPath, "run", [string]$State.tunnel_id)
& $Cloudflared @Arguments
exit $LASTEXITCODE
