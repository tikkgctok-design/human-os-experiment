import hashlib
import sqlite3
from pathlib import Path

import pytest

from human_os.ingestion import IngestObject, ingest_object


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


def _file_input(path: Path, object_type: str, source_id: str | None = None) -> IngestObject:
    return IngestObject(
        object_type=object_type,
        source="local.test",
        source_id=source_id,
        raw_uri=path.resolve().as_uri(),
        mime_type={
            "photo": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/mpeg",
        }[object_type],
        occurred_at="2026-01-02T03:04:05+00:00",
        captured_at="2026-08-11T07:00:00+00:00",
        metadata={"fixture": True},
    )


def test_new_photo_receives_object_id_without_mutating_raw(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "photo.jpg"
    original = b"synthetic photo bytes"
    photo.write_bytes(original)

    result = ingest_object(_file_input(photo, "photo"), db, SCHEMA)

    assert result.object_id.startswith("hos_obj_")
    assert result.outcome == "inserted"
    assert photo.read_bytes() == original
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """
            SELECT object_type, source, source_id, raw_uri, content_hash,
                   mime_type, metadata_json, occurred_at, captured_at, created_at
            FROM objects WHERE object_id = ?
            """,
            (result.object_id,),
        ).fetchone()
    assert row[:2] == ("photo", "local.test")
    assert row[2] == photo.resolve().as_uri()
    assert row[3] == photo.resolve().as_uri()
    assert row[4] == hashlib.sha256(original).hexdigest()
    assert row[5] == "image/jpeg"
    assert '"fixture":true' in row[6]
    assert row[7] == "2026-01-02T03:04:05+00:00"
    assert row[8] == "2026-08-11T07:00:00+00:00"
    assert row[9] == row[8]


def test_reingesting_same_photo_preserves_object_id(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"same photo")
    item = _file_input(photo, "photo")

    first = ingest_object(item, db, SCHEMA)
    with sqlite3.connect(db) as conn:
        first_version = conn.execute(
            "SELECT captured_at, created_at FROM object_versions"
        ).fetchone()
    second = ingest_object(item, db, SCHEMA)

    assert second.object_id == first.object_id
    assert second.outcome == "unchanged"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM blob_locations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT captured_at, created_at FROM object_versions"
        ).fetchone() == first_version


def test_same_bytes_at_different_raw_uris_have_distinct_objects_one_blob(
    tmp_path: Path,
) -> None:
    db = tmp_path / "human_os.sqlite"
    first_path = tmp_path / "camera" / "photo.jpg"
    second_path = tmp_path / "album" / "photo-copy.jpg"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    payload = b"identical photo bytes"
    first_path.write_bytes(payload)
    second_path.write_bytes(payload)

    first = ingest_object(_file_input(first_path, "photo"), db, SCHEMA)
    second = ingest_object(_file_input(second_path, "photo"), db, SCHEMA)

    assert first.object_id != second.object_id
    assert first.blob_id == second.blob_id
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM blob_locations").fetchone()[0] == 2


def test_new_note_receives_object_id_and_reimport_is_idempotent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "human_os.sqlite"
    note = IngestObject(
        object_type="note",
        source="notes.test",
        source_id="note-001",
        raw_uri="notes://note-001",
        content=b"A reconstructible memory note.",
        mime_type="text/plain",
        metadata={"title": "Synthetic note"},
    )

    first = ingest_object(note, db, SCHEMA)
    second = ingest_object(note, db, SCHEMA)

    assert first.object_id.startswith("hos_obj_")
    assert second.object_id == first.object_id
    assert second.outcome == "unchanged"
    assert first.blob_id is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM object_versions").fetchone()[0] == 1


@pytest.mark.parametrize("object_type", ["photo", "video", "audio"])
def test_supported_binary_types_share_canonical_ingestion(
    tmp_path: Path, object_type: str
) -> None:
    db = tmp_path / "human_os.sqlite"
    raw = tmp_path / f"sample.{object_type}"
    raw.write_bytes(f"synthetic {object_type}".encode())

    result = ingest_object(_file_input(raw, object_type), db, SCHEMA)

    assert result.object_id.startswith("hos_obj_")
    assert result.blob_id is not None


def test_ingestion_never_changes_raw_file_metadata_or_content(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "immutable.jpg"
    photo.write_bytes(b"immutable raw")
    before = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)

    ingest_object(_file_input(photo, "photo"), db, SCHEMA)

    after = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)
    assert after == before
