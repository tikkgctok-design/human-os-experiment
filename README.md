# Human OS Experiment

Model-independent, reconstructible personal memory for replaceable AI models.

This repository is the public technical workspace for the Human OS experiment.

> Status: Milestone 1 — canonical identity-first ingestion foundation.

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

Run an import from an installed development environment:

```console
python -m human_os.chatgpt_import conversations.json private/human_os.db
```

RAW exports and generated databases are private local data and are ignored by Git.
