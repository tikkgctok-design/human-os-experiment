"""Import the official ChatGPT conversations.json structure into Human OS SQLite.

This first importer is intentionally structural: it registers conversations,
mapping nodes, messages, parent edges, and containment relations without asking
an AI model whether an object is important. RAW remains external and immutable;
the database stores stable identities plus pointers back to RAW.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ids import object_id, relation_id

SOURCE_CONVERSATION = "chatgpt.conversation"
SOURCE_NODE = "chatgpt.node"
SOURCE_MESSAGE = "chatgpt.message"
SCHEMA_VERSION = 2


def _iso_from_unix(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
    ).fetchone()
    if not exists:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        return
    _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Upgrade a v1 structural index in place without rewriting its objects."""
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
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _now()),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _upsert_object(
    conn: sqlite3.Connection,
    *,
    object_id_value: str,
    object_type: str,
    source: str,
    source_id: str,
    occurred_at: str | None,
    captured_at: str,
    parent_id: str | None,
    raw_uri: str,
    content_hash: str,
    import_id: str,
    topic: str | None = None,
) -> str:
    existing = conn.execute(
        "SELECT object_id, content_hash FROM objects WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    if existing and existing[0] != object_id_value:
        return "conflict"
    if existing and existing[1] == content_hash:
        version_exists = conn.execute(
            "SELECT 1 FROM object_versions WHERE object_id = ? AND content_hash = ?",
            (object_id_value, content_hash),
        ).fetchone()
        if not version_exists:
            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM object_versions WHERE object_id = ?",
                (object_id_value,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO object_versions (
                    object_id, version, import_id, object_type, occurred_at,
                    captured_at, parent_id, raw_uri, content_hash, mime_type,
                    topic, created_at
                ) SELECT object_id, ?, ?, object_type, occurred_at, captured_at,
                    parent_id, raw_uri, content_hash, mime_type, topic, created_at
                  FROM objects WHERE object_id = ?
                """,
                (next_version, import_id, object_id_value),
            )
        return "unchanged"

    if existing:
        conn.execute(
            """
            UPDATE objects SET object_type = ?, occurred_at = ?, captured_at = ?,
                raw_uri = ?, content_hash = ?, mime_type = ?, topic = ?
            WHERE object_id = ?
            """,
            (
                object_type,
                occurred_at,
                captured_at,
                raw_uri,
                content_hash,
                "application/json",
                topic,
                object_id_value,
            ),
        )
        outcome = "changed"
    else:
        conn.execute(
            """
            INSERT INTO objects (
                object_id, object_type, source, source_id, occurred_at, captured_at,
                parent_id, raw_uri, content_hash, mime_type, topic, event_id,
                confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                object_id_value,
                object_type,
                source,
                source_id,
                occurred_at,
                captured_at,
                parent_id,
                raw_uri,
                content_hash,
                "application/json",
                topic,
                captured_at,
            ),
        )
        outcome = "inserted"

    known_version = conn.execute(
        "SELECT 1 FROM object_versions WHERE object_id = ? AND content_hash = ?",
        (object_id_value, content_hash),
    ).fetchone()
    if not known_version:
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM object_versions WHERE object_id = ?",
            (object_id_value,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO object_versions (
                object_id, version, import_id, object_type, occurred_at, captured_at,
                parent_id, raw_uri, content_hash, mime_type, topic, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id_value,
                next_version,
                import_id,
                object_type,
                occurred_at,
                captured_at,
                parent_id,
                raw_uri,
                content_hash,
                "application/json",
                topic,
                captured_at,
            ),
        )
    return outcome


def _insert_relation(
    conn: sqlite3.Connection,
    *,
    relation_type: str,
    from_object_id: str,
    to_object_id: str,
    created_at: str,
) -> str:
    rid = relation_id(relation_type, from_object_id, to_object_id)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO relations (
            relation_id, from_object_id, to_object_id, relation_type,
            confidence, model_id, created_at
        ) VALUES (?, ?, ?, ?, 1.0, 'deterministic-importer-v0.1', ?)
        """,
        (rid, from_object_id, to_object_id, relation_type, created_at),
    )
    return "inserted" if cursor.rowcount else "unchanged"


def _diagnostic(
    conn: sqlite3.Connection,
    import_id: str,
    code: str,
    detail: str,
    created_at: str,
    source_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO import_diagnostics (
            import_id, severity, code, source_id, detail, created_at
        ) VALUES (?, 'warning', ?, ?, ?, ?)
        """,
        (import_id, code, source_id, detail, created_at),
    )


def _iter_conversations(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        conversations = payload.get("conversations")
        if isinstance(conversations, list):
            for item in conversations:
                if isinstance(item, dict):
                    yield item
            return
    raise ValueError(
        "Expected official ChatGPT conversations JSON as a list or "
        "{'conversations': [...]} object"
    )


def import_chatgpt_export(
    conversations_path: Path,
    database_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    payload = json.loads(conversations_path.read_text(encoding="utf-8"))
    captured_at = _now()
    raw_base = conversations_path.resolve().as_uri()
    source_hash = _sha256_json(payload)
    import_id = f"hos_imp_{uuid.uuid4().hex}"

    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_schema(conn, schema_path)
        conn.execute(
            """
            INSERT INTO import_runs (
                import_id, source, raw_uri, source_hash, started_at, status
            ) VALUES (?, 'chatgpt', ?, ?, ?, 'running')
            """,
            (import_id, raw_base, source_hash, captured_at),
        )
        conn.commit()

        stats: dict[str, Any] = {
            "import_id": import_id,
            "conversations": 0,
            "nodes": 0,
            "messages": 0,
            "relations": 0,
            "objects_inserted": 0,
            "objects_unchanged": 0,
            "objects_changed": 0,
            "conflicts": 0,
            "relations_inserted": 0,
            "relations_unchanged": 0,
            "relations_removed": 0,
            "warnings": 0,
        }

        def count_object(outcome: str) -> None:
            key = {
                "inserted": "objects_inserted",
                "unchanged": "objects_unchanged",
                "changed": "objects_changed",
                "conflict": "conflicts",
            }[outcome]
            stats[key] += 1

        def count_relation(outcome: str) -> None:
            stats[f"relations_{outcome}"] += 1
            stats["relations"] += 1

        def warn(code: str, detail: str, source_id: str | None = None) -> None:
            _diagnostic(conn, import_id, code, detail, captured_at, source_id)
            stats["warnings"] += 1

        try:
            conn.execute("BEGIN")
            for conversation in _iter_conversations(payload):
                conv_source_id = str(
                    conversation.get("conversation_id") or conversation.get("id") or ""
                )
                if not conv_source_id:
                    warn(
                        "conversation_missing_id",
                        "Conversation was skipped because it has no conversation_id/id",
                    )
                    continue

                conv_oid = object_id(SOURCE_CONVERSATION, conv_source_id)
                outcome = _upsert_object(
                    conn,
                    object_id_value=conv_oid,
                    object_type="chatgpt_conversation",
                    source=SOURCE_CONVERSATION,
                    source_id=conv_source_id,
                    occurred_at=_iso_from_unix(conversation.get("create_time")),
                    captured_at=captured_at,
                    parent_id=None,
                    raw_uri=f"{raw_base}#conversation={conv_source_id}",
                    content_hash=_sha256_json(conversation),
                    import_id=import_id,
                    topic=None,
                )
                count_object(outcome)
                stats["conversations"] += 1

                mapping = conversation.get("mapping") or {}
                if not isinstance(mapping, dict):
                    warn(
                        "invalid_mapping",
                        "Conversation mapping is not an object and was skipped",
                        conv_source_id,
                    )
                    continue

                node_oids = {
                    str(node_id): object_id(SOURCE_NODE, str(node_id))
                    for node_id in mapping.keys()
                }

                # Insert all nodes before applying parent links. Mapping order is not
                # guaranteed to be parent-first in an official export.
                for node_id, node in mapping.items():
                    node_id = str(node_id)
                    if not isinstance(node, dict):
                        warn(
                            "invalid_node",
                            "Mapping node is not an object; an empty structural node was indexed",
                            node_id,
                        )
                        node = {}
                    message = (
                        node.get("message")
                        if isinstance(node.get("message"), dict)
                        else None
                    )
                    node_oid = node_oids[node_id]
                    outcome = _upsert_object(
                        conn,
                        object_id_value=node_oid,
                        object_type="chatgpt_node",
                        source=SOURCE_NODE,
                        source_id=node_id,
                        occurred_at=_iso_from_unix(
                            message.get("create_time") if message else None
                        ),
                        captured_at=captured_at,
                        parent_id=None,
                        raw_uri=f"{raw_base}#conversation={conv_source_id}&node={node_id}",
                        content_hash=_sha256_json(node),
                        import_id=import_id,
                        topic=None,
                    )
                    count_object(outcome)
                    relation_outcome = _insert_relation(
                        conn,
                        relation_type="conversation_contains_node",
                        from_object_id=conv_oid,
                        to_object_id=node_oid,
                        created_at=captured_at,
                    )
                    count_relation(relation_outcome)
                    stats["nodes"] += 1

                    if message:
                        message_id = str(message.get("id") or "")
                        if message_id:
                            msg_oid = object_id(SOURCE_MESSAGE, message_id)
                            author = (
                                message.get("author")
                                if isinstance(message.get("author"), dict)
                                else {}
                            )
                            role = str(author.get("role") or "unknown")
                            content = (
                                message.get("content")
                                if isinstance(message.get("content"), dict)
                                else {}
                            )
                            content_type = str(content.get("content_type") or "unknown")
                            outcome = _upsert_object(
                                conn,
                                object_id_value=msg_oid,
                                object_type=f"chatgpt_message_{role}",
                                source=SOURCE_MESSAGE,
                                source_id=message_id,
                                occurred_at=_iso_from_unix(message.get("create_time")),
                                captured_at=captured_at,
                                parent_id=node_oid,
                                raw_uri=(
                                    f"{raw_base}#conversation={conv_source_id}&node={node_id}"
                                    f"&message={message_id}"
                                ),
                                content_hash=_sha256_json(message),
                                import_id=import_id,
                                topic=content_type,
                            )
                            count_object(outcome)
                            relation_outcome = _insert_relation(
                                conn,
                                relation_type="node_contains_message",
                                from_object_id=node_oid,
                                to_object_id=msg_oid,
                                created_at=captured_at,
                            )
                            count_relation(relation_outcome)
                            stats["messages"] += 1
                            if content_type in {"reasoning_recap", "thoughts"}:
                                warn(
                                    "content_not_normalized",
                                    f"Content type {content_type!r} is preserved in RAW but not normalized",
                                    message_id,
                                )
                            metadata = message.get("metadata")
                            if isinstance(metadata, dict) and metadata.get("attachments"):
                                warn(
                                    "attachments_not_indexed",
                                    "Message attachments remain in RAW and are not indexed yet",
                                    message_id,
                                )
                        else:
                            warn(
                                "message_missing_id",
                                "Message was skipped because it has no native id",
                                node_id,
                            )

                for node_id, node in mapping.items():
                    node_id = str(node_id)
                    node = node if isinstance(node, dict) else {}
                    parent_source_id = node.get("parent")
                    if parent_source_id is None:
                        child_oid = node_oids[node_id]
                        removed = conn.execute(
                            "DELETE FROM relations WHERE relation_type = 'node_parent' "
                            "AND to_object_id = ?",
                            (child_oid,),
                        ).rowcount
                        stats["relations_removed"] += removed
                        conn.execute(
                            "UPDATE objects SET parent_id = NULL WHERE object_id = ?",
                            (child_oid,),
                        )
                        conn.execute(
                            """
                            UPDATE object_versions SET parent_id = NULL
                            WHERE object_id = ? AND version = (
                                SELECT MAX(version) FROM object_versions
                                WHERE object_id = ?
                            )
                            """,
                            (child_oid, child_oid),
                        )
                        continue
                    parent_source_id = str(parent_source_id)
                    parent_oid = node_oids.get(parent_source_id)
                    child_oid = node_oids[node_id]
                    if parent_oid is None:
                        warn(
                            "missing_parent",
                            f"Parent node {parent_source_id!r} is absent from the conversation mapping",
                            node_id,
                        )
                        removed = conn.execute(
                            "DELETE FROM relations WHERE relation_type = 'node_parent' "
                            "AND to_object_id = ?",
                            (child_oid,),
                        ).rowcount
                        stats["relations_removed"] += removed
                        conn.execute(
                            "UPDATE objects SET parent_id = NULL WHERE object_id = ?",
                            (child_oid,),
                        )
                        conn.execute(
                            """
                            UPDATE object_versions SET parent_id = NULL
                            WHERE object_id = ? AND version = (
                                SELECT MAX(version) FROM object_versions
                                WHERE object_id = ?
                            )
                            """,
                            (child_oid, child_oid),
                        )
                        continue
                    expected_relation_id = relation_id(
                        "node_parent", parent_oid, child_oid
                    )
                    removed = conn.execute(
                        """
                        DELETE FROM relations
                        WHERE relation_type = 'node_parent' AND to_object_id = ?
                          AND relation_id <> ?
                        """,
                        (child_oid, expected_relation_id),
                    ).rowcount
                    stats["relations_removed"] += removed
                    conn.execute(
                        "UPDATE objects SET parent_id = ? WHERE object_id = ?",
                        (parent_oid, child_oid),
                    )
                    conn.execute(
                        """
                        UPDATE object_versions SET parent_id = ?
                        WHERE object_id = ? AND version = (
                            SELECT MAX(version) FROM object_versions WHERE object_id = ?
                        )
                        """,
                        (parent_oid, child_oid, child_oid),
                    )
                    relation_outcome = _insert_relation(
                        conn,
                        relation_type="node_parent",
                        from_object_id=parent_oid,
                        to_object_id=child_oid,
                        created_at=captured_at,
                    )
                    count_relation(relation_outcome)

            completed_at = _now()
            conn.execute(
                """
                UPDATE import_runs
                SET status = 'complete', completed_at = ?, stats_json = ?
                WHERE import_id = ?
                """,
                (
                    completed_at,
                    json.dumps(stats, ensure_ascii=False, sort_keys=True),
                    import_id,
                ),
            )
            conn.commit()
            return stats
        except Exception as exc:
            conn.rollback()
            conn.execute(
                """
                UPDATE import_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE import_id = ?
                """,
                (_now(), str(exc), import_id),
            )
            conn.commit()
            raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import ChatGPT conversations.json into Human OS SQLite"
    )
    parser.add_argument("conversations_json", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("schema/human_os.sqlite.sql"),
        help="Path to Human OS SQLite schema",
    )
    args = parser.parse_args()
    stats = import_chatgpt_export(args.conversations_json, args.database, args.schema)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
