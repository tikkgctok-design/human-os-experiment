# Roadmap

## Milestone 0 — Repository foundation

- [x] Create public repository.
- [x] Connect ChatGPT and GitHub Desktop.
- [x] Add privacy-focused `.gitignore` and `SECURITY.md`.
- [x] Publish initial architecture.

## Milestone 1 — Lossless ChatGPT ingestion

> Baseline foundation completed on 2026-08-11: stable identities, schema v2,
> versioned idempotent imports, diagnostics and migration coverage are in place.
> Attachment indexing and content normalization remain before the milestone is closed.

- [x] Preserve `reasoning_recap` and `thoughts` structurally and report them as not yet normalized.
- [ ] Finalize `source_id` and `event_id` formats (`object_id` v0.1 is stable).
- [x] Create canonical versioned SQLite INDEX with v1-to-v2 migration.
- [ ] Import conversations, nodes/messages, attachments, parent links, timestamps and source IDs (attachments remain).
- [x] Make ingestion idempotent and retain changed object versions.
- [x] Validate referential integrity, duplicate handling and non-fatal diagnostics.
- [x] Add canonical identity-first ingestion for photo, video, audio, note and message.
- [x] Add schema v3 attachments, deduplicated blobs and idempotent RAW locations.
- [ ] Normalize special ChatGPT content and attachment references.
- [x] Add a derived, idempotent EXIF/video metadata extraction layer.
- [x] Populate `occurred_at` only from high-confidence timezone-aware metadata.

## INDEX v5 — Semantic extraction layer v1

- [x] Add versioned, idempotent semantic results tied to canonical object and source
  content identity.
- [x] Add a typed extractor registry for photo, video, audio, note and message.
- [x] Preserve complete/partial/unsupported/failed outcomes with diagnostics and
  provenance.
- [x] Add derived semantic relations without manufacturing canonical object IDs.
- [x] Prepare event evidence for time, GPS, visual similarity, people, text context
  and temporal-neighbor signals without implementing clustering.
- [x] Validate the local pipeline with five synthetic object types.
- [x] Add pinned local Florence-2 caption/OCR and DETR object-detection adapters.
- [x] Validate PHOTO semantics on a controlled 12-photo real RAW sample.
- [ ] Replace or supplement Florence OCR with a Cyrillic-capable implementation.
- [ ] Define human-review states and precision gates for caption/object candidates.
- [ ] Benchmark a faster detector/runtime before any archive-wide semantic run.
- [ ] Select and benchmark local speech-to-text and audio/video summarization.
- [ ] Add model artifact identity, runtime parameters and reproducibility manifests.
- [ ] Promote reviewed people/place/topic candidates into a canonical graph.
- [ ] Implement event clustering only after evidence quality and review workflow exist.

## Milestone 2 — Retrieval proof

- [x] Add a deterministic read-only retrieval service over current semantic evidence.
- [x] Add CLI and localhost-only JSON transports without external inference.
- [x] Validate the first four real PHOTO queries, including explicit best-effort limits.
- [x] Return provenance from retrieved claims back to source objects.
- [ ] Expand validation to 5–10 real retrieval queries.
- [ ] Demonstrate chronology and event-linked retrieval, not only semantic similarity.

## Milestone 3 — Photo archive

- [ ] Ingest the photo archive using the canonical identity layer (hashing and metadata
  storage primitives are implemented).
- [x] Extract EXIF/video time, GPS, dimensions, camera and codec metadata with diagnostics.
- [ ] Add duplicate detection and similarity clustering.
- [ ] Add selective visual analysis.
- [ ] Link photos to chats and reconstructed events.

## Milestone 4 — Episodes and reconstructible memory

- [ ] Build EPISODES from linked objects.
- [ ] Generate rebuildable SUMMARY and MASTER layers.
- [ ] Create request-specific CURRENT context for replaceable models.
