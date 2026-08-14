"""Controlled real-photo validation runner for the PHOTO semantic layer."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from .photo_semantic import (
    PHOTO_EXTRACTORS,
    PhotoVisionBackend,
    ProductionPhotoBackend,
    run_photo_semantics,
)
from .schema import ensure_schema


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_from_uri(raw_uri: str) -> Path:
    parsed = urlparse(raw_uri)
    if parsed.scheme != "file":
        raise ValueError("PHOTO validation requires file-backed RAW objects")
    text = unquote(parsed.path)
    if parsed.netloc:
        text = f"//{parsed.netloc}{text}"
    if len(text) >= 3 and text[0] == "/" and text[2] == ":":
        text = text[1:]
    return Path(text)


def _snapshot(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as raw:
        while chunk := raw.read(1024 * 1024):
            digest.update(chunk)
    stat = path.stat()
    return {
        "sha256": digest.hexdigest(),
        "byte_size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "objects",
            "blobs",
            "blob_locations",
            "attachments",
            "relations",
            "object_versions",
            "metadata_extractions",
            "semantic_results",
            "semantic_relations",
            "event_evidence",
        )
    }


def _load_objects(
    conn: sqlite3.Connection, object_ids: Sequence[str]
) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for object_id in object_ids:
        row = conn.execute(
            """
            SELECT o.object_id, o.object_type, o.raw_uri, o.occurred_at,
                   o.captured_at, o.content_hash, a.blob_id
            FROM objects o
            LEFT JOIN attachments a ON a.object_id = o.object_id
            WHERE o.object_id = ?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"canonical object does not exist: {object_id}")
        if row[1] != "photo" or row[6] is None:
            raise ValueError(f"object is not an attached photo: {object_id}")
        loaded.append(
            {
                "object_id": row[0],
                "object_type": row[1],
                "raw_uri": row[2],
                "raw_path": str(_path_from_uri(row[2])),
                "occurred_at": row[3],
                "captured_at": row[4],
                "content_hash": row[5],
                "blob_id": row[6],
            }
        )
    return loaded


def _semantic_payload(conn: sqlite3.Connection, object_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    rows = conn.execute(
        """
        SELECT semantic_result_id, extractor_name, extractor_version,
               semantic_type, status, confidence, result_json, result_text,
               diagnostics_json, provenance_json
        FROM semantic_results
        WHERE object_id = ? AND is_current = 1
          AND extractor_name IN (?, ?, ?, ?)
        ORDER BY extractor_name
        """,
        (object_id, *PHOTO_EXTRACTORS),
    ).fetchall()
    for row in rows:
        relations = conn.execute(
            """
            SELECT relation_type, target_ref, confidence
            FROM semantic_relations WHERE semantic_result_id = ?
            ORDER BY relation_type, target_ref
            """,
            (row[0],),
        ).fetchall()
        payload[row[1]] = {
            "semantic_result_id": row[0],
            "extractor_version": row[2],
            "semantic_type": row[3],
            "status": row[4],
            "confidence": row[5],
            "result_json": json.loads(row[6]) if row[6] else None,
            "result_text": row[7],
            "diagnostics": json.loads(row[8]),
            "provenance": json.loads(row[9]),
            "relations": [
                {
                    "relation_type": relation[0],
                    "target_ref": relation[1],
                    "confidence": relation[2],
                }
                for relation in relations
            ],
        }
    return payload


def validate_photo_sample(
    object_ids: Sequence[str],
    database_path: Path,
    schema_path: Path,
    output_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> dict[str, Any]:
    """Validate a bounded sample twice and persist private reproducible evidence."""
    if not object_ids:
        raise ValueError("at least one object_id is required")
    if len(set(object_ids)) != len(object_ids):
        raise ValueError("validation object_ids must be unique")
    backend = backend or ProductionPhotoBackend()
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn, schema_path)
        conn.commit()
        objects = _load_objects(conn, object_ids)
        counts_before = _counts(conn)

    raw_before = {
        item["object_id"]: _snapshot(Path(item["raw_path"])) for item in objects
    }
    for item in objects:
        if raw_before[item["object_id"]]["sha256"] != item["content_hash"]:
            raise ValueError(
                f"RAW hash differs from canonical content_hash: {item['object_id']}"
            )

    first_ids: dict[str, dict[str, str]] = {}
    started = perf_counter()
    for item in objects:
        results = run_photo_semantics(
            item["object_id"], database_path, schema_path, backend=backend
        )
        first_ids[item["object_id"]] = {
            name: result.semantic_result_id for name, result in results.items()
        }
    first_duration = round(perf_counter() - started, 3)
    with sqlite3.connect(database_path) as conn:
        counts_after_first = _counts(conn)

    repeat_ids: dict[str, dict[str, str]] = {}
    started = perf_counter()
    for item in objects:
        results = run_photo_semantics(
            item["object_id"], database_path, schema_path, backend=backend
        )
        repeat_ids[item["object_id"]] = {
            name: result.semantic_result_id for name, result in results.items()
        }
        if not all(result.outcome == "unchanged" for result in results.values()):
            raise RuntimeError(f"semantic repeat was not idempotent: {item['object_id']}")
    repeat_duration = round(perf_counter() - started, 3)
    if repeat_ids != first_ids:
        raise RuntimeError("semantic_result_id changed during repeat validation")

    raw_after = {
        item["object_id"]: _snapshot(Path(item["raw_path"])) for item in objects
    }
    raw_unchanged = raw_after == raw_before
    if not raw_unchanged:
        raise RuntimeError("RAW content or timestamps changed during validation")

    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        counts_after_repeat = _counts(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        records = []
        for item in objects:
            metadata = conn.execute(
                """
                SELECT extraction_id, status, metadata_json, occurred_at,
                       occurred_at_source, extracted_at
                FROM metadata_extractions
                WHERE object_id = ? AND blob_id = ?
                ORDER BY extracted_at DESC LIMIT 1
                """,
                (item["object_id"], item["blob_id"]),
            ).fetchone()
            records.append(
                {
                    **item,
                    "raw_snapshot": raw_after[item["object_id"]],
                    "metadata": (
                        {
                            "extraction_id": metadata[0],
                            "status": metadata[1],
                            "result": json.loads(metadata[2]),
                            "occurred_at": metadata[3],
                            "occurred_at_source": metadata[4],
                            "extracted_at": metadata[5],
                        }
                        if metadata
                        else None
                    ),
                    "semantics": _semantic_payload(conn, item["object_id"]),
                }
            )

    report = {
        "report_version": "1.0.0",
        "generated_at": _now(),
        "database": str(database_path.resolve()),
        "sample_size": len(objects),
        "extractors": list(PHOTO_EXTRACTORS),
        "counts": {
            "before": counts_before,
            "after_first": counts_after_first,
            "after_repeat": counts_after_repeat,
        },
        "durations_seconds": {
            "first_run": first_duration,
            "idempotent_repeat": repeat_duration,
        },
        "checks": {
            "raw_unchanged": raw_unchanged,
            "semantic_result_ids_stable": repeat_ids == first_ids,
            "counts_stable_on_repeat": counts_after_repeat == counts_after_first,
            "integrity_check": [list(row) for row in integrity],
            "foreign_key_check": [list(row) for row in foreign_keys],
            "person_identity_relations": sum(
                1
                for item in records
                for semantic in item["semantics"].values()
                for relation in semantic["relations"]
                if relation["relation_type"] == "semantic_mentions_person"
            ),
        },
        "objects": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", type=Path, help="JSON array of canonical object_ids")
    parser.add_argument("database", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    object_ids = json.loads(args.selection.read_text(encoding="utf-8"))
    validate_photo_sample(object_ids, args.database, args.schema, args.output)


if __name__ == "__main__":
    main()
