"""Canonical SQLite schema creation and migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def ensure_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    """Create the canonical schema or migrate an existing index to v4."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
    ).fetchone()
    if not exists:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        return

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_runs (
            import_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            raw_uri TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            started_at DATETIME NOT NULL,
            completed_at DATETIME,
            status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
            stats_json TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_import_runs_source_hash
            ON import_runs(source, source_hash);
        CREATE TABLE IF NOT EXISTS object_versions (
            object_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            import_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            occurred_at DATETIME,
            captured_at DATETIME NOT NULL,
            parent_id TEXT,
            raw_uri TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mime_type TEXT,
            topic TEXT,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (object_id, version),
            UNIQUE (object_id, content_hash),
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (import_id) REFERENCES import_runs(import_id),
            FOREIGN KEY (parent_id) REFERENCES objects(object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_object_versions_import
            ON object_versions(import_id);
        CREATE INDEX IF NOT EXISTS idx_object_versions_hash
            ON object_versions(content_hash);
        CREATE TABLE IF NOT EXISTS import_diagnostics (
            diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
            code TEXT NOT NULL,
            source_id TEXT,
            detail TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (import_id) REFERENCES import_runs(import_id)
        );
        CREATE INDEX IF NOT EXISTS idx_import_diagnostics_import
            ON import_diagnostics(import_id);
        """
    )

    if not _has_column(conn, "objects", "metadata_json"):
        conn.execute(
            "ALTER TABLE objects ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
    if not _has_column(conn, "object_versions", "metadata_json"):
        conn.execute(
            "ALTER TABLE object_versions ADD COLUMN metadata_json "
            "TEXT NOT NULL DEFAULT '{}'"
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS blobs (
            blob_id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            mime_type TEXT,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
            object_id TEXT PRIMARY KEY,
            blob_id TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (blob_id) REFERENCES blobs(blob_id)
        );
        CREATE INDEX IF NOT EXISTS idx_attachments_blob ON attachments(blob_id);
        CREATE TABLE IF NOT EXISTS blob_locations (
            location_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            blob_id TEXT NOT NULL,
            raw_uri TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            UNIQUE (object_id, raw_uri),
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (blob_id) REFERENCES blobs(blob_id)
        );
        CREATE INDEX IF NOT EXISTS idx_blob_locations_blob
            ON blob_locations(blob_id);
        CREATE INDEX IF NOT EXISTS idx_blob_locations_object
            ON blob_locations(object_id);
        CREATE TABLE IF NOT EXISTS metadata_extractions (
            extraction_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            blob_id TEXT NOT NULL,
            extractor_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('complete', 'partial', 'failed')
            ),
            metadata_json TEXT NOT NULL,
            occurred_at TEXT,
            occurred_at_source TEXT,
            occurred_at_confidence TEXT NOT NULL CHECK (
                occurred_at_confidence IN ('high', 'none')
            ),
            extracted_at DATETIME NOT NULL,
            UNIQUE (object_id, blob_id, extractor_id),
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (blob_id) REFERENCES blobs(blob_id)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_extractions_object
            ON metadata_extractions(object_id);
        CREATE INDEX IF NOT EXISTS idx_metadata_extractions_blob
            ON metadata_extractions(blob_id);
        CREATE TABLE IF NOT EXISTS metadata_diagnostics (
            diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
            code TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            UNIQUE (extraction_id, code, detail),
            FOREIGN KEY (extraction_id)
                REFERENCES metadata_extractions(extraction_id)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_diagnostics_extraction
            ON metadata_diagnostics(extraction_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _now()),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
