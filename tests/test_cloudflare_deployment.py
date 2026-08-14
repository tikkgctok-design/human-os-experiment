import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARE_SCRIPTS = ROOT / "scripts" / "cloudflare"


def test_named_tunnel_scripts_are_secret_free_and_only_target_public_adapters() -> None:
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in CLOUDFLARE_SCRIPTS.glob("*.ps1")
    }
    assert {
        "Cloudflare.Common.ps1", "Install-HumanOSCloudflared.ps1",
        "Initialize-HumanOSNamedTunnel.ps1", "Start-HumanOSNamedTunnel.ps1",
        "Test-HumanOSNamedTunnel.ps1", "Register-HumanOSProductionTasks.ps1",
        "Set-HumanOSCloudflareEdgeRoutes.ps1", "Start-HumanOSProduction.ps1",
        "Enable-AmneziaTunnelAutostart.ps1",
    } <= set(texts)
    combined = "\n".join(texts.values())
    assert "http://127.0.0.1:8899" in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert "http://127.0.0.1:8990" in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert "^/mobile(/.*)?$" in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert "http://127.0.0.1:8765" not in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert "http://127.0.0.1:8787" not in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert "http_status:404" in texts["Initialize-HumanOSNamedTunnel.ps1"]
    assert not re.search(r"(?i)(?:token|secret)\s*=\s*['\"][0-9a-f]{64,}", combined)
    assert "cert.pem" not in texts["Register-HumanOSProductionTasks.ps1"]
    supervisor = texts["Start-HumanOSProduction.ps1"]
    assert "AmneziaVPN" in supervisor
    assert "Test-InternetReady" in supervisor
    assert "Test-PublicHealth" in supervisor
    assert "HealthFailuresBeforeRestart" in supervisor
    assert "Local\\HumanOSProductionSupervisor" in supervisor
    assert "EdgeBindAddress" not in texts["Register-HumanOSProductionTasks.ps1"]
    assert "HUMAN_OS_TOOL_TOKEN=" not in supervisor
    assert "$_.Exception.Message" not in supervisor
    autostart = texts["Enable-AmneziaTunnelAutostart.ps1"]
    assert "StartupType Automatic" in autostart
    assert "Run as administrator" in autostart
    routes = texts["Set-HumanOSCloudflareEdgeRoutes.ps1"]
    assert "198.41.192.0" in routes and "198.41.200.0" in routes
    assert "route.exe -p ADD" in routes and "[switch]$Remove" in routes


def test_named_tunnel_private_outputs_are_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "private/" in gitignore
    docs = (ROOT / "deploy" / "CLOUDFLARE_NAMED_TUNNEL.md").read_text(encoding="utf-8")
    assert "private/cloudflare/" in docs
    assert "127.0.0.1:8899" in docs
