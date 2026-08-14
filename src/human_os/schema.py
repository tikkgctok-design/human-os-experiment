"""Canonical SQLite schema creation and migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def ensure_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    """Create the canonical schema or migrate an existing index to v5."""
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
        CREATE TABLE IF NOT EXISTS semantic_results (
            semantic_result_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            source_blob_id TEXT,
            source_content_hash TEXT NOT NULL,
            extractor_name TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            semantic_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('complete', 'partial', 'unsupported', 'failed')
            ),
            confidence REAL CHECK (
                confidence IS NULL OR
                (confidence >= 0.0 AND confidence <= 1.0)
            ),
            result_json TEXT,
            result_text TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            provenance_json TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
            created_at DATETIME NOT NULL,
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (source_blob_id) REFERENCES blobs(blob_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_results_identity
            ON semantic_results (
                object_id, extractor_name, extractor_version, semantic_type,
                source_content_hash, COALESCE(source_blob_id, '')
            );
        CREATE INDEX IF NOT EXISTS idx_semantic_results_object
            ON semantic_results(object_id);
        CREATE INDEX IF NOT EXISTS idx_semantic_results_current
            ON semantic_results(object_id, semantic_type, is_current);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_results_one_current
            ON semantic_results(object_id, extractor_name, semantic_type)
            WHERE is_current = 1;
        CREATE TABLE IF NOT EXISTS semantic_relations (
            semantic_relation_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            semantic_result_id TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK (
                relation_type IN (
                    'object_has_semantic_result', 'semantic_mentions_entity',
                    'semantic_mentions_place', 'semantic_mentions_person',
                    'semantic_mentions_topic'
                )
            ),
            target_ref TEXT NOT NULL DEFAULT '',
            confidence REAL CHECK (
                confidence IS NULL OR
                (confidence >= 0.0 AND confidence <= 1.0)
            ),
            created_at DATETIME NOT NULL,
            UNIQUE (semantic_result_id, relation_type, target_ref),
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (semantic_result_id)
                REFERENCES semantic_results(semantic_result_id)
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_relations_object
            ON semantic_relations(object_id);
        CREATE INDEX IF NOT EXISTS idx_semantic_relations_result
            ON semantic_relations(semantic_result_id);
        CREATE INDEX IF NOT EXISTS idx_semantic_relations_type
            ON semantic_relations(relation_type);
        CREATE TABLE IF NOT EXISTS event_evidence (
            event_evidence_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            semantic_result_id TEXT,
            evidence_type TEXT NOT NULL CHECK (
                evidence_type IN (
                    'time', 'gps', 'visual_similarity', 'person',
                    'text_context', 'temporal_neighbor'
                )
            ),
            evidence_json TEXT NOT NULL,
            confidence REAL CHECK (
                confidence IS NULL OR
                (confidence >= 0.0 AND confidence <= 1.0)
            ),
            created_at DATETIME NOT NULL,
            FOREIGN KEY (object_id) REFERENCES objects(object_id),
            FOREIGN KEY (semantic_result_id)
                REFERENCES semantic_results(semantic_result_id)
        );
        CREATE INDEX IF NOT EXISTS idx_event_evidence_object
            ON event_evidence(object_id);
        CREATE INDEX IF NOT EXISTS idx_event_evidence_type
            ON event_evidence(evidence_type);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _now()),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
