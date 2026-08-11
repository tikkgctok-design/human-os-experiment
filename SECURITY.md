# Security and privacy

Human OS is designed around sensitive personal data. This public repository must never contain private RAW life data.

## Never commit

- official ChatGPT exports or other personal account exports;
- private conversations;
- personal photographs, video, audio, medical or financial records;
- production SQLite databases or indexes built from real personal data;
- API keys, tokens, passwords, credentials or private URLs;
- local filesystem paths that expose personal information.

Use synthetic or explicitly anonymized examples only.

## Architectural principle

RAW data remains under the person's control. Public code and schemas may describe how Human OS processes data, but the public repository is not the storage layer for a person's life archive.
