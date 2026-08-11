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
