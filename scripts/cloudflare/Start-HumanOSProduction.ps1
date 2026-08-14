[CmdletBinding()]
param(
    [string]$VpnInterfacePattern = "AmneziaVPN*",
    [string]$PrivateDirectory = "private/production",
    [ValidateRange(5, 300)]
    [int]$PollSeconds = 30,
    [ValidateRange(1, 20)]
    [int]$HealthFailuresBeforeRestart = 3,
    [ValidateRange(15, 900)]
    [int]$MaximumBackoffSeconds = 300
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $ProjectRoot
$PrivatePath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $PrivateDirectory))
New-Item -ItemType Directory -Path $PrivatePath -Force | Out-Null
$LogPath = Join-Path $PrivatePath "production-supervisor.log"
$RuntimeScript = Join-Path $ProjectRoot "scripts\Start-HumanOSTool.ps1"
$TunnelScript = Join-Path $ProjectRoot "scripts\cloudflare\Start-HumanOSNamedTunnel.ps1"
$ToolEnvironment = Join-Path $ProjectRoot "private\tool.env"
$StatePath = Join-Path $ProjectRoot "private\cloudflare\state.json"

function Write-SupervisorLog([string]$Level, [string]$Message) {
    if ((Test-Path -LiteralPath $LogPath) -and (Get-Item $LogPath).Length -gt 5MB) {
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.previous" -Force
    }
    $Line = [ordered]@{
        timestamp = [DateTime]::UtcNow.ToString("o")
        level = $Level
        message = $Message
    } | ConvertTo-Json -Compress
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
}

function Get-PrivateEnvValue([string]$Path, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required private environment file is missing."
    }
    $Prefix = "$Name="
    $Line = Get-Content -LiteralPath $Path -Encoding UTF8 |
        Where-Object { $_.StartsWith($Prefix, [StringComparison]::Ordinal) } |
        Select-Object -First 1
    if (-not $Line) { throw "Required private environment value is missing: $Name" }
    return $Line.Substring($Prefix.Length)
}

function Test-LoopbackServices {
    foreach ($Port in 8765, 8787, 8899, 8990) {
        $Client = [Net.Sockets.TcpClient]::new()
        try {
            $Pending = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
            if (-not $Pending.AsyncWaitHandle.WaitOne(1000) -or -not $Client.Connected) {
                return $false
            }
            $Client.EndConnect($Pending)
        } catch { return $false } finally { $Client.Dispose() }
    }
    return $true
}

function Test-InternetReady {
    try {
        [void][Net.Dns]::GetHostAddresses("region1.v2.argotunnel.com")
        return $true
    } catch { return $false }
}

function Get-ReadyVpnAddress {
    $Adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like $VpnInterfacePattern -and $_.Status -eq "Up" })
    foreach ($Adapter in $Adapters) {
        $Address = Get-NetIPAddress -InterfaceIndex $Adapter.ifIndex -AddressFamily IPv4 `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($Address) { return [string]$Address }
    }
    return $null
}

function Test-PublicHealth([string]$Token) {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $false }
    try {
        $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$State.hostname -notmatch '^[a-z0-9.-]+$') { return $false }
        $Response = Invoke-WebRequest -UseBasicParsing -Method Get `
            -Uri "https://$($State.hostname)/bridge/health" `
            -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 20
        return [int]$Response.StatusCode -eq 200
    } catch { return $false }
}

function Start-HiddenScript([string]$Script, [string]$StdoutName, [string]$StderrName) {
    return Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Script) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $PrivatePath $StdoutName) `
        -RedirectStandardError (Join-Path $PrivatePath $StderrName)
}

$Mutex = [Threading.Mutex]::new($false, "Local\HumanOSProductionSupervisor")
$OwnsMutex = $false
try {
    $OwnsMutex = $Mutex.WaitOne(0)
} catch [Threading.AbandonedMutexException] {
    $OwnsMutex = $true
}
if (-not $OwnsMutex) {
    Write-SupervisorLog "info" "Another production supervisor is already active; exiting."
    $Mutex.Dispose()
    exit 0
}

$RuntimeProcess = $null
$TunnelProcess = $null
$TunnelFailures = 0
$Backoff = 15
try {
    $ToolToken = Get-PrivateEnvValue -Path $ToolEnvironment -Name "HUMAN_OS_TOOL_TOKEN"
    Write-SupervisorLog "info" "Production supervisor started."
    while (-not (Test-InternetReady)) {
        Write-SupervisorLog "info" "Internet DNS is not ready; retrying."
        Start-Sleep -Seconds 15
    }
    $VpnAddress = Get-ReadyVpnAddress
    while (-not $VpnAddress) {
        Write-SupervisorLog "info" "AmneziaVPN is not ready; retrying."
        Start-Sleep -Seconds 15
        $VpnAddress = Get-ReadyVpnAddress
    }
    Write-SupervisorLog "info" "AmneziaVPN interface is ready."

    while ($true) {
        if (-not (Test-LoopbackServices)) {
            if ($RuntimeProcess -and -not $RuntimeProcess.HasExited) {
                Stop-Process -Id $RuntimeProcess.Id -Force -ErrorAction SilentlyContinue
            }
            Write-SupervisorLog "warn" "Loopback runtime is unavailable; starting it."
            $RuntimeProcess = Start-HiddenScript $RuntimeScript "runtime.stdout.log" "runtime.stderr.log"
            $Deadline = (Get-Date).AddSeconds(45)
            while ((Get-Date) -lt $Deadline -and -not (Test-LoopbackServices)) {
                Start-Sleep -Seconds 2
            }
            if (-not (Test-LoopbackServices)) {
                Write-SupervisorLog "error" "Loopback runtime failed readiness; backing off."
                Start-Sleep -Seconds $Backoff
                $Backoff = [Math]::Min($MaximumBackoffSeconds, $Backoff * 2)
                continue
            }
            Write-SupervisorLog "info" "Loopback runtime is ready."
        }

        $VpnAddress = Get-ReadyVpnAddress
        if (-not $VpnAddress) {
            Write-SupervisorLog "warn" "AmneziaVPN became unavailable; waiting without restarting aggressively."
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        $Cloudflared = @(Get-Process cloudflared -ErrorAction SilentlyContinue)
        if ($Cloudflared.Count -eq 0) {
            Write-SupervisorLog "warn" "Cloudflare connector is unavailable; starting it through the active VPN route."
            $TunnelProcess = Start-HiddenScript $TunnelScript "tunnel.stdout.log" "tunnel.stderr.log"
            Start-Sleep -Seconds $Backoff
        }

        if (Test-PublicHealth -Token $ToolToken) {
            if ($TunnelFailures -gt 0) { Write-SupervisorLog "info" "Public HTTPS health recovered." }
            $TunnelFailures = 0
            $Backoff = 15
        } else {
            $TunnelFailures++
            Write-SupervisorLog "warn" "Public HTTPS health failed ($TunnelFailures/$HealthFailuresBeforeRestart)."
            if ($TunnelFailures -ge $HealthFailuresBeforeRestart) {
                Write-SupervisorLog "warn" "Restarting Cloudflare connector after repeated health failures."
                Get-Process cloudflared -ErrorAction SilentlyContinue |
                    Stop-Process -Force -ErrorAction SilentlyContinue
                if ($TunnelProcess -and -not $TunnelProcess.HasExited) {
                    Stop-Process -Id $TunnelProcess.Id -Force -ErrorAction SilentlyContinue
                }
                $TunnelFailures = 0
                Start-Sleep -Seconds $Backoff
                $Backoff = [Math]::Min($MaximumBackoffSeconds, $Backoff * 2)
            }
        }
        Start-Sleep -Seconds $PollSeconds
    }
} catch {
    # Exception text from Windows/process APIs can contain private filesystem paths.
    Write-SupervisorLog "error" "Production supervisor stopped unexpectedly; inspect local private child logs."
    throw
} finally {
    if ($OwnsMutex) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
