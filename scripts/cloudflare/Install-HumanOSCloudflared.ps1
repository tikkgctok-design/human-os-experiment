[CmdletBinding()]
param([switch]$Install)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Cloudflare.Common.ps1")

try {
    $Cloudflared = Get-HumanOSCloudflared
    & $Cloudflared --version
    exit $LASTEXITCODE
} catch {
    if (-not $Install) {
        throw
    }
}

$Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $Winget) {
    throw "winget is unavailable. Install cloudflared from the official Cloudflare download page."
}

& $Winget.Source install --id Cloudflare.cloudflared --exact --accept-package-agreements --accept-source-agreements
if ($LASTEXITCODE -ne 0) {
    throw "cloudflared installation failed with exit code $LASTEXITCODE"
}

$Cloudflared = Get-HumanOSCloudflared
& $Cloudflared --version
exit $LASTEXITCODE
