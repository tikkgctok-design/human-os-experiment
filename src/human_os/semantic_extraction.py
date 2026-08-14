"""Versioned derived semantic extraction for canonical Human OS objects."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ids import HUMAN_OS_NAMESPACE
from .schema import ensure_schema
from .semantic_registry import (
    DEFAULT_REGISTRY,
    SEMANTIC_RELATION_TYPES,
    SEMANTIC_STATUSES,
    ExtractionContext,
    ExtractorRegistry,
    SemanticMention,
    SemanticOutput,
)


@dataclass(frozen=True)
class SemanticRunResult:
    semantic_result_id: str
    object_id: str
    extractor_name: str
    extractor_version: str
    semantic_type: str
    source_content_hash: str
    source_blob_id: str | None
    outcome: str
    status: str
    confidence: float | None
    result_json: dict[str, Any] | list[Any] | None
    result_text: str | None
    diagnostics: tuple[str, ...]
    provenance: dict[str, Any]
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _result_id(
    object_id: str,
    extractor_name: str,
    extractor_version: str,
    semantic_type: str,
    source_content_hash: str,
    source_blob_id: str | None,
) -> str:
    value = uuid.uuid5(
        HUMAN_OS_NAMESPACE,
        "semantic:"
        f"{object_id}:{extractor_name}:{extractor_version}:{semantic_type}:"
        f"{source_content_hash}:{source_blob_id or ''}",
    )
    return f"hos_sem_{value.hex}"


def _relation_id(
    semantic_result_id: str, relation_type: str, target_ref: str
) -> str:
    value = uuid.uuid5(
        HUMAN_OS_NAMESPACE,
        f"semantic-relation:{semantic_result_id}:{relation_type}:{target_ref}",
    )
    return f"hos_semrel_{value.hex}"


def _decode_json(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _load_result(
    conn: sqlite3.Connection, semantic_result_id: str, outcome: str
) -> SemanticRunResult | None:
    row = conn.execute(
        """
        SELECT object_id, extractor_name, extractor_version, semantic_type,
               source_content_hash, source_blob_id, status, confidence,
               result_json, result_text, diagnostics_json, provenance_json,
               created_at
        FROM semantic_results WHERE semantic_result_id = ?
        """,
        (semantic_result_id,),
    ).fetchone()
    if row is None:
        return None
    result_json = _decode_json(row[8], None)
    diagnostics = tuple(_decode_json(row[10], []))
    provenance = _decode_json(row[11], {})
    return SemanticRunResult(
        semantic_result_id=semantic_result_id,
        object_id=row[0],
        extractor_name=row[1],
        extractor_version=row[2],
        semantic_type=row[3],
        source_content_hash=row[4],
        source_blob_id=row[5],
        outcome=outcome,
        status=row[6],
        confidence=row[7],
        result_json=result_json,
        result_text=row[9],
        diagnostics=diagnostics,
        provenance=provenance,
        created_at=row[12],
    )


def _failed_output(exc: Exception) -> SemanticOutput:
    return SemanticOutput(
        status="failed",
        diagnostics=(f"extractor_failed:{type(exc).__name__}: {exc}",),
    )


def _validate_output(output: SemanticOutput) -> SemanticOutput:
    if not isinstance(output, SemanticOutput):
        raise TypeError("extractor must return SemanticOutput")
    if output.status not in SEMANTIC_STATUSES:
        raise ValueError(f"invalid semantic status: {output.status}")
    if output.confidence is not None and not 0.0 <= output.confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if output.result_json is not None and not isinstance(output.result_json, (dict, list)):
        raise TypeError("result_json must be a JSON object or array")
    if output.result_json is not None:
        _json(output.result_json)
    if output.result_text is not None and not isinstance(output.result_text, str):
        raise TypeError("result_text must be text")
    if any(not isinstance(item, str) for item in output.diagnostics):
        raise TypeError("diagnostics must contain strings")
    if not isinstance(output.provenance, dict):
        raise TypeError("provenance must be a JSON object")
    _json(output.provenance)
    if output.status == "failed" and not output.diagnostics:
        raise ValueError("failed extraction must include diagnostics")
    for mention in output.mentions:
        if not isinstance(mention, SemanticMention):
            raise TypeError("mentions must contain SemanticMention values")
        if mention.relation_type not in SEMANTIC_RELATION_TYPES - {
            "object_has_semantic_result"
        }:
            raise ValueError(f"unsupported semantic relation: {mention.relation_type}")
        if not mention.target_ref:
            raise ValueError("semantic mention target_ref is required")
        if mention.confidence is not None and not 0.0 <= mention.confidence <= 1.0:
            raise ValueError("mention confidence must be between 0 and 1")
    return output


def _insert_relation(
    conn: sqlite3.Connection,
    *,
    object_id: str,
    semantic_result_id: str,
    relation_type: str,
    target_ref: str,
    confidence: float | None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO semantic_relations (
            semantic_relation_id, object_id, semantic_result_id, relation_type,
            target_ref, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _relation_id(semantic_result_id, relation_type, target_ref),
            object_id,
            semantic_result_id,
            relation_type,
            target_ref,
            confidence,
            created_at,
        ),
    )


def run_semantic_extractor(
    object_id: str,
    extractor_name: str,
    database_path: Path,
    schema_path: Path,
    *,
    extractor_version: str | None = None,
    registry: ExtractorRegistry = DEFAULT_REGISTRY,
) -> SemanticRunResult:
    """Run one extractor without changing RAW, blob identity, or object identity."""
    spec = registry.get(extractor_name, extractor_version)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn, schema_path)
        row = conn.execute(
            """
            SELECT o.object_type, o.source, o.source_id, o.raw_uri,
                   o.content_hash, o.metadata_json, a.blob_id
            FROM objects o
            LEFT JOIN attachments a ON a.object_id = o.object_id
            WHERE o.object_id = ?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"canonical object does not exist: {object_id}")
        (
            object_type,
            source,
            source_id,
            raw_uri,
            content_hash,
            canonical_metadata_json,
            source_blob_id,
        ) = row
        semantic_result_id = _result_id(
            object_id,
            spec.name,
            spec.version,
            spec.semantic_type,
            content_hash,
            source_blob_id,
        )

        existing = _load_result(conn, semantic_result_id, "unchanged")
        if existing is not None:
            with conn:
                conn.execute(
                    """
                    UPDATE semantic_results SET is_current = 0
                    WHERE object_id = ? AND extractor_name = ?
                      AND semantic_type = ? AND semantic_result_id <> ?
                    """,
                    (object_id, spec.name, spec.semantic_type, semantic_result_id),
                )
                conn.execute(
                    "UPDATE semantic_results SET is_current = 1 "
                    "WHERE semantic_result_id = ?",
                    (semantic_result_id,),
                )
            return existing

        metadata_row = None
        if source_blob_id is not None:
            metadata_row = conn.execute(
                """
                SELECT extraction_id, extractor_id, status, metadata_json
                FROM metadata_extractions
                WHERE object_id = ? AND blob_id = ?
                ORDER BY extracted_at DESC, extraction_id DESC LIMIT 1
                """,
                (object_id, source_blob_id),
            ).fetchone()
        derived_metadata = _decode_json(metadata_row[3], None) if metadata_row else None
        context = ExtractionContext(
            object_id=object_id,
            object_type=object_type,
            raw_uri=raw_uri,
            content_hash=content_hash,
            blob_id=source_blob_id,
            canonical_metadata=_decode_json(canonical_metadata_json, {}),
            derived_metadata=derived_metadata,
        )
        if object_type not in spec.object_types:
            output = SemanticOutput(
                status="unsupported",
                diagnostics=(
                    f"extractor_not_supported_for_object_type:{object_type}",
                ),
            )
        else:
            try:
                output = _validate_output(spec.handler(context))
            except Exception as exc:
                output = _failed_output(exc)

        created_at = _now()
        provenance = {
            "canonical_object": {
                "object_id": object_id,
                "object_type": object_type,
                "source": source,
                "source_id": source_id,
                "raw_uri": raw_uri,
            },
            "extractor": {
                "name": spec.name,
                "version": spec.version,
                "runtime": output.provenance,
            },
            "source": {
                "blob_id": source_blob_id,
                "content_hash": content_hash,
                "metadata_extraction_id": metadata_row[0] if metadata_row else None,
                "metadata_extractor_id": metadata_row[1] if metadata_row else None,
                "metadata_status": metadata_row[2] if metadata_row else None,
            },
        }
        result_json_text = _json(output.result_json) if output.result_json is not None else None
        with conn:
            conn.execute(
                """
                UPDATE semantic_results SET is_current = 0
                WHERE object_id = ? AND extractor_name = ? AND semantic_type = ?
                """,
                (object_id, spec.name, spec.semantic_type),
            )
            conn.execute(
                """
                INSERT INTO semantic_results (
                    semantic_result_id, object_id, source_blob_id,
                    source_content_hash, extractor_name, extractor_version,
                    semantic_type, status, confidence, result_json, result_text,
                    diagnostics_json, provenance_json, is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    semantic_result_id,
                    object_id,
                    source_blob_id,
                    content_hash,
                    spec.name,
                    spec.version,
                    spec.semantic_type,
                    output.status,
                    output.confidence,
                    result_json_text,
                    output.result_text,
                    _json(list(output.diagnostics)),
                    _json(provenance),
                    created_at,
                ),
            )
            _insert_relation(
                conn,
                object_id=object_id,
                semantic_result_id=semantic_result_id,
                relation_type="object_has_semantic_result",
                target_ref=semantic_result_id,
                confidence=output.confidence,
                created_at=created_at,
            )
            for mention in output.mentions:
                _insert_relation(
                    conn,
                    object_id=object_id,
                    semantic_result_id=semantic_result_id,
                    relation_type=mention.relation_type,
                    target_ref=mention.target_ref,
                    confidence=mention.confidence,
                    created_at=created_at,
                )
        inserted = _load_result(conn, semantic_result_id, "inserted")
        if inserted is None:
            raise RuntimeError("semantic result insert did not persist")
        return inserted
    finally:
        conn.close()
