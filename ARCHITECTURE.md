# Human OS Architecture

Human OS is a model-independent, reconstructible personal memory system.

Core pipeline:

```text
LIFE DATA
   ↓
IMMUTABLE RAW
   ↓
STABLE IDENTITY
   ↓
INDEX
   ↓
EPISODES
   ↓
SUMMARY
   ↓
MASTER
   ↓
CURRENT
   ↓
REPLACEABLE MODEL
```

Short form:

```text
RAW → INDEX → EPISODES → SUMMARY → MASTER → CURRENT → MODEL
```

## Core rules

1. No AI interpretation may destroy the source it was derived from.
2. Compression is not deletion: derived layers must be rebuildable from more primary layers.
3. MASTER is a versioned interpretation, not ground truth.
4. Stable object identity must survive changes of model, cloud, path and storage provider.
5. IDs are assigned before AI interpretation.
6. Serious claims should preserve provenance back to RAW.
7. `occurred_at` and `captured_at` are distinct concepts.

## Identity model

- `source_id`: native identifier from the source system.
- `object_id`: stable Human OS identifier independent of provider and model.
- `event_id`: identifier that links multiple objects into one real-world event.

## Retrieval

Vector search may be used inside the index, but semantic similarity alone is not the memory system. Human OS also needs chronology, provenance, version history, relations, event reconstruction and exact access back to RAW.

The v1 retrieval bridge is a read-only projection over existing INDEX evidence:

```text
natural-language query + filters
   -> transport-independent deterministic search
   -> current semantic results + metadata + relations
   -> ranked canonical object_id values + evidence/provenance
   -> CLI or 127.0.0.1-only JSON API (`/v1/health`, `/v1/search`, `/v1/object/{id}`)
```

The database is opened with SQLite `mode=ro` and `query_only`; no extractor is invoked
during retrieval. Ranking reports both matched and missing concepts. Consequently, a
best-effort candidate can say that the current semantic layer sees a person and snow
while explicitly refusing to assert an unobserved clothing category. Scores are search
signals, not edits to object confidence and not canonical facts.

The API is a transport adapter, not a second retrieval engine. `/v1/search` calls the
same ranking service as the CLI and replaces local RAW locations with opaque
`human-os://object/<object_id>` references. Stored provenance is projected to blob/hash
and extractor references so nested absolute paths cannot leak. Incomplete matches are
labelled `candidate`, list their missing evidence, and always carry `is_fact: false`.

## External read-only bridge

```text
External AI Client
   -> HTTPS tunnel / reverse proxy (replaceable provider)
   -> authenticated bridge on 127.0.0.1
   -> Local API on 127.0.0.1
   -> Retrieval Service
   -> Semantic Evidence
   -> Canonical Object
```

The bridge is an allowlisted adapter, never a generic proxy. It knows exactly three
external routes: `POST /v1/search`, `GET /v1/object/{object_id}` and
`GET /bridge/health`. The fixed upstream must be an `http://127.0.0.1:<port>` origin;
arbitrary paths, methods, headers and upstream hosts are rejected. Authorization uses
a minimum 256-bit Bearer token from environment-only private configuration and
constant-time comparison. The token is never forwarded to the Local API.

External TLS terminates at a replaceable tunnel or reverse proxy, while both local
services stay loopback-only. The bridge requires the trusted HTTPS forwarding marker,
applies configurable body limits, upstream timeout and per-token rate limiting, and
returns only JSON. It never imports or opens the canonical database. Audit logs exclude
queries and result content, recording only request ID, endpoint, status, latency,
result count and a one-way principal hash. A final path-redaction boundary removes
RAW URI keys and absolute Windows/file paths even if a future upstream regresses.

## ChatGPT-facing tool endpoint

```text
ChatGPT / AI Client
   -> stable provider-neutral HTTPS ingress
   -> Human OS Tool API on 127.0.0.1:8899
   -> authenticated bridge on 127.0.0.1:8787
   -> Local API on 127.0.0.1
   -> Retrieval Service
   -> Semantic Evidence
   -> Canonical Object
```

The Tool API is a transport/contract boundary, not another search engine. It calls only the
bridge allowlist and preserves the exact ranked `object_id` order returned by the
shared retrieval service. It compacts evidence into the five public categories
`caption`, `ocr`, `object`, `place` and `metadata`, while retaining confidence and an
explicit `fact`/`candidate`/`inference` assessment. Missing evidence is represented as
an unconfirmed inference and can never become a fact merely because it appeared in a
natural-language query.

Configuration is private and environment-only. External and internal bridge Bearer
tokens are separate 256-bit secrets. The adapter requires forwarded HTTPS, uses a
bounded timeout, emits no content logs, rejects unsafe responses containing local path
material, and normalizes transport, auth, rate-limit, server and response-shape
failures. It cannot open SQLite or RAW.

`tool-schema/human_os.openapi.json` defines the three connector operations and their
strict compact response contract. `human_os.openapi_contract` materializes an ignored
import copy only after a real stable HTTPS origin is supplied. The public ingress may
be replaced without code changes and may forward only to the Tool API, never directly
to the bridge, Local API, database or filesystem.
This separation keeps an AI client outside the laptop filesystem/SQLite trust boundary
and leaves the existing HTTPS bridge security and retrieval implementation unchanged.

On Windows, one current-user Scheduled Task runs the production supervisor after
logon. The supervisor enforces this readiness order: Internet DNS, active AmneziaVPN
interface, four loopback services (Local API, bridge, Tool API, mobile gateway),
Cloudflare Named Tunnel, then public HTTPS health.
A named local mutex makes repeated starts idempotent. Runtime and tunnel failures use
bounded retry/backoff; no credential, query, semantic content, DB path or RAW path is
written to the operational log.

## Mobile web boundary

```text
Android / Opera
   -> HTTPS /mobile (one-time mobile login, short session + signed trusted-device grant)
   -> Mobile gateway on 127.0.0.1:8990
      -> read-only object resolver -> private derived preview cache
   -> Tool API on 127.0.0.1:8899
   -> Bridge -> Local API -> Retrieval Service
```

The browser never receives `HUMAN_OS_TOOL_TOKEN` or `HUMAN_OS_MOBILE_TOKEN`. A
restart-safe HMAC-signed trusted-device cookie can mint a fresh short session through
the fixed refresh route without weakening Tool API authentication. The mobile gateway
validates Origin/CSRF/session state, forwards only search to the
existing Tool API, and applies a second privacy filter to the returned JSON. Mobile
and Tool API credentials are independent 256-bit values stored only in ignored
private environment files.

The mobile-only image route accepts a canonical photo `object_id` plus a fixed
`thumbnail` or `preview` variant. It opens INDEX read-only to resolve the private RAW
location, generates an EXIF-corrected bounded JPEG, and serves the cached derivative
only to an active mobile session. Tool API, Bridge, and Local Retrieval contracts do
not expose or proxy image binaries.
## Rebuildability

Derived layers such as embeddings, episodes, summaries, MASTER and CURRENT should be replaceable and recomputable. The long-term asset is the person's preserved source data plus stable identity and provenance.

## Person-centered environment model

Human OS is not the AI model and it is not any particular device. It is the personal digital environment between the person, their devices, their long-term memory and replaceable AI systems.

Its role is to receive multimodal information from the person and their devices, preserve and structure that information with provenance, maintain long-term life context, retrieve only the context relevant to the current situation, and make that context available to the appropriate AI system.

The architectural boundary is:

```text
PERSON
  ↕
SENSOR / INTERFACE LAYER
phone • headset • glasses • watch • cameras • other sensors
  ↕
HUMAN OS
identity • permissions • context • provenance • memory
  ↕
LIFE MEMORY / RETRIEVAL
objects • events • people • places • photos • documents • timeline
  ↕
AI ROUTER / REPLACEABLE INTELLIGENCE
GPT • Gemini • local models • future models
  ↕
ACTION LAYER
calendar • reminders • apps • devices • other permitted actions
  ↕
PERSON
```

### Architectural distinction

- **Human OS** = the person's environment, memory, context, identity, permissions and rules.
- **AI models** = replaceable intelligence engines that Human OS may call when reasoning or generation is needed.
- **Devices** = replaceable sensor and interaction interfaces that provide input to Human OS and deliver output back to the person.

Human OS therefore does not place the person's memory inside one AI provider. It places replaceable AI systems around the person's persistent memory and life context.

Short principle:

> **Human OS does not put memory inside AI. Human OS puts AI around the person's memory and life.**

This makes the person's preserved memory and context the stable center of the architecture while models, devices and service providers remain replaceable components.

## Semantic extraction layer

Semantic extraction is a versioned, rebuildable projection above canonical identity
and metadata. A semantic result references an existing `object_id` and the exact
`content_hash`/`blob_id` it analyzed. It never receives a new Human OS object identity.

```text
IMMUTABLE RAW
   ↓
object_id + blob_id
   ↓
derived metadata
   ↓
extractor registry (name + version)
   ↓
semantic_results + semantic_relations
```

The same extractor/version/source fingerprint is idempotent. A changed source or a
new extractor version creates a new historical result and retires the older current
projection. Unsupported and failed runs are durable results with diagnostics, not
ingestion failures.

`semantic_relations` belongs to the derived layer. It can record mentions of an entity,
place, person or topic without manufacturing canonical `object_id` values for concepts
that have not yet been promoted into a reviewed graph.

## Event preparation

Automatic event clustering is intentionally deferred. `event_evidence` can accumulate
versioned evidence from time, GPS, visual similarity, shared people, text context and
temporal neighborhood. A future event builder may combine that evidence, but no event
identity is asserted by semantic extraction v1.

## PHOTO production extractor

The PHOTO pipeline is a composite local runtime rather than one opaque model call:

```text
photo object + source blob/hash
   ├─ Florence-2 → image_caption
   ├─ Florence-2 → ocr_text
   ├─ DETR       → detected_objects + scored bounding boxes
   └─ GPS metadata only → detected_places candidates
```

Every branch produces its own semantic result and can be versioned or retried
independently. Exact model revisions and inference parameters belong to result
provenance. A successful extractor status means the computation completed; it does
not promote an unreviewed caption, OCR token or object label into canonical truth.
`person` may occur as a generic object label, but PHOTO v1 creates neither face
embeddings nor person identities or `semantic_mentions_person` relations.
