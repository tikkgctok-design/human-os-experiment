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
