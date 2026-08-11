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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ids import object_id, relation_id

SOURCE_CONVERSATION = "chatgpt.conversation"
SOURCE_NODE = "chatgpt.node"
SOURCE_MESSAGE = "chatgpt.message"


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


def _insert_object(
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
    topic: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO objects (
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


def _insert_relation(
    conn: sqlite3.Connection,
    *,
    relation_type: str,
    from_object_id: str,
    to_object_id: str,
    created_at: str,
) -> None:
    rid = relation_id(relation_type, from_object_id, to_object_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO relations (
            relation_id, from_object_id, to_object_id, relation_type,
            confidence, model_id, created_at
        ) VALUES (?, ?, ?, ?, 1.0, 'deterministic-importer-v0.1', ?)
        """,
        (rid, from_object_id, to_object_id, relation_type, created_at),
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
) -> dict[str, int]:
    payload = json.loads(conversations_path.read_text(encoding="utf-8"))
    captured_at = _now()
    raw_base = conversations_path.resolve().as_uri()

    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_schema(conn, schema_path)

        stats = {"conversations": 0, "nodes": 0, "messages": 0, "relations": 0}

        with conn:
            for conversation in _iter_conversations(payload):
                conv_source_id = str(
                    conversation.get("conversation_id") or conversation.get("id") or ""
                )
                if not conv_source_id:
                    raise ValueError("Conversation is missing conversation_id/id")

                conv_oid = object_id(SOURCE_CONVERSATION, conv_source_id)
                _insert_object(
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
                    topic=None,
                )
                stats["conversations"] += 1

                mapping = conversation.get("mapping") or {}
                if not isinstance(mapping, dict):
                    continue

                node_oids = {
                    str(node_id): object_id(SOURCE_NODE, str(node_id))
                    for node_id in mapping.keys()
                }

                # Insert all nodes before applying parent links. Mapping order is not
                # guaranteed to be parent-first in an official export.
                for node_id, node in mapping.items():
                    node_id = str(node_id)
                    node = node if isinstance(node, dict) else {}
                    message = (
                        node.get("message")
                        if isinstance(node.get("message"), dict)
                        else None
                    )
                    node_oid = node_oids[node_id]
                    _insert_object(
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
                        topic=None,
                    )
                    _insert_relation(
                        conn,
                        relation_type="conversation_contains_node",
                        from_object_id=conv_oid,
                        to_object_id=node_oid,
                        created_at=captured_at,
                    )
                    stats["nodes"] += 1
                    stats["relations"] += 1

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
                            _insert_object(
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
                                topic=content_type,
                            )
                            _insert_relation(
                                conn,
                                relation_type="node_contains_message",
                                from_object_id=node_oid,
                                to_object_id=msg_oid,
                                created_at=captured_at,
                            )
                            stats["messages"] += 1
                            stats["relations"] += 1

                for node_id, node in mapping.items():
                    node_id = str(node_id)
                    node = node if isinstance(node, dict) else {}
                    parent_source_id = node.get("parent")
                    if parent_source_id is None:
                        continue
                    parent_source_id = str(parent_source_id)
                    parent_oid = node_oids.get(parent_source_id)
                    child_oid = node_oids[node_id]
                    if parent_oid is None:
                        continue
                    conn.execute(
                        "UPDATE objects SET parent_id = ? "
                        "WHERE object_id = ? AND parent_id IS NULL",
                        (parent_oid, child_oid),
                    )
                    _insert_relation(
                        conn,
                        relation_type="node_parent",
                        from_object_id=parent_oid,
                        to_object_id=child_oid,
                        created_at=captured_at,
                    )
                    stats["relations"] += 1

        return stats
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
