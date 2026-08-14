[CmdletBinding()]
param(
    [string]$Database = "private/human_os.db",
    [string]$BridgeEnvironment = "private/bridge.env",
    [string]$ToolEnvironment = "private/tool.env",
    [string]$MobileEnvironment = "private/mobile.env",
    [string]$PrivateRuntimeDirectory = "private/runtime"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual environment is missing: $Python"
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m human_os.windows_runtime `
    --db $Database `
    --bridge-env $BridgeEnvironment `
    --tool-env $ToolEnvironment `
    --mobile-env $MobileEnvironment `
    --private-dir $PrivateRuntimeDirectory
exit $LASTEXITCODE
