from pathlib import Path
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
