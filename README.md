# Human OS Experiment

Model-independent, reconstructible personal memory for replaceable AI models.

This repository is the public technical workspace for the Human OS experiment.

> Status: Milestone 1 — versioned, diagnostic ChatGPT ingestion.

## Implemented

- deterministic UUIDv5 identities for source objects and structural relations;
- SQLite index for conversations, mapping nodes, messages and parent links;
- idempotent imports with explicit inserted/unchanged/changed/conflict counts;
- import-run audit log, object version history and non-fatal diagnostics;
- automatic migration of the original v1 structural index to schema v2;
- synthetic regression tests for repeat imports, changed objects and malformed data.

Run an import from an installed development environment:

```console
python -m human_os.chatgpt_import conversations.json private/human_os.db
```

RAW exports and generated databases are private local data and are ignored by Git.
