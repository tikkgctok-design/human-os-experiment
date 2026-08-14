# Cloudflare Named Tunnel for Human OS

This deployment is a concrete implementation of the provider-neutral ingress contract.
It does not change retrieval and does not expose ports 8765, 8787 or 8899 directly.
`cloudflared` creates an outbound connection and forwards one stable hostname only to
`http://127.0.0.1:8899`.

## 1. Check or install cloudflared

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Install-HumanOSCloudflared.ps1
```

If missing, install it explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Install-HumanOSCloudflared.ps1 -Install
```

## 2. One-time account authorization

The selected domain must already be an active zone in the Cloudflare account. Run the
following command yourself and complete the browser login/domain authorization:

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel login
```

This creates `%USERPROFILE%\.cloudflared\cert.pem`. It is an account credential: never
copy it into the repository, chat, screenshots, issue comments or logs.

## 3. Create the named tunnel and DNS route

Choose a subdomain, for example `memory.your-domain.example`, then run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Initialize-HumanOSNamedTunnel.ps1 `
  -Hostname "YOUR_REAL_HOSTNAME" `
  -TunnelName "human-os-tool"
```

The script creates/reuses the named tunnel, copies its runtime credential into ignored
`private/cloudflare/`, writes the private ingress config, validates it, and creates the
Cloudflare DNS route. The tracked repository receives no UUID, hostname or credential.

## 4. Start and validate

Console 1:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Start-HumanOSTool.ps1
```

Console 2:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Start-HumanOSNamedTunnel.ps1
```

If a VPN captures the default route, bind only the Cloudflare edge connection to the
physical interface without changing system routes:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Start-HumanOSNamedTunnel.ps1 `
  -EdgeBindAddress "YOUR_PHYSICAL_INTERFACE_IPV4"
```

If the VPN still owns the route despite source binding, add only Cloudflare's two
documented Tunnel edge networks through the physical gateway. Run this once from
an elevated PowerShell; the change is reversible with the same command plus
`-Remove` and adds no inbound route:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Set-HumanOSCloudflareEdgeRoutes.ps1 `
  -Gateway "YOUR_PHYSICAL_GATEWAY" -InterfaceIndex YOUR_INTERFACE_INDEX
```

Console 3:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Test-HumanOSNamedTunnel.ps1 -FullValidation
```

All validation artifacts remain under ignored `private/cloudflare/`.

## 5. Render the OpenAPI import copy

```powershell
$State = Get-Content private/cloudflare/state.json -Raw | ConvertFrom-Json
$env:HUMAN_OS_PUBLIC_BASE_URL = "https://$($State.hostname)"
.venv\Scripts\python.exe -m human_os.openapi_contract `
  --output private/human_os.openapi.json
Remove-Item Env:HUMAN_OS_PUBLIC_BASE_URL
```

Import the ignored output file into the AI client and configure API Key/Bearer auth
with `HUMAN_OS_TOOL_TOKEN` from private configuration. Never paste the token into the
OpenAPI document.

## 6. Ordered automatic startup with watchdog

After successful E2E validation, register one idempotent current-user task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Register-HumanOSProductionTasks.ps1
```

If the Amnezia tunnel profile service is still configured as `Manual`, run this once
from **PowerShell opened with Run as administrator**:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Enable-AmneziaTunnelAutostart.ps1
```

The helper changes only the startup type of the existing `AmneziaVPN` tunnel service;
it does not read, copy, print, or modify the VPN profile.

The task waits for Internet DNS and the `AmneziaVPN` interface, starts the three
loopback services, starts the Named Tunnel through the active VPN route, and checks
the public HTTPS health endpoint. Repeated health failures trigger a bounded backoff;
the named mutex prevents duplicate supervisors. Task arguments contain script paths
only, never tokens or tunnel credentials.

After a reboot, sign in to Windows and verify:

```powershell
Get-ScheduledTask -TaskName "Human OS Production Supervisor"
Invoke-WebRequest https://memory.humonosmemory.com/bridge/health `
  -Headers @{ Authorization = "Bearer YOUR_PRIVATE_TOOL_TOKEN" }
```

Do not save the token in shell history on a shared device. Supervisor logs remain in
ignored `private/production/` and never include request text, tokens, credentials,
captions, OCR, filesystem paths, or database paths.

## 7. Minimal phone test

Install a Python environment on the phone (Termux on Android or Pyto/Pythonista-style
environment on iOS), transfer only `scripts/mobile_test.py`, then run:

```text
python mobile_test.py
```

Enter `HUMAN_OS_TOOL_TOKEN` at the hidden prompt. The client keeps it only in process
memory and sends `найди фотографии со снегом` over HTTPS. Never transfer
`bridge.env`, the internal bridge token, Cloudflare credentials, DB, RAW, or any file
from `private/` to the phone.

## 8. Mobile web route

The named tunnel exposes the responsive mobile page at
`https://memory.humonosmemory.com/mobile`. Only `/mobile`, `/mobile/session` and
`/mobile/search` are routed to the loopback mobile gateway on `127.0.0.1:8990`;
the existing Tool API routes remain on `127.0.0.1:8899`.

The page contains no secret. Copy only `HUMAN_OS_MOBILE_TOKEN` from ignored
`private/mobile.env` into a password manager or another private transfer channel and
enter it once in Opera. Never transfer `HUMAN_OS_TOOL_TOKEN`, the bridge token,
Cloudflare credentials, DB, RAW, or any private config file to the phone.