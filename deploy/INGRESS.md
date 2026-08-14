# Stable HTTPS ingress contract

The public ingress is replaceable infrastructure, not part of retrieval or identity.
It may be a named tunnel, reverse proxy, VPN funnel or self-hosted TLS proxy, provided
that it satisfies all of these requirements:

- owns a stable HTTPS hostname and a valid publicly trusted certificate;
- terminates TLS 1.2 or newer on public port 443;
- forwards only to `http://127.0.0.1:8899` on the Human OS Windows host;
- sets `X-Forwarded-Proto: https` itself and does not trust a client-supplied value;
- exposes only `POST /v1/search`, `GET /v1/object/{object_id}` and
  `GET /bridge/health`;
- preserves `Authorization`, `Content-Type` and `X-Request-ID`, but strips other
  unneeded forwarding headers;
- applies no path prefix or rewrite;
- never forwards to ports 8765 (Local API), 8787 (bridge), SQLite or the filesystem;
- stores provider credentials and tunnel configuration only below ignored `private/`;
- starts after `scripts/Start-HumanOSTool.ps1` and stops independently.

The tracked repository intentionally contains no provider account, hostname,
certificate, tunnel identifier or credential. After provisioning a stable ingress,
render the importable OpenAPI copy:

```powershell
$env:HUMAN_OS_PUBLIC_BASE_URL = "https://memory.example.net"
.venv\Scripts\python.exe -m human_os.openapi_contract `
  --output private/human_os.openapi.json
```

The renderer rejects HTTP, localhost, credentials, URL paths and example hostnames.
The resulting ignored file is the schema to import into an external AI client.

The supported Cloudflare Named Tunnel implementation is documented in
[`CLOUDFLARE_NAMED_TUNNEL.md`](CLOUDFLARE_NAMED_TUNNEL.md). Provider-specific state
remains outside the architecture and entirely below ignored `private/cloudflare/`.
