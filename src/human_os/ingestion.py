"""Universal identity-first ingestion into the canonical Human OS INDEX."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .ids import blob_id as make_blob_id
from .ids import location_id as make_location_id
from .ids import object_id as make_object_id
from .schema import ensure_schema

SUPPORTED_OBJECT_TYPES = frozenset({"photo", "video", "audio", "note", "message"})
BINARY_OBJECT_TYPES = frozenset({"photo", "video", "audio"})


@dataclass(frozen=True)
class IngestObject:
    object_type: str
    source: str
    raw_uri: str
    source_id: str | None = None
    content: bytes | None = None
    occurred_at: str | None = None
    captured_at: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    object_id: str
    outcome: str
    content_hash: str
    blob_id: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_digest(item: IngestObject) -> tuple[str, int]:
    if item.content is not None:
        return hashlib.sha256(item.content).hexdigest(), len(item.content)
    parsed = urlparse(item.raw_uri)
    if parsed.scheme != "file":
        raise ValueError("content is required when raw_uri is not a file URI")
    path_text = unquote(parsed.path)
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    if len(path_text) >= 3 and path_text[0] == "/" and path_text[2] == ":":
        path_text = path_text[1:]
    digest = hashlib.sha256()
    byte_size = 0
    with Path(path_text).open("rb") as raw:
        while chunk := raw.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    return digest.hexdigest(), byte_size


def _insert_version(
    conn: sqlite3.Connection,
    *,
    object_id: str,
    import_id: str,
    item: IngestObject,
    captured_at: str,
    content_hash: str,
    metadata_json: str,
    refresh_known: bool,
) -> None:
    known = conn.execute(
        "SELECT 1 FROM object_versions WHERE object_id = ? AND content_hash = ?",
        (object_id, content_hash),
    ).fetchone()
    if known:
        if refresh_known:
            conn.execute(
                """
                UPDATE object_versions SET object_type = ?, occurred_at = ?,
                    captured_at = ?, raw_uri = ?, mime_type = ?, metadata_json = ?
                WHERE object_id = ? AND content_hash = ?
                """,
                (
                    item.object_type,
                    item.occurred_at,
                    captured_at,
                    item.raw_uri,
                    item.mime_type,
                    metadata_json,
                    object_id,
                    content_hash,
                ),
            )
        return
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM object_versions WHERE object_id = ?",
        (object_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO object_versions (
            object_id, version, import_id, object_type, occurred_at, captured_at,
            parent_id, raw_uri, content_hash, mime_type, topic, metadata_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?)
        """,
        (
            object_id,
            version,
            import_id,
            item.object_type,
            item.occurred_at,
            captured_at,
            item.raw_uri,
            content_hash,
            item.mime_type,
            metadata_json,
            captured_at,
        ),
    )


def ingest_object(
    item: IngestObject,
    database_path: Path,
    schema_path: Path,
) -> IngestResult:
    """Ingest one source object without modifying its RAW representation."""
    if item.object_type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(f"Unsupported object_type: {item.object_type}")
    if not item.source or not item.raw_uri:
        raise ValueError("source and raw_uri are required")
    if not isinstance(item.metadata, dict):
        raise ValueError("metadata must be a JSON object")

    content_hash, byte_size = _content_digest(item)
    source_id = item.source_id or item.raw_uri
    object_id = make_object_id(item.source, source_id)
    captured_at = item.captured_at or _now()
    metadata_json = json.dumps(
        item.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    import_id = f"hos_imp_{uuid.uuid4().hex}"
    attached_blob_id = (
        make_blob_id(content_hash) if item.object_type in BINARY_OBJECT_TYPES else None
    )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn, schema_path)
        started_at = _now()
        with conn:
            conn.execute(
                """
                INSERT INTO import_runs (
                    import_id, source, raw_uri, source_hash, started_at, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (import_id, item.source, item.raw_uri, content_hash, started_at),
            )
            existing = conn.execute(
                """
                SELECT object_id, content_hash, object_type, raw_uri, mime_type,
                       metadata_json, occurred_at
                FROM objects
                """
                "WHERE source = ? AND source_id = ?",
                (item.source, source_id),
            ).fetchone()
            if existing and existing[0] != object_id:
                raise ValueError("source identity conflicts with deterministic object_id")
            if not existing:
                conn.execute(
                    """
                    INSERT INTO objects (
                        object_id, object_type, source, source_id, occurred_at,
                        captured_at, parent_id, raw_uri, content_hash, mime_type,
                        topic, event_id, confidence, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        object_id,
                        item.object_type,
                        item.source,
                        source_id,
                        item.occurred_at,
                        captured_at,
                        item.raw_uri,
                        content_hash,
                        item.mime_type,
                        metadata_json,
                        captured_at,
                    ),
                )
                outcome = "inserted"
            elif existing[1:] == (
                content_hash,
                item.object_type,
                item.raw_uri,
                item.mime_type,
                metadata_json,
                item.occurred_at,
            ):
                outcome = "unchanged"
            else:
                conn.execute(
                    """
                    UPDATE objects SET object_type = ?, occurred_at = ?,
                        captured_at = ?, raw_uri = ?, content_hash = ?,
                        mime_type = ?, metadata_json = ?
                    WHERE object_id = ?
                    """,
                    (
                        item.object_type,
                        item.occurred_at,
                        captured_at,
                        item.raw_uri,
                        content_hash,
                        item.mime_type,
                        metadata_json,
                        object_id,
                    ),
                )
                outcome = "changed"

            _insert_version(
                conn,
                object_id=object_id,
                import_id=import_id,
                item=item,
                captured_at=captured_at,
                content_hash=content_hash,
                metadata_json=metadata_json,
                refresh_known=outcome == "changed",
            )

            if attached_blob_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO blobs (
                        blob_id, sha256, byte_size, mime_type, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        attached_blob_id,
                        content_hash,
                        byte_size,
                        item.mime_type,
                        captured_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO attachments (object_id, blob_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(object_id) DO UPDATE SET blob_id = excluded.blob_id
                    """,
                    (object_id, attached_blob_id, captured_at),
                )
                conn.execute(
                    """
                    INSERT INTO blob_locations (
                        location_id, object_id, blob_id, raw_uri, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(object_id, raw_uri)
                    DO UPDATE SET blob_id = excluded.blob_id
                    """,
                    (
                        make_location_id(object_id, item.raw_uri),
                        object_id,
                        attached_blob_id,
                        item.raw_uri,
                        captured_at,
                    ),
                )

            completed_at = _now()
            stats = json.dumps({"outcome": outcome}, sort_keys=True)
            conn.execute(
                """
                UPDATE import_runs SET status = 'complete', completed_at = ?,
                    stats_json = ? WHERE import_id = ?
                """,
                (completed_at, stats, import_id),
            )
        return IngestResult(object_id, outcome, content_hash, attached_blob_id)
    finally:
        conn.close()
