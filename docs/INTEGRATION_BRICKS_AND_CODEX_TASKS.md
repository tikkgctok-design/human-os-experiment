# Human OS — Integration Bricks & Codex Task Pack

Status: planning / research backlog

Purpose: keep Human OS Core small and connect mature ready-made components through adapters instead of rewriting photo search, speech, glasses, tracking, OCR, translation, etc. from scratch.

## Target architecture

```text
Headset / Glasses / Phone sensors
        ↓
Mobile Gateway / Device Adapters
        ↓
Human OS API + Memory Core
        ↓
Memory Providers / Evidence Providers
        ↓
AI Providers (GPT / Claude / Gemini / local)
```

Human OS owns: identity, memory graph, timeline, provenance, permissions, retrieval contracts, evidence links, provider registry.

External bricks own specialized capabilities: photo management, camera/mic transport, STT, TTS, OCR, translation, GPS collection, smart-glasses hardware access.

---

## 1. Smart glasses bricks

### xg.glass
Repository: https://github.com/hkust-spark/xg-glass-sdk

Why interesting: unified SDK over multiple smart-glasses ecosystems. Main primitives are camera photo capture, video stream, display, image display, microphone stream and speaker output. Supports/adapts devices from multiple vendors including Rokid, Meta, Brilliant Labs, RayNeo, Even Realities, INMO and others depending on capability.

Human OS role: preferred generic `GlassesProvider` candidate. Do not bind Human OS to one glasses brand.

### Mentra Bluetooth SDK / MentraOS
Repositories:
- https://github.com/Mentra-Community/Mentra-Bluetooth-SDK-Starter-Kit
- https://github.com/Mentra-Community/MentraOS
- https://github.com/Mentra-Community/Edge_AI_SmartGlasses

Why interesting: Bluetooth/mobile SDK, mic/audio guides, photo/video, streaming, display, buttons, reconnect logic and cross-device abstraction for compatible glasses.

Human OS role: second `GlassesProvider`, especially useful for BLE audio/camera/device events.

### OpenSource-AI-Glasses
Repository: https://github.com/Iam5tillLearning/OpenSource-Ai-Glasses

Why interesting: embedded Linux glasses with C/C++ SDK, BLE, RTSP camera stream, recording/playback, photo taking, display and device controls.

Human OS role: reference implementation for fully open hardware path and future self-hosted glasses experiments.

### nisaetus
Repository: https://github.com/pepebot-space/nisaetus

Why interesting: practical BLE glasses client showing camera thumbnails, OPUS microphone transport, Wi-Fi media download and WebSocket AI integration.

Human OS role: protocol/reference brick, not a required dependency.

---

## 2. Headset / microphone / voice bricks

Goal: headset should connect to the phone; phone is the gateway. Human OS should not implement Bluetooth audio drivers.

Candidate technologies to evaluate:
- Android native audio routing / AudioRecord / Bluetooth headset microphone
- whisper.cpp — https://github.com/ggerganov/whisper.cpp
- faster-whisper — https://github.com/SYSTRAN/faster-whisper
- Vosk — https://github.com/alphacep/vosk-api
- openWakeWord — https://github.com/dscripka/openWakeWord
- Piper TTS — https://github.com/rhasspy/piper

Provider contracts:

```text
AudioInputProvider.start()/stop()
STTProvider.transcribe()/stream()
WakeWordProvider.detect()
TTSProvider.synthesize()/stream()
```

First MVP must be push-to-talk. Wake word and 24/7 listening come later.

---

## 3. Photo / video memory bricks

### Immich
Repository: https://github.com/immich-app/immich

Why interesting: mature self-hosted photo/video system with mobile backup, timeline, thumbnails, EXIF/GPS, people/faces and semantic/smart search.

Human OS role: preferred `PhotoProvider` candidate. Human OS should consume asset IDs, metadata, thumbnails and search results instead of becoming a full photo manager.

### PhotoPrism
Repository: https://github.com/photoprism/photoprism

Why interesting: alternative self-hosted photo indexing/organization stack with labels and metadata.

Human OS role: optional second `PhotoProvider` to keep architecture vendor-neutral.

### Existing Human OS photo index

Keep current implementation as fallback/reference provider and for evidence/provenance experiments. Do not delete it merely because Immich is added.

Canonical Human OS photo result:

```text
object_id
provider_id
provider_asset_id
captured_at
location
people
caption
ocr
concepts
thumbnail_ref
evidence_refs
confidence
provenance
```

---

## 4. Geolocation / background tracking bricks

### Traccar Client + Traccar Server
Repositories:
- https://github.com/traccar/traccar-client
- https://github.com/traccar/traccar

Why interesting: existing open-source background GPS tracking pipeline for Android/iOS to a self-hosted server, configurable reporting intervals and accuracy.

Human OS role: possible `LocationProvider` instead of writing a 24/7 GPS tracker from zero.

Canonical location evidence:

```text
event_id
device_id
captured_at
latitude
longitude
accuracy_m
altitude
speed
bearing
provider_id
source_event_id
hash/provenance
```

Do not infer exact location from AI if raw GPS evidence exists; keep measured evidence separate from interpretation.

---

## 5. OCR / translation / AI capabilities

Do not hard-wire one model. Define providers first.

Suggested categories:
- OCRProvider: local OCR or provider-supplied OCR from Immich/photo stack
- TranslationProvider: Android/system service, local model, GPT, Claude, Gemini or dedicated translation API
- AIProvider: GPT / Claude / Gemini / local model

Canonical translation result:

```text
source_text
translated_text
source_language
target_language
provider_id
confidence
latency_ms
provenance
```

Voice translator pipeline:

```text
Headset mic → Phone → STT → language detect → TranslationProvider → optional AI context → TTS → Headset
```

Human OS memory is consulted only when context is useful; translation must not require the full memory round-trip for every phrase.

---

## 6. Sensor / evidence stream — future branch

Do not implement full 24/7 sensor stream yet, but keep interfaces compatible with it.

Potential inputs:
- phone GPS
- accelerometer / gyroscope
- activity recognition
- battery/charging
- network changes
- Bluetooth device presence
- new photo/video events
- watch heart-rate/sleep/workout events where APIs permit
- glasses camera/mic/button/device events

Canonical `SensorEvent`:

```text
event_id
owner_id
device_id
sensor_type
captured_at
payload_or_payload_ref
accuracy
provider_id
source_event_id
content_hash
signature
provenance
```

Human OS should later build episodes from events. Facts/evidence and AI interpretation must stay separate.

---

# Codex task pack

Run tasks sequentially. Do not allow several coding agents to edit the same files/branch simultaneously.

## CODEX-01 — Provider architecture

```text
Project: C:\GitHub\human-os-experiment

Inspect the current Human OS architecture and introduce a minimal provider/adapter layer without breaking the existing Memory Core, retrieval, public API, mobile gateway, RAW/evidence model or tests.

Define provider categories:
- PhotoProvider
- VisionProvider
- OCRProvider
- AudioInputProvider
- STTProvider
- TTSProvider
- TranslationProvider
- GlassesProvider
- LocationProvider
- AIProvider
- FileProvider

Each provider exposes provider_id, version, capabilities, health/status, local/cloud, privacy metadata and optional latency/cost metadata.

Add a ProviderRegistry with config-based selection and fallbacks.

Do not implement heavy models yet.
Do not change RAW files.
Do not expose secrets or filesystem paths.
Run full tests and SQLite integrity checks.
Do not commit/push unless explicitly requested.

Return: files changed, interfaces, compatibility impact, tests, and recommended next provider to implement.
```

## CODEX-02 — Immich PhotoProvider

```text
Implement an Immich PhotoProvider behind the provider interface.

Capabilities to support where Immich API allows:
- asset lookup
- semantic/smart search
- thumbnails
- EXIF/date/time
- GPS/location metadata
- people/faces metadata
- media type
- safe preview/open flow

Map Immich assets into Human OS MemoryObject without leaking local paths.
Keep current Human OS photo index as fallback provider.
Do not copy or mutate originals unnecessarily.
Use mock/config tests if credentials/server are not yet available.
Document exact manual installation/credential steps before requesting secrets.
```

## CODEX-03 — Phone push-to-talk voice pipeline

```text
Build the first phone voice MVP as push-to-talk, not wake-word.

Flow:
Bluetooth headset or phone mic → Android/mobile client → AudioInputProvider → STTProvider → text → Human OS query/intent → response text → TTSProvider → headset/phone audio.

Prefer system/native phone audio routing; do not write Bluetooth drivers.
Add timing metrics for capture, STT, Human OS, AI and TTS.
Keep text input working as fallback.
Do not implement continuous listening yet.
```

## CODEX-04 — STT adapters

```text
Implement STT provider adapters in priority order:
1. Android/system STT adapter or mobile contract
2. whisper.cpp or faster-whisper local backend
3. Vosk fallback if useful
4. generic cloud STT adapter interface

Canonical result: text, language, confidence, start/end timestamps, provider_id, latency_ms, optional audio_evidence_ref.
Support partial + final transcript when provider supports streaming.
Do not upload privacy-sensitive audio to cloud without policy permission.
```

## CODEX-05 — TTS adapters

```text
Implement TTS provider abstraction with Android/system TTS as first mobile implementation and Piper/local option as second backend.

Support streaming/early playback if possible.
Return provider_id, format, sample rate, latency and audio reference/stream.
Prove phone → headset playback route end-to-end.
```

## CODEX-06 — Translation provider + live translator MVP

```text
Add TranslationProvider and a live translator mode.

Flow:
audio → STT → language detection → translation → TTS.

Target language is user-configurable (default from profile, never hard-code globally).
Providers can include local model, dedicated translation API or AI providers.
Human OS memory/context is optional and only supplied when it improves translation.
Measure latency and support fallback providers.
First MVP is push-to-talk / turn-based, not always-listening simultaneous translation.
```

## CODEX-07 — Glasses abstraction research spike

```text
Research integration feasibility for:
- xg.glass SDK
- Mentra Bluetooth SDK / MentraOS
- OpenSource-AI-Glasses

Do not vendor huge repositories into Human OS.
Create GlassesProvider interface around capabilities:
connect/disconnect/status
capture_photo
start/stop camera stream
start/stop microphone
play_audio
display_text/display_image where supported
button/touch/device events
battery/status

Build a simulator/mock first so Human OS is not blocked on owning specific glasses hardware.
Produce a compatibility matrix and recommend the first physical glasses family to test.
```

## CODEX-08 — Traccar LocationProvider

```text
Evaluate Traccar Client/Server as the first LocationProvider for Human OS.

Goal: obtain reliable background location events from Android without writing our own 24/7 tracker from scratch.

Create an adapter/import path that maps Traccar positions to Human OS SensorEvent/location evidence.
Preserve source timestamp, device ID, GPS accuracy and provenance.
Do not expose precise location publicly by default.
Do not implement full Episode Builder yet.
```

## CODEX-09 — Unified evidence ingestion

```text
Create/extend a safe ingestion boundary for externally-produced evidence from photo, location, audio and glasses providers.

Requirements:
- idempotency
- content/source IDs
- deduplication
- provenance
- timestamp normalization
- owner/user isolation readiness
- no arbitrary direct DB writes from providers
- queue/retry/failure state
- raw evidence immutable by default

All provider writes must pass through this boundary.
```

## CODEX-10 — Portable node environment

```text
Prepare Human OS for multiple machines: development laptop, background worker laptop, home PC/server and later VPS/cloud.

Use containerized or reproducible services where practical.
Create documented roles:
- core node
- photo/ML worker
- background ingestion worker
- development workstation

A new node should be bootstrappable from repo + config + secrets without manual machine-specific surgery.
Do not migrate production data yet; make the environment portable first.
```

---

## Priority order

1. Provider architecture
2. Immich PhotoProvider
3. Push-to-talk voice MVP
4. STT + TTS
5. Translation
6. Glasses simulator/provider
7. Location provider
8. Unified evidence ingestion
9. Portable multi-node environment
10. Later: wake word, watches, 24/7 Sensor Stream, Episode Builder

## Rule for all integrations

Human OS is the memory/context layer, not a replacement for every specialized app.

Prefer:

```text
best existing component → adapter → Human OS
```

instead of:

```text
rewrite everything inside Human OS
```

Before adopting any dependency, verify license, maintenance activity, security model, platform support and whether its public API is stable enough for an adapter.
