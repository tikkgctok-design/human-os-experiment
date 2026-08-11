from pathlib import Path
import json
import sqlite3

from human_os.chatgpt_import import import_chatgpt_export


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "chatgpt_export_minimal.json"
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


def _counts(db: Path) -> tuple[int, int]:
    with sqlite3.connect(db) as conn:
        objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    return objects, relations


def test_import_is_structurally_complete_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"

    first = import_chatgpt_export(FIXTURE, db, SCHEMA)
    first_counts = _counts(db)

    second = import_chatgpt_export(FIXTURE, db, SCHEMA)
    second_counts = _counts(db)

    # 1 conversation + 3 mapping nodes + 2 messages.
    assert first_counts == (6, 7)
    assert second_counts == first_counts

    assert first["conversations"] == 1
    assert first["nodes"] == 3
    assert first["messages"] == 2
    assert first["objects_inserted"] == 6
    assert first["relations_inserted"] == 7
    assert second["objects_unchanged"] == 6
    assert second["relations_unchanged"] == 7
    assert second["relations_removed"] == 0

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        bad_fks = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert bad_fks == []

        user = conn.execute(
            "SELECT object_type, topic FROM objects WHERE source = 'chatgpt.message' AND source_id = 'msg_user_001'"
        ).fetchone()
        assert user == ("chatgpt_message_user", "text")

        parent_edges = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_type = 'node_parent'"
        ).fetchone()[0]
        assert parent_edges == 2

        versions = conn.execute("SELECT COUNT(*) FROM object_versions").fetchone()[0]
        runs = conn.execute(
            "SELECT status, COUNT(*) FROM import_runs GROUP BY status"
        ).fetchall()
        assert versions == 6
        assert runs == [("complete", 2)]


def test_changed_objects_create_versions(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    changed_export = tmp_path / "changed.json"
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    import_chatgpt_export(FIXTURE, db, SCHEMA)
    payload[0]["mapping"]["node_assistant"]["message"]["content"]["parts"] = [
        "Updated synthetic response."
    ]
    changed_export.write_text(json.dumps(payload), encoding="utf-8")
    result = import_chatgpt_export(changed_export, db, SCHEMA)

    # The message, its containing node, and containing conversation all changed.
    assert result["objects_changed"] == 3
    assert result["objects_unchanged"] == 3
    with sqlite3.connect(db) as conn:
        versions = conn.execute("SELECT COUNT(*) FROM object_versions").fetchone()[0]
        message_versions = conn.execute(
            """
            SELECT COUNT(*) FROM object_versions
            WHERE object_id = (
                SELECT object_id FROM objects
                WHERE source = 'chatgpt.message' AND source_id = 'msg_assistant_001'
            )
            """
        ).fetchone()[0]
    assert versions == 9
    assert message_versions == 2

    reverted = import_chatgpt_export(FIXTURE, db, SCHEMA)
    assert reverted["objects_changed"] == 3
    with sqlite3.connect(db) as conn:
        # Reusing a known state updates the current projection without duplicating it.
        assert conn.execute("SELECT COUNT(*) FROM object_versions").fetchone()[0] == 9


def test_import_records_non_fatal_diagnostics(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    diagnostic_export = tmp_path / "diagnostics.json"
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    message = payload[0]["mapping"]["node_assistant"]["message"]
    message["content"]["content_type"] = "reasoning_recap"
    message["metadata"]["attachments"] = [{"name": "synthetic.txt"}]
    payload[0]["mapping"]["node_assistant"]["parent"] = "missing_node"
    diagnostic_export.write_text(json.dumps(payload), encoding="utf-8")

    result = import_chatgpt_export(diagnostic_export, db, SCHEMA)

    assert result["warnings"] == 3
    assert result["relations_removed"] == 0
    with sqlite3.connect(db) as conn:
        codes = {
            row[0]
            for row in conn.execute("SELECT code FROM import_diagnostics").fetchall()
        }
    assert codes == {
        "attachments_not_indexed",
        "content_not_normalized",
        "missing_parent",
    }


def test_v1_database_is_migrated_without_rewriting_existing_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "human_os.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE objects (
                object_id TEXT PRIMARY KEY, object_type TEXT NOT NULL,
                source TEXT, source_id TEXT, occurred_at DATETIME,
                captured_at DATETIME, parent_id TEXT, raw_uri TEXT NOT NULL,
                content_hash TEXT, mime_type TEXT, topic TEXT, event_id TEXT,
                confidence REAL, created_at DATETIME NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES objects(object_id)
            );
            CREATE UNIQUE INDEX idx_objects_source_source_id
                ON objects(source, source_id)
                WHERE source IS NOT NULL AND source_id IS NOT NULL;
            CREATE TABLE relations (
                relation_id TEXT PRIMARY KEY, from_object_id TEXT NOT NULL,
                to_object_id TEXT NOT NULL, relation_type TEXT NOT NULL,
                confidence REAL, model_id TEXT, created_at DATETIME NOT NULL,
                FOREIGN KEY (from_object_id) REFERENCES objects(object_id),
                FOREIGN KEY (to_object_id) REFERENCES objects(object_id)
            );
            INSERT INTO objects VALUES (
                'legacy', 'legacy_object', 'legacy', 'one', NULL, NULL, NULL,
                'file:///legacy', 'abc', 'application/json', NULL, NULL, NULL,
                '2026-01-01T00:00:00+00:00'
            );
            """
        )

    result = import_chatgpt_export(FIXTURE, db, SCHEMA)

    assert result["objects_inserted"] == 6
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM objects WHERE object_id = 'legacy'"
        ).fetchone()[0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "import_runs",
        "object_versions",
        "import_diagnostics",
        "attachments",
        "blobs",
        "blob_locations",
        "metadata_extractions",
        "metadata_diagnostics",
    } <= tables
