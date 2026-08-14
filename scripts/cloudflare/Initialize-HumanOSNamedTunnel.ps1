[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Hostname,
    [string]$TunnelName = "human-os-tool"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Cloudflare.Common.ps1")

Assert-HumanOSHostname -Hostname $Hostname
if ($TunnelName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$') {
    throw "TunnelName contains unsupported characters."
}

$Cloudflared = Get-HumanOSCloudflared
$AccountCertificate = Join-Path $env:USERPROFILE ".cloudflared\cert.pem"
if (-not (Test-Path -LiteralPath $AccountCertificate -PathType Leaf)) {
    throw @"
Cloudflare login is required before tunnel creation.
Run this yourself and complete the browser authorization:
  & '$Cloudflared' tunnel login
Then rerun this script with the same -Hostname and -TunnelName.
Do not copy cert.pem, tunnel credentials, tokens, or dashboard secrets into chat.
"@
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PrivateDirectory = Join-Path $ProjectRoot "private\cloudflare"
New-Item -ItemType Directory -Path $PrivateDirectory -Force | Out-Null

$TunnelListJson = (& $Cloudflared tunnel list --output json) -join "`n"
$Tunnels = @($TunnelListJson | ConvertFrom-Json) | Where-Object { $null -ne $_ }
$Tunnel = $Tunnels | Where-Object {
    $_.PSObject.Properties.Name -contains "name" -and $_.name -eq $TunnelName
} | Select-Object -First 1
if (-not $Tunnel) {
    & $Cloudflared tunnel create $TunnelName
    if ($LASTEXITCODE -ne 0) {
        throw "Named tunnel creation failed with exit code $LASTEXITCODE"
    }
    $TunnelListJson = (& $Cloudflared tunnel list --output json) -join "`n"
    $Tunnels = @($TunnelListJson | ConvertFrom-Json) | Where-Object { $null -ne $_ }
    $Tunnel = $Tunnels | Where-Object {
        $_.PSObject.Properties.Name -contains "name" -and $_.name -eq $TunnelName
    } | Select-Object -First 1
}
if (-not $Tunnel -or -not $Tunnel.id) {
    throw "Could not resolve the UUID for named tunnel '$TunnelName'."
}

$TunnelId = [string]$Tunnel.id
$SourceCredentials = Join-Path $env:USERPROFILE ".cloudflared\$TunnelId.json"
$PrivateCredentials = Join-Path $PrivateDirectory "$TunnelId.json"
if (-not (Test-Path -LiteralPath $PrivateCredentials -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $SourceCredentials -PathType Leaf)) {
        throw "Tunnel credential file is missing for $TunnelId."
    }
    Copy-Item -LiteralPath $SourceCredentials -Destination $PrivateCredentials
}

$ConfigPath = Join-Path $PrivateDirectory "config.yml"
$StatePath = Join-Path $PrivateDirectory "state.json"
$NormalizedCredentials = $PrivateCredentials.Replace("\", "/")
$Config = @"
tunnel: $TunnelId
credentials-file: $NormalizedCredentials
ingress:
  - hostname: $Hostname
    path: ^/mobile(/.*)?$
    service: http://127.0.0.1:8990
  - hostname: $Hostname
    service: http://127.0.0.1:8899
  - service: http_status:404
"@
[System.IO.File]::WriteAllText($ConfigPath, $Config, [System.Text.UTF8Encoding]::new($false))

& $Cloudflared tunnel --config $ConfigPath ingress validate
if ($LASTEXITCODE -ne 0) {
    throw "Cloudflare ingress validation failed."
}

& $Cloudflared tunnel route dns $TunnelId $Hostname
if ($LASTEXITCODE -ne 0) {
    throw "Cloudflare DNS route creation failed. Verify that the domain is active in this account."
}

$State = [ordered]@{
    tunnel_name = $TunnelName
    tunnel_id = $TunnelId
    hostname = $Hostname
    config_path = $ConfigPath
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
}
[System.IO.File]::WriteAllText(
    $StatePath,
    (($State | ConvertTo-Json -Depth 3) + [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Named tunnel configuration ready for https://$Hostname"
Write-Host "Private config: $ConfigPath"
Write-Host "Next: run Start-HumanOSNamedTunnel.ps1"
