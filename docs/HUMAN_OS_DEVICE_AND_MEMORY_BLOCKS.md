# Human OS — Device, Memory and Sensor Blocks

This document captures the architecture blocks discussed for Human OS so they are not lost and can be implemented gradually.

## Core principle

Human OS is the personal layer between a person, their devices/data and replaceable AI systems.

```text
PERSON
  ↕
DEVICES / DATA SOURCES
  ↕
HUMAN OS
  ↕
PERSONAL MEMORY
  ↕
AI ROUTER / MODELS
  ↕
ACTIONS / OUTPUT
```

The key rule is:

> Devices and data connect to Human OS. AI systems also connect to Human OS. The person does not move their life into one specific AI provider.

## 1. Human OS Memory Core

The memory layer should preserve source evidence and build derived layers above it.

```text
RAW / EVIDENCE
   ↓
STABLE IDENTITY
   ↓
INDEX
   ↓
SEMANTIC RESULTS + RELATIONS
   ↓
EPISODES / CHECKPOINTS
   ↓
RETRIEVAL
   ↓
AI CONTEXT
```

Principles:
- RAW must remain immutable where possible.
- AI interpretations must be separated from source evidence.
- Derived layers must be rebuildable.
- Every serious claim should preserve provenance.
- `occurred_at` and `captured_at` are different concepts.
- A generated reconstruction is not equivalent to original evidence.

## 2. Photo Memory Pipeline

```text
PHONE CAMERA
   ↓
GOOGLE DRIVE / SOURCE STORAGE
   ↓
PHOTO WATCHER
   ↓
HASH / DEDUP
   ↓
EXIF
(date, time, GPS, device)
   ↓
VISION / OCR
   ↓
CONCEPTS / OBJECTS / PLACES
   ↓
SEMANTIC INDEX + RELATIONS
   ↓
HUMAN OS MEMORY
```

The goal is to transform an archive of files into an indexed biographical memory so the system does not need to re-open every photo for every query.

Future automatic ingestion states:

```text
discovered → stable → queued → processing → indexed → failed/retry
```

## 3. Mobile Client / Mobile Gateway

The phone is the gateway between wearable devices and Human OS.

```text
HEADPHONES / GLASSES / WATCH
          ↓
       PHONE
          ↓ HTTPS
memory.humonosmemory.com
          ↓
      HUMAN OS
```

The mobile client should remain lightweight. Heavy processing stays in Human OS and AI services.

Responsibilities of the phone:
- authentication/session;
- microphone input;
- camera/photo upload;
- GPS and basic sensor access;
- Bluetooth/Wi-Fi device connectivity;
- local queue/buffer;
- TTS playback;
- temporary cache.

## 4. Headphones / Voice Block

First implementation does not need custom Bluetooth drivers. Android already exposes Bluetooth headset microphone/output.

Prototype path:

```text
HEADSET MIC
   ↓
ANDROID PHONE
   ↓
PRESS-TO-TALK BUTTON
   ↓
STT
   ↓
HUMAN OS
   ↓
MEMORY / AI
   ↓
TEXT RESPONSE
   ↓
TTS
   ↓
HEADSET
```

Then evolve to wake-word mode:

```text
"LEO"
  ↓
local wake word
  ↓
voice session
  ↓
STT → Human OS → AI/Memory → TTS
```

Suggested staged rollout:
1. push-to-talk;
2. STT;
3. TTS;
4. continuous session;
5. wake word;
6. hands-free background mode.

Potential open-source references/starting points to evaluate later:
- OpenClaw Assistant — Android voice assistant architecture with wake word/STT/TTS/continuous mode/self-hosted gateway pattern;
- Dicio — Android assistant with Vosk/OpenWakeWord patterns;
- Rhasspy Mobile — background wake-word patterns;
- openWakeWord — wake-word component;
- offline Android STT projects such as Scrib/Outspoke.

These should be treated as reference implementations or reusable components, not as the Human OS backend itself.

## 5. Smart Glasses Block

Glasses are a visual sensor, not the intelligence layer.

```text
GLASSES CAMERA
    ↓ Bluetooth/Wi-Fi/API
PHONE MOBILE GATEWAY
    ↓
HUMAN OS
    ↓
VISION MODEL + MEMORY
    ↓
VOICE/TEXT RESPONSE
```

Preferred operation is event-driven rather than continuous video upload.

Examples:
- “Leo, what is this?” → capture frame → vision → answer.
- “Leo, remember this.” → frame + timestamp + GPS + voice note → checkpoint.

## 6. Ingestion / Write Layer

Current retrieval/read path should be complemented by a restricted write API.

Suggested capabilities:
- `upload_photo`
- `create_checkpoint`
- `add_voice_note`
- `confirm_metadata`

Do not allow arbitrary database writes from AI or mobile clients.

Photo write path:

```text
upload
  ↓
hash
  ↓
dedup
  ↓
object_id
  ↓
EXIF
  ↓
vision/OCR
  ↓
semantic index
  ↓
relations
  ↓
checkpoint/event linkage
```

## 7. AI Router

Human OS should support multiple replaceable AI systems.

```text
TASK / EVENT
   ↓
HUMAN OS ROUTER
   ├── GPT
   ├── Gemini
   ├── local model
   ├── vision model
   ├── speech model
   ├── translation model
   └── coding/automation agent
```

Routing criteria can include:
- task type;
- latency;
- cost;
- privacy;
- model capability;
- local vs cloud processing.

Core principle:

> Human OS belongs to the person. AI systems are rented capabilities.

## 8. 24/7 Sensor Stream — Future Branch

Working name:
- Human OS Evidence Stream
- Life Sensor Layer

Goal: a continuous evidence-oriented stream from phone, watch, glasses and future devices that allows important life events to be reconstructed later without storing continuous video/audio.

### Unified event model

```text
SensorEvent
├── event_id
├── device_id
├── sensor_type
├── captured_at
├── value / payload_ref
├── accuracy
├── source
├── hash
├── signature
└── provenance
```

### Potential sources

Phone:
- GPS/location;
- accelerometer;
- gyroscope;
- activity/motion state;
- battery/charging;
- network state;
- Bluetooth connections;
- screen/activity state where permitted;
- new photo/camera events.

Watch:
- heart rate;
- steps;
- sleep;
- workouts;
- movement/activity;
- other metrics exposed by the device API.

Glasses:
- connection state;
- triggered frames;
- microphone/audio events where permitted;
- visual event metadata.

### Local buffering

The phone should keep an encrypted local queue and upload batches when connectivity is available.

```text
SENSORS
  ↓
LOCAL BUFFER
  ↓
BATCH UPLOAD
  ↓
HUMAN OS RAW EVIDENCE
```

If internet is unavailable, data must not be lost.

### Adaptive sampling

24/7 sensing does not mean recording everything at maximum frequency.

Modes:
- Passive — low-cost sensing;
- Active — increased sampling after a meaningful context change;
- Explicit — rich capture after a user command such as “Leo, remember this.”

Example:
- sleeping/home unchanged → very low sampling;
- movement begins → sampling increases;
- arrival at a new place → checkpoint candidate;
- photo / unusual physiological signal / new device / voice command → richer capture.

## 9. Episode Builder

Raw sensor events should be transformed into candidate episodes.

```text
SensorEvents
     ↓
Episode Builder
     ↓
Checkpoint / Episode
     ↓
AI Interpretation
     ↓
Human confirmation/correction
```

Example evidence timeline:

```text
09:02 left home
09:14 entered vehicle
09:37 arrived at new location
09:48 captured 7 photos
10:31 left location
```

AI may infer “probably a training session” but this must remain an interpretation, not a source fact.

## 10. Evidence vs Interpretation

Human OS should explicitly separate:

```text
EVIDENCE
- original photo
- signed timestamp
- GPS
- sensor data
- audio

INTERPRETATION
- caption
- inferred place
- inferred person
- inferred event meaning
- generated reconstruction
```

A generated image/reconstruction must never be presented as original historical evidence.

Core rule:

> Preserve the minimum set of primary signals sufficient to reconstruct the context of important moments, without pretending that generated reconstructions are exact records.

## 11. Shared / Collective Episodic Memory — Future Branch

Multiple users may share evidence for the same real-world event without merging their private memories.

```text
PERSON A MEMORY ─┐
PERSON B MEMORY ─┼→ SHARED EVENT → reconstructed common history
PERSON C MEMORY ─┘
```

Possible shared evidence:
- timestamps;
- GPS;
- selected photos;
- selected video/audio;
- event metadata.

Each user retains private data and grants explicit permission for shared fragments.

Important distinction:

> Shared history does not require a shared database.

The system should preserve different human recollections and AI interpretations instead of forcing one narrative to become “truth”.

## 12. Security and Privacy Principles

These become increasingly important as the system becomes richer.

- user-owned memory;
- encryption at rest and in transit;
- device-level trust/session management;
- scoped permissions;
- revocable sharing;
- access logs;
- provenance;
- immutable/source evidence where appropriate;
- no permanent hidden access for external AI providers;
- AI receives only the minimum context needed for a task;
- private RAW paths and storage topology must not leak through public APIs.

## 13. Near-term implementation order

Do not implement every branch at once.

Recommended order:

```text
1. Photo archive ingestion
2. Reliable semantic retrieval
3. Thumbnails + dates/GPS in mobile UI
4. Automatic Drive photo watcher
5. Restricted write/ingestion API
6. Voice push-to-talk
7. STT/TTS headset path
8. AI router
9. Smart glasses adapter
10. Sensor Stream / Evidence Stream (later)
11. Shared episodic memory (later)
```

The 24/7 Sensor Stream and Collective Episodic Memory are intentionally future branches, not immediate implementation tasks.
