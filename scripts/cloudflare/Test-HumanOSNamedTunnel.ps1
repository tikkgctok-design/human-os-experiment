[CmdletBinding()]
param(
    [string]$PrivateDirectory = "private/cloudflare",
    [string]$Database = "private/human_os.db",
    [string]$BridgeEnvironment = "private/bridge.env",
    [string]$ToolEnvironment = "private/tool.env",
    [string]$ClientBindAddress,
    [switch]$FullValidation
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Cloudflare.Common.ps1")

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $ProjectRoot
$StatePath = Join-Path $ProjectRoot "$PrivateDirectory/state.json"
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    throw "Named tunnel state is missing."
}
$State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-HumanOSHostname -Hostname $State.hostname
$ToolToken = Get-HumanOSEnvValue -Path $ToolEnvironment -Name "HUMAN_OS_TOOL_TOKEN"
$Headers = @{ Authorization = "Bearer $ToolToken" }
if ($ClientBindAddress) {
    $ParsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($ClientBindAddress, [ref]$ParsedAddress) -or
        $ParsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "ClientBindAddress must be an IPv4 address."
    }
    $CurlConfig = Join-Path $ProjectRoot "$PrivateDirectory/health-curl.tmp"
    $CurlOutput = Join-Path $ProjectRoot "$PrivateDirectory/health-response.tmp"
    $CurlOutputForConfig = $CurlOutput.Replace("\", "/")
    try {
        @(
            "silent", "show-error", "max-time = 30",
            "interface = `"$ClientBindAddress`"",
            "url = `"https://$($State.hostname)/bridge/health`"",
            "header = `"Authorization: Bearer $ToolToken`"",
            "header = `"User-Agent: HumanOS-Validation/1.0`"",
            "output = `"$CurlOutputForConfig`"", "write-out = `"%{http_code}`""
        ) | Set-Content -LiteralPath $CurlConfig -Encoding ASCII
        $StatusCode = & curl.exe --config $CurlConfig
        if ($LASTEXITCODE -ne 0 -or $StatusCode -ne "200") {
            throw "Human OS production health request failed."
        }
        $Health = Get-Content -LiteralPath $CurlOutput -Raw -Encoding UTF8 | ConvertFrom-Json
    } finally {
        Remove-Item -LiteralPath $CurlConfig, $CurlOutput -Force -ErrorAction SilentlyContinue
    }
} else {
    $Health = Invoke-RestMethod -Method Get -Uri "https://$($State.hostname)/bridge/health" -Headers $Headers -TimeoutSec 30
}
if (-not $Health.ok -or -not $Health.bridge_alive -or -not $Health.local_api_reachable -or $Health.bridge_opens_database) {
    throw "Human OS production health check failed."
}
Write-Host "HTTPS health check passed for https://$($State.hostname)"

if ($FullValidation) {
    $BridgeToken = Get-HumanOSEnvValue -Path $BridgeEnvironment -Name "HUMAN_OS_BRIDGE_TOKEN"
    $env:HUMAN_OS_TOOL_TOKEN = $ToolToken
    $env:HUMAN_OS_BRIDGE_TOKEN = $BridgeToken
    try {
        $ValidationArguments = @(
            "-m", "human_os.tool_validation",
            "--db", $Database,
            "--tool-url", "https://$($State.hostname)",
            "--bridge-url", "http://127.0.0.1:8787",
            "--output", "private/cloudflare/production-validation-report.json"
        )
        if ($ClientBindAddress) {
            $ValidationArguments += @("--source-address", $ClientBindAddress)
        }
        & .venv\Scripts\python.exe @ValidationArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Production E2E validation failed."
        }
    } finally {
        Remove-Item Env:HUMAN_OS_TOOL_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:HUMAN_OS_BRIDGE_TOKEN -ErrorAction SilentlyContinue
    }
}
