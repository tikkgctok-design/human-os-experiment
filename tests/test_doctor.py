import hashlib
import sqlite3
from pathlib import Path

from human_os.doctor import main, run_doctor
from human_os.schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"
OBJECT_ID = "hos_obj_11111111111111111111111111111111"


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "index.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        digest = hashlib.sha256(OBJECT_ID.encode()).hexdigest()
        conn.execute(
            """
            INSERT INTO objects (
                object_id, object_type, source, source_id, occurred_at, captured_at,
                raw_uri, content_hash, mime_type, metadata_json, created_at
            ) VALUES (?, 'photo', 'fixture', ?, ?, ?, ?, ?, 'image/jpeg', '{}', ?)
            """,
            (
                OBJECT_ID, OBJECT_ID, "2024-03-23T13:08:58+11:00",
                "2024-03-23T13:08:58+11:00", f"file:///archive/{OBJECT_ID}.jpg", digest,
                "2026-08-13T00:00:00+00:00",
            ),
        )
    return database


def test_doctor_reports_ok_status_for_healthy_index(tmp_path: Path) -> None:
    database = _database(tmp_path)

    report = run_doctor(database)

    assert report["database_available"] is True
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["schema_up_to_date"] is True
    assert report["counts"]["objects"] == 1
    assert report["search_available"] is True
    assert report["issues"] == []
    assert report["status"] == "OK"


def test_doctor_is_read_only_and_does_not_modify_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)

    run_doctor(database)

    after = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)
    assert after == before


def test_doctor_reports_error_status_when_database_missing(tmp_path: Path) -> None:
    report = run_doctor(tmp_path / "missing.sqlite")

    assert report["database_available"] is False
    assert report["status"] == "ERROR"
    assert report["issues"]


def test_doctor_cli_prints_status_line(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)

    main(["--db", str(database)])

    captured = capsys.readouterr()
    assert '"status": "OK"' in captured.out
