# Roadmap

## Milestone 0 — Repository foundation

- [x] Create public repository.
- [x] Connect ChatGPT and GitHub Desktop.
- [x] Add privacy-focused `.gitignore` and `SECURITY.md`.
- [x] Publish initial architecture.

## Milestone 1 — Lossless ChatGPT ingestion

- [ ] Fix converter handling for `reasoning_recap` and `thoughts`.
- [ ] Finalize `source_id`, `object_id`, and `event_id` formats.
- [ ] Create canonical SQLite INDEX.
- [ ] Import conversations, nodes/messages, attachments, parent links, timestamps and source IDs.
- [ ] Make ingestion idempotent.
- [ ] Validate referential integrity and duplicate handling.

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
