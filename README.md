# Human OS Experiment

Model-independent, reconstructible personal memory for replaceable AI models.

This repository is the public technical workspace for the Human OS experiment.

> Status: INDEX schema v5 — versioned semantic architecture plus the first local
> production PHOTO caption/OCR/object/place pipeline.

## Implemented

- deterministic UUIDv5 identities for source objects and structural relations;
- SQLite index for conversations, mapping nodes, messages and parent links;
- idempotent imports with explicit inserted/unchanged/changed/conflict counts;
- import-run audit log, object version history and non-fatal diagnostics;
- automatic migration of the original v1 structural index to schema v2;
- synthetic regression tests for repeat imports, changed objects and malformed data.
- universal identity-first ingestion for photos, videos, audio, notes and messages;
- schema v3 with canonical `attachments`, content-addressed `blobs` and
  provenance-preserving `blob_locations`;
- streaming SHA-256 for file-backed RAW without modifying source files.

## Universal ingestion identity

Source-object identity and binary identity are deliberately separate:

- `(source, source_id)` deterministically produces one stable Human OS `object_id`;
- when no native `source_id` exists, the canonical `raw_uri` is used as source identity;
- SHA-256 produces a shared `blob_id` for binary content;
- identical bytes at different RAW locations remain distinct Human OS objects while
  pointing to the same blob;
- repeated ingestion updates neither identity nor duplicate objects, attachments or
  locations.

The canonical API is `human_os.ingestion.ingest_object`. File-backed RAW is opened
read-only and hashed in chunks; notes and messages may provide immutable source bytes
directly. Metadata is stored as canonical JSON in `objects.metadata_json` and versioned
alongside the object state.

## Derived media metadata

`human_os.metadata_extraction.extract_media_metadata` adds a rebuildable metadata
projection above identity ingestion. Schema v4 stores extractor-versioned results in
`metadata_extractions` and diagnostics in `metadata_diagnostics`; neither object nor
blob identity is derived from EXIF or media tags.

Photo extraction uses Pillow for EXIF dates, timezone offsets, GPS, camera make/model,
orientation and dimensions. Video extraction uses MediaInfo for embedded creation
times, duration, dimensions, codec/container fields and supported ISO 6709 locations.
Canonical `occurred_at` is populated only from an unambiguous timezone-aware source.
Missing, naive or conflicting timestamps remain null and produce diagnostics.

## Versioned semantic extraction

`human_os.semantic_extraction.run_semantic_extractor` adds a rebuildable semantic
projection without changing RAW, `object_id` or `blob_id`. Every result records the
canonical object, exact content hash/blob, extractor name and version, status,
confidence, payload, diagnostics and provenance. The same extractor/version/source
fingerprint is idempotent. A different extractor version or changed source creates a
new result, keeps history and makes only the matching latest projection current.

Schema v5 adds `semantic_results`, `semantic_relations` and an empty
`event_evidence` preparation table. The lightweight default registry exposes safe
contracts for photo, video, audio, note and message semantics without loading models;
the separate production PHOTO registry supplies the pinned local adapters described
below. Other unimplemented tasks return durable `unsupported` results. No inference
request is sent to an external service.

## Production PHOTO semantics

`human_os.photo_semantic.run_photo_semantics` is the first local production
semantic pipeline. It writes four independent, versioned results through the
existing `semantic_results`/`semantic_relations` layer:

- detailed captions and OCR regions from a pinned Florence-2 base-ft checkpoint;
- scored COCO object bounding boxes from a pinned DETR ResNet-50 checkpoint;
- place candidates only from valid GPS derived metadata, without visual guessing
  or reverse geocoding.

Model revisions, runtime/library versions, device/dtype, generation parameters,
thresholds and source blob/hash are preserved in provenance. The optional
`vision` dependency group installs the local PyTorch/Transformers runtime. Face
identification and canonical person identities are explicitly outside this layer.

## Read-only retrieval bridge

`human_os.search` provides deterministic retrieval over current, already-computed
semantic results, semantic relations, metadata and canonical object relations. It opens
SQLite with `mode=ro` and `PRAGMA query_only`; retrieval never runs an extractor and
never writes INDEX or RAW. Russian PHOTO queries are bridged to the English PHOTO v1
labels by a small, explicit vocabulary. Missing evidence is returned as a best-effort
limitation rather than promoted to canonical truth.

CLI:

```console
python -m human_os.search "найди фотографии со снегом" --db private/human_os.db
```

Localhost-only JSON API:

```console
python -m human_os.search_api --db private/human_os.db --port 8765
curl http://127.0.0.1:8765/v1/health
curl -X POST http://127.0.0.1:8765/v1/search -H "Content-Type: application/json" -d "{\"query\":\"найди фото с вывеской\",\"filters\":{\"media_type\":\"photo\"},\"limit\":10}"
curl http://127.0.0.1:8765/v1/object/hos_obj_eb67743bd6785ca5b3b5e8b02ebfaa57
```

Optional filters are `time_from`, `time_to`, `place`, and `media_type`. Each hit
contains the canonical `object_id`, opaque `safe_ref`, timestamps, deterministic score/reason,
matched and missing concepts, plus exact semantic/metadata/relation evidence and its
provenance reference. Absolute RAW paths and stored provenance fields containing those
paths are never serialized by the API. The HTTP server rejects non-`127.0.0.1` bindings
in v1. Search remains the same transport-independent service used by the CLI.

Real-archive validation (report stays under ignored `private/`):

```console
python -m human_os.search_validation --db private/human_os.db --output private/search-validation-report.json
```

## External read-only bridge

`human_os.bridge` is a separate authenticated HTTP adapter in front of the Local API.
It never opens SQLite and exposes only `POST /v1/search`,
`GET /v1/object/{object_id}` and `GET /bridge/health`. Both the Local API and bridge
remain bound to `127.0.0.1`; an independently operated HTTPS tunnel may point only to
the bridge port. The bridge does not contain provider-specific tunnel code.

Generate a 256-bit token directly into ignored private configuration (the command does
not print it):

```console
python -c "import secrets,pathlib; pathlib.Path('private').mkdir(exist_ok=True); pathlib.Path('private/bridge.env').write_text('HUMAN_OS_BRIDGE_TOKEN='+secrets.token_hex(32)+'\n',encoding='utf-8')"
```

Start the Local API:

```console
python -m human_os.search_api --db private/human_os.db --host 127.0.0.1 --port 8765
```

In a second PowerShell, load the private token and start the bridge:

```powershell
python -m human_os.bridge --env-file private/bridge.env
```

For a temporary validation-only Cloudflare Quick Tunnel:

```console
cloudflared tunnel --url http://127.0.0.1:8787
```

If that provider's edge protocol is blocked by the current network, the same
provider-neutral bridge can be validated with LocalTunnel and stopped with `Ctrl+C`:

```console
npx --yes localtunnel --port 8787 --local-host 127.0.0.1
```

Use the temporary HTTPS URL printed by the selected tunnel CLI:

```console
curl -X POST https://RANDOM.trycloudflare.com/v1/search -H "Authorization: Bearer TOKEN_FROM_PRIVATE_CONFIG" -H "Content-Type: application/json" -d "{\"query\":\"найди фотографии со снегом\"}"
```

Configuration is environment-only: body limit, upstream timeout and rate limits use
`HUMAN_OS_BRIDGE_BODY_LIMIT`, `HUMAN_OS_BRIDGE_UPSTREAM_TIMEOUT`,
`HUMAN_OS_BRIDGE_RATE_LIMIT` and `HUMAN_OS_BRIDGE_RATE_WINDOW`. Audit records contain
only timestamp, request ID, endpoint, status, latency, result count and a one-way hash
of the token principal. Queries, captions, OCR and tokens are never logged.

## ChatGPT / AI client tool endpoint

`human_os.tool_api` is a separate loopback HTTP adapter above the authenticated bridge.
It implements exactly the three operations described by the OpenAPI contract:

- `human_os_search(query, limit=10)`;
- `human_os_get_object(object_id)`;
- `human_os_health()`.

The Tool API and `human_os.tool_client` do not open INDEX or RAW. The Tool API uses a
separate external 256-bit `HUMAN_OS_TOOL_TOKEN`; its internal bridge credential remains
`HUMAN_OS_BRIDGE_TOKEN`. Both live only in ignored private env files and must differ.
Search output is compact: ranked canonical IDs, scores, timestamps, media type,
opaque `safe_ref`, reasons and explicitly assessed evidence. Derived evidence is not
promoted to fact; an unsupported clothing concept such as `swimwear` is returned as
`candidate`/`inference` with `is_fact: false`.

Start all loopback services on Windows with the foreground supervisor:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Start-HumanOSTool.ps1
```

The runtime binds Local API, bridge, Tool API and the mobile gateway only to
`127.0.0.1` on ports 8765, 8787, 8899 and 8990 respectively. A stable provider-neutral
TLS ingress forwards only the three allowlisted paths to port 8899. See
`deploy/WINDOWS.md`, `deploy/INGRESS.md` and
`deploy/CLOUDFLARE_NAMED_TUNNEL.md` for the supported named-tunnel deployment.

For unattended production startup on Windows, register the single ordered supervisor:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts/cloudflare/Register-HumanOSProductionTasks.ps1
```

It waits for Internet and the AmneziaVPN interface before starting the loopback
runtime and Named Tunnel. Its health watchdog uses bounded backoff and stores only
content-free operational logs under ignored `private/production/`. A minimal phone
search test is provided in `scripts/mobile_test.py`; it prompts for the external tool
token without echoing or saving it.

`tool-schema/human_os.openapi.json` is the versioned schema template. Once a stable
hostname exists, render the ignored import copy with the exact production server:

```powershell
$env:HUMAN_OS_PUBLIC_BASE_URL = "https://YOUR-STABLE-HOSTNAME"
.venv\Scripts\python.exe -m human_os.openapi_contract --output private/human_os.openapi.json
```

The renderer rejects HTTP, localhost, credentials, URL paths and example hostnames.
Import `private/human_os.openapi.json`, never the unresolved template. Configure
`HUMAN_OS_TOOL_TOKEN` as the external Bearer/API-key secret; never embed it in schema.
The schema marks all three read-only operations `x-openai-isConsequential: false`.

## Mobile web client

The minimal Android/Opera client is served at
`https://memory.humonosmemory.com/mobile`. The static page contains no bearer secret.
A separate 256-bit `HUMAN_OS_MOBILE_TOKEN` lives only in ignored
`private/mobile.env`. On the first login it is exchanged over HTTPS for a short-lived
`Secure`, `HttpOnly`, `SameSite=Strict` session cookie, an anti-CSRF nonce, and a
signed 30-day trusted-device cookie. The page automatically renews an expired session
through `POST /mobile/session/refresh`; neither the mobile access token nor the Tool
API token is stored by JavaScript or returned to the browser. Search requests
then go to the loopback-only mobile gateway on `127.0.0.1:8990`; the gateway adds the
server-side Tool API credential and forwards only `POST /v1/search`.

The gateway opens canonical INDEX strictly read-only and reads a RAW photo only while
creating an authenticated derived preview; it never modifies or exposes either. It
rejects unsafe responses, uses a strict same-origin policy, logs no query/result/token content, and exposes only
`GET /mobile`, `POST /mobile/session`, `POST /mobile/session/refresh`, and
`POST /mobile/search` through the named tunnel path rule.

Photo results are enriched only inside the authenticated mobile boundary. The gateway
resolves a canonical `object_id` against INDEX in read-only mode, applies EXIF
orientation, and caches derived JPEGs under ignored `private/mobile-preview-cache/`.
`GET /mobile/image/{object_id}?variant=thumbnail|preview` requires an active mobile
session and never returns the RAW URI, filesystem location, database path, or original
binary. Thumbnail clicks open the larger bounded preview, not RAW.
The intended data flow is:

```text
ChatGPT / AI Client -> stable HTTPS ingress -> Human OS Tool API -> Bridge -> Local API
   -> Retrieval Service -> Semantic Evidence -> Canonical Object
```

Bridge/network errors are normalized to `timeout`, `bridge_unreachable`,
`unauthorized`, `rate_limited`, `bridge_error`, `malformed_response`,
`unsafe_response` or `invalid_request`; error JSON never contains the token or local
configuration. The adapter does not log requests or responses.

For a Python client of the stable endpoint:

```powershell
$env:HUMAN_OS_TOOL_BASE_URL = "https://YOUR-STABLE-HOSTNAME"
python -m human_os.tool_client --env-file private/tool.env search "найди где я на снегу"
```

Run an import from an installed development environment:

```console
python -m human_os.chatgpt_import conversations.json private/human_os.db
```

RAW exports and generated databases are private local data and are ignored by Git.
