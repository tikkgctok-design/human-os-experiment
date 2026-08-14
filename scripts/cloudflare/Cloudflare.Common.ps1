Set-StrictMode -Version Latest

function Get-HumanOSCloudflared {
    $Candidates = @(@(
        (Get-Command cloudflared.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })

    if (-not $Candidates) {
        throw "cloudflared is not installed. Run Install-HumanOSCloudflared.ps1 -Install."
    }
    return (Resolve-Path -LiteralPath $Candidates[0]).Path
}

function Assert-HumanOSHostname {
    param([Parameter(Mandatory)][string]$Hostname)

    if ($Hostname -notmatch '^(?=.{4,253}$)(?!-)(?:[a-z0-9-]+\.)+[a-z]{2,63}$') {
        throw "Hostname must be a DNS name only, for example memory.example.com."
    }
    if ($Hostname -match '(?i)(^|\.)example\.(com|net|org|invalid)$') {
        throw "A real Cloudflare-managed hostname is required."
    }
}

function Get-HumanOSEnvValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Private environment file does not exist: $Path"
    }
    $Prefix = "$Name="
    $Line = Get-Content -LiteralPath $Path -Encoding UTF8 |
        Where-Object { $_.StartsWith($Prefix, [System.StringComparison]::Ordinal) } |
        Select-Object -First 1
    if (-not $Line) {
        throw "Required private setting is missing: $Name"
    }
    return $Line.Substring($Prefix.Length)
}
