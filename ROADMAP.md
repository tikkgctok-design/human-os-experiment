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

## Milestone 2 — Retrieval proof

- [ ] Implement 5–10 real retrieval queries.
- [ ] Return provenance from retrieved claims back to source objects.
- [ ] Demonstrate chronology and event-linked retrieval, not only semantic similarity.

## Milestone 3 — Photo archive

- [ ] Ingest photo metadata and hashes.
- [ ] Extract EXIF time, GPS and device metadata where available.
- [ ] Add duplicate detection and similarity clustering.
- [ ] Add selective visual analysis.
- [ ] Link photos to chats and reconstructed events.

## Milestone 4 — Episodes and reconstructible memory

- [ ] Build EPISODES from linked objects.
- [ ] Generate rebuildable SUMMARY and MASTER layers.
- [ ] Create request-specific CURRENT context for replaceable models.
