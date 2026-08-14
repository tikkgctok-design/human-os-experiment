import json
import sqlite3
from pathlib import Path

from PIL import Image

from human_os.ingestion import IngestObject, ingest_object
from human_os.metadata_extraction import extract_media_metadata
from human_os.schema import ensure_schema
from human_os.semantic_extraction import run_semantic_extractor
from human_os.semantic_registry import (
    DEFAULT_REGISTRY,
    ExtractorRegistry,
    ExtractorSpec,
    SemanticMention,
    SemanticOutput,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


def _item(path: Path, object_type: str, source_id: str) -> IngestObject:
    return IngestObject(
        object_type=object_type,
        source="semantic.test",
        source_id=source_id,
        raw_uri=path.resolve().as_uri(),
        mime_type={
            "photo": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/wav",
            "note": "text/plain",
            "message": "text/plain",
        }[object_type],
        captured_at="2026-08-11T08:00:00+00:00",
        metadata={"synthetic": True},
    )


def _registry(*specs: ExtractorSpec) -> ExtractorRegistry:
    registry = ExtractorRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def _caption(version: str = "1.0.0") -> ExtractorSpec:
    return ExtractorSpec(
        name="fixture_caption",
        version=version,
        semantic_type="image_caption",
        object_types=frozenset({"photo"}),
        handler=lambda context: SemanticOutput(
            status="complete",
            confidence=0.9,
            result_json={"byte_count": len(context.read_bytes())},
            result_text="Synthetic image",
            mentions=(
                SemanticMention("semantic_mentions_topic", "synthetic", 0.8),
            ),
        ),
    )


def test_semantic_result_is_created_for_existing_object(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo-v1")
    ingested = ingest_object(_item(photo, "photo", "photo-1"), db, SCHEMA)

    result = run_semantic_extractor(
        ingested.object_id,
        "fixture_caption",
        db,
        SCHEMA,
        registry=_registry(_caption()),
    )

    assert result.object_id == ingested.object_id
    assert result.status == "complete"
    assert result.outcome == "inserted"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT object_id, source_blob_id, source_content_hash, is_current "
            "FROM semantic_results WHERE semantic_result_id = ?",
            (result.semantic_result_id,),
        ).fetchone()
        relations = conn.execute(
            "SELECT relation_type, target_ref FROM semantic_relations "
            "WHERE semantic_result_id = ? ORDER BY relation_type",
            (result.semantic_result_id,),
        ).fetchall()
        canonical_id = conn.execute(
            "SELECT object_id FROM objects WHERE object_id = ?", (ingested.object_id,)
        ).fetchone()[0]
    assert row == (ingested.object_id, ingested.blob_id, ingested.content_hash, 1)
    assert relations == [
        ("object_has_semantic_result", result.semantic_result_id),
        ("semantic_mentions_topic", "synthetic"),
    ]
    assert canonical_id == ingested.object_id


def test_same_extractor_version_and_source_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"same-photo")
    ingested = ingest_object(_item(photo, "photo", "photo-1"), db, SCHEMA)
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return SemanticOutput(status="complete", result_text="once")

    registry = _registry(
        ExtractorSpec(
            "fixture_once", "1.0.0", "image_caption", frozenset({"photo"}), handler
        )
    )
    first = run_semantic_extractor(
        ingested.object_id, "fixture_once", db, SCHEMA, registry=registry
    )
    second = run_semantic_extractor(
        ingested.object_id, "fixture_once", db, SCHEMA, registry=registry
    )

    assert second.semantic_result_id == first.semantic_result_id
    assert second.outcome == "unchanged"
    assert calls == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0] == 1


def test_new_extractor_version_creates_history_and_new_current_result(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"same-photo")
    ingested = ingest_object(_item(photo, "photo", "photo-1"), db, SCHEMA)
    registry = _registry(_caption("1.0.0"), _caption("2.0.0"))

    old = run_semantic_extractor(
        ingested.object_id,
        "fixture_caption",
        db,
        SCHEMA,
        extractor_version="1.0.0",
        registry=registry,
    )
    new = run_semantic_extractor(
        ingested.object_id,
        "fixture_caption",
        db,
        SCHEMA,
        extractor_version="2.0.0",
        registry=registry,
    )

    assert old.semantic_result_id != new.semantic_result_id
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT extractor_version, is_current FROM semantic_results "
            "ORDER BY extractor_version"
        ).fetchall()
    assert rows == [("1.0.0", 0), ("2.0.0", 1)]


def test_changed_source_blob_invalidates_current_result_but_preserves_history(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo-v1")
    item = _item(photo, "photo", "stable-source")
    first_ingest = ingest_object(item, db, SCHEMA)
    registry = _registry(_caption())
    old = run_semantic_extractor(
        first_ingest.object_id, "fixture_caption", db, SCHEMA, registry=registry
    )

    photo.write_bytes(b"photo-v2-with-different-content")
    second_ingest = ingest_object(item, db, SCHEMA)
    new = run_semantic_extractor(
        second_ingest.object_id, "fixture_caption", db, SCHEMA, registry=registry
    )

    assert second_ingest.object_id == first_ingest.object_id
    assert second_ingest.blob_id != first_ingest.blob_id
    assert new.semantic_result_id != old.semantic_result_id
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT source_blob_id, is_current FROM semantic_results ORDER BY created_at"
        ).fetchall()
        assert conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0] == 2
    assert rows == [(first_ingest.blob_id, 0), (second_ingest.blob_id, 1)]


def test_unsupported_extractor_is_durable_and_does_not_break_ingestion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo")
    ingested = ingest_object(_item(photo, "photo", "photo-1"), db, SCHEMA)

    result = run_semantic_extractor(
        ingested.object_id, "image_caption", db, SCHEMA
    )

    assert result.status == "unsupported"
    assert result.diagnostics == ("extractor_not_implemented:image_caption",)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM semantic_results").fetchone()[0] == "unsupported"


def test_failed_extraction_is_recorded_with_diagnostics(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo")
    ingested = ingest_object(_item(photo, "photo", "photo-1"), db, SCHEMA)

    def fail(_):
        raise RuntimeError("synthetic failure")

    registry = _registry(
        ExtractorSpec(
            "fixture_fail", "1.0.0", "image_caption", frozenset({"photo"}), fail
        )
    )
    result = run_semantic_extractor(
        ingested.object_id, "fixture_fail", db, SCHEMA, registry=registry
    )

    assert result.status == "failed"
    assert "RuntimeError" in result.diagnostics[0]
    assert "synthetic failure" in result.diagnostics[0]
    with sqlite3.connect(db) as conn:
        stored = json.loads(
            conn.execute("SELECT diagnostics_json FROM semantic_results").fetchone()[0]
        )
    assert stored == list(result.diagnostics)


def test_semantic_extraction_preserves_raw_and_canonical_identity(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "immutable.jpg"
    photo.write_bytes(b"immutable RAW")
    before = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)
    ingested = ingest_object(_item(photo, "photo", "immutable"), db, SCHEMA)

    run_semantic_extractor(
        ingested.object_id,
        "fixture_caption",
        db,
        SCHEMA,
        registry=_registry(_caption()),
    )

    after = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)
    with sqlite3.connect(db) as conn:
        object_ids = conn.execute("SELECT object_id FROM objects").fetchall()
    assert after == before
    assert object_ids == [(ingested.object_id,)]


def test_required_extractors_are_registered() -> None:
    expected = {
        "image_caption",
        "ocr_text",
        "detected_objects",
        "detected_places",
        "detected_people_candidates",
        "video_metadata_semantic",
        "keyframes",
        "speech_reference",
        "visual_summary",
        "speech_to_text",
        "audio_summary",
        "text_normalization",
        "topics",
        "entities",
        "summary",
    }

    assert {spec.name for spec in DEFAULT_REGISTRY.list()} == expected


def test_schema_v4_migrates_to_v5_without_rewriting_canonical_objects(
    tmp_path: Path,
) -> None:
    db = tmp_path / "v4.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.executescript(
            """
            DROP TABLE event_evidence;
            DROP TABLE semantic_relations;
            DROP TABLE semantic_results;
            DELETE FROM schema_migrations;
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (4, '2026-08-11T00:00:00+00:00');
            PRAGMA user_version = 4;
            INSERT INTO objects (
                object_id, object_type, source, source_id, raw_uri,
                content_hash, captured_at, metadata_json, created_at
            ) VALUES (
                'hos_obj_preserved', 'note', 'migration.test', 'note-1',
                'notes://note-1', 'abc123', '2026-08-11T00:00:00+00:00',
                '{}', '2026-08-11T00:00:00+00:00'
            );
            """
        )
        ensure_schema(conn, SCHEMA)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        preserved = conn.execute(
            "SELECT object_id, source_id FROM objects WHERE object_id = ?",
            ("hos_obj_preserved",),
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == 5
    assert preserved == ("hos_obj_preserved", "note-1")
    assert {"semantic_results", "semantic_relations", "event_evidence"} <= tables


def test_synthetic_five_object_pipeline_has_complete_chain(tmp_path: Path) -> None:
    db = tmp_path / "demo.sqlite"
    cases = [
        ("photo", "image_caption", b"photo fixture"),
        ("note", "text_normalization", b"  A  local note.\n"),
        ("message", "text_normalization", b"A local message."),
        ("audio", "speech_to_text", b"audio fixture"),
        ("video", "video_metadata_semantic", b"video fixture"),
    ]
    results = []
    for index, (object_type, extractor, payload) in enumerate(cases):
        raw = tmp_path / f"{index}-{object_type}.raw"
        if object_type == "photo":
            Image.new("RGB", (4, 3), color=(10, 20, 30)).save(raw, format="JPEG")
        else:
            raw.write_bytes(payload)
        ingested = ingest_object(_item(raw, object_type, f"demo-{index}"), db, SCHEMA)
        if object_type in {"photo", "video"}:
            extract_media_metadata(ingested.object_id, db, SCHEMA)
        semantic = run_semantic_extractor(
            ingested.object_id, extractor, db, SCHEMA
        )
        results.append((ingested, semantic))

    with sqlite3.connect(db) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "objects",
                "blobs",
                "attachments",
                "metadata_extractions",
                "semantic_results",
                "semantic_relations",
            )
        }
        joined = conn.execute(
            """
            SELECT COUNT(*)
            FROM objects o
            JOIN semantic_results s ON s.object_id = o.object_id
            JOIN semantic_relations r ON r.semantic_result_id = s.semantic_result_id
            WHERE r.relation_type = 'object_has_semantic_result'
            """
        ).fetchone()[0]
    assert counts == {
        "objects": 5,
        "blobs": 3,
        "attachments": 3,
        "metadata_extractions": 2,
        "semantic_results": 5,
        "semantic_relations": 5,
    }
    assert joined == 5
    assert [result.status for _, result in results] == [
        "unsupported",
        "complete",
        "complete",
        "unsupported",
        "unsupported",
    ]
    assert sum(
        result.provenance["source"]["metadata_extraction_id"] is not None
        for _, result in results
    ) == 2
