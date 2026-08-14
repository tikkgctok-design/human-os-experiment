import sqlite3
from pathlib import Path

from PIL import Image

from human_os.ingestion import IngestObject, ingest_object
from human_os.metadata_extraction import extract_media_metadata
from human_os.photo_semantic import (
    CAPTION_EXTRACTOR,
    OBJECTS_EXTRACTOR,
    OCR_EXTRACTOR,
    PLACE_EXTRACTOR,
    PhotoVisionResult,
    build_photo_production_registry,
    run_photo_semantics,
)
from human_os.photo_validation import validate_photo_sample


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


class FixtureVisionBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        self.calls.append(task)
        assert path.read_bytes().startswith(b"\xff\xd8")
        parsed = {
            "<DETAILED_CAPTION>": "A person standing beside a red bicycle.",
            "<OCR_WITH_REGION>": {
                "labels": ["TRAIL 7"],
                "quad_boxes": [[1, 1, 20, 1, 20, 8, 1, 8]],
            },
            "<OD>": {
                "labels": ["person", "bicycle"],
                "bboxes": [[1, 1, 10, 20], [11, 5, 30, 22]],
                "scores": [0.91, 0.83],
            },
        }[task]
        return PhotoVisionResult(
            task=task,
            parsed=parsed,
            runtime={
                "backend": "fixture",
                "model_id": "fixture/florence",
                "model_revision": "fixture-sha",
                "parameters": {"num_beams": 3, "do_sample": False},
            },
        )


def _photo(path: Path) -> IngestObject:
    return IngestObject(
        object_type="photo",
        source="photo.semantic.test",
        source_id="photo-1",
        raw_uri=path.resolve().as_uri(),
        mime_type="image/jpeg",
        captured_at="2026-08-11T09:00:00+00:00",
        metadata={"fixture": True},
    )


def _make_photo(path: Path) -> None:
    Image.new("RGB", (40, 30), color=(120, 30, 10)).save(path, format="JPEG")


def test_production_photo_extractors_write_four_versioned_semantic_results(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    _make_photo(photo)
    ingested = ingest_object(_photo(photo), db, SCHEMA)
    backend = FixtureVisionBackend()

    results = run_photo_semantics(
        ingested.object_id, db, SCHEMA, backend=backend
    )

    assert set(results) == {
        CAPTION_EXTRACTOR,
        OCR_EXTRACTOR,
        OBJECTS_EXTRACTOR,
        PLACE_EXTRACTOR,
    }
    assert results[CAPTION_EXTRACTOR].result_text == (
        "A person standing beside a red bicycle."
    )
    assert results[OCR_EXTRACTOR].result_text == "TRAIL 7"
    assert results[OBJECTS_EXTRACTOR].confidence == 0.87
    assert results[PLACE_EXTRACTOR].result_json == {"candidates": []}
    assert backend.calls == ["<DETAILED_CAPTION>", "<OCR_WITH_REGION>", "<OD>"]

    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            """
            SELECT extractor_name, extractor_version, source_blob_id,
                   source_content_hash, status, provenance_json
            FROM semantic_results ORDER BY extractor_name
            """
        ).fetchall()
        relation_types = conn.execute(
            "SELECT relation_type, target_ref FROM semantic_relations "
            "ORDER BY relation_type, target_ref"
        ).fetchall()
        canonical_id = conn.execute("SELECT object_id FROM objects").fetchone()[0]

    assert len(stored) == 4
    versions = {row[0]: row[1] for row in stored}
    assert versions[OBJECTS_EXTRACTOR] == "2.0.1"
    assert all(
        version == "1.0.0"
        for name, version in versions.items()
        if name != OBJECTS_EXTRACTOR
    )
    assert all(row[2] == ingested.blob_id for row in stored)
    assert all(row[3] == ingested.content_hash for row in stored)
    assert all('"model_id":"fixture/florence"' in row[5] for row in stored[:3])
    assert canonical_id == ingested.object_id
    assert ("semantic_mentions_entity", "object:bicycle") in relation_types
    assert ("semantic_mentions_entity", "object:person") in relation_types
    assert not any(row[0] == "semantic_mentions_person" for row in relation_types)


def test_photo_semantic_batch_is_idempotent_and_does_not_read_raw_twice(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    _make_photo(photo)
    ingested = ingest_object(_photo(photo), db, SCHEMA)
    backend = FixtureVisionBackend()

    first = run_photo_semantics(ingested.object_id, db, SCHEMA, backend=backend)
    second = run_photo_semantics(ingested.object_id, db, SCHEMA, backend=backend)

    assert {
        name: result.semantic_result_id for name, result in second.items()
    } == {name: result.semantic_result_id for name, result in first.items()}
    assert all(result.outcome == "unchanged" for result in second.values())
    assert backend.calls == ["<DETAILED_CAPTION>", "<OCR_WITH_REGION>", "<OD>"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0] == 6


def test_place_candidate_uses_only_reliable_gps_metadata(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    _make_photo(photo)
    ingested = ingest_object(_photo(photo), db, SCHEMA)
    extract_media_metadata(ingested.object_id, db, SCHEMA)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE metadata_extractions
            SET metadata_json = '{"gps":{"latitude":48.5,"longitude":142.75}}'
            WHERE object_id = ?
            """,
            (ingested.object_id,),
        )
        conn.commit()

    result = run_photo_semantics(
        ingested.object_id,
        db,
        SCHEMA,
        backend=FixtureVisionBackend(),
        extractor_names=(PLACE_EXTRACTOR,),
    )[PLACE_EXTRACTOR]

    assert result.status == "complete"
    assert result.confidence == 1.0
    assert result.result_json == {
        "candidates": [
            {
                "kind": "gps_coordinate",
                "latitude": 48.5,
                "longitude": 142.75,
                "confidence": 1.0,
            }
        ]
    }
    with sqlite3.connect(db) as conn:
        relation = conn.execute(
            "SELECT relation_type, target_ref FROM semantic_relations "
            "WHERE relation_type = 'semantic_mentions_place'"
        ).fetchone()
    assert relation == ("semantic_mentions_place", "geo:48.500000,142.750000")


def test_photo_semantics_preserves_raw_bytes_timestamps_and_object_id(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    _make_photo(photo)
    before = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)
    ingested = ingest_object(_photo(photo), db, SCHEMA)

    run_photo_semantics(
        ingested.object_id, db, SCHEMA, backend=FixtureVisionBackend()
    )

    after = (photo.read_bytes(), photo.stat().st_size, photo.stat().st_mtime_ns)
    with sqlite3.connect(db) as conn:
        object_ids = conn.execute("SELECT object_id FROM objects").fetchall()
    assert after == before
    assert object_ids == [(ingested.object_id,)]


def test_backend_failure_is_durable_and_does_not_block_other_results(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    _make_photo(photo)
    ingested = ingest_object(_photo(photo), db, SCHEMA)

    class FailingBackend(FixtureVisionBackend):
        def analyze(self, path: Path, task: str) -> PhotoVisionResult:
            if task == "<OCR_WITH_REGION>":
                raise RuntimeError("fixture OCR failure")
            return super().analyze(path, task)

    results = run_photo_semantics(
        ingested.object_id, db, SCHEMA, backend=FailingBackend()
    )

    assert results[OCR_EXTRACTOR].status == "failed"
    assert "fixture OCR failure" in results[OCR_EXTRACTOR].diagnostics[0]
    assert results[CAPTION_EXTRACTOR].status == "complete"
    assert results[OBJECTS_EXTRACTOR].status == "complete"
    assert results[PLACE_EXTRACTOR].status == "complete"


def test_registry_exposes_four_photo_production_extractors() -> None:
    registry = build_photo_production_registry(FixtureVisionBackend())

    assert {spec.name for spec in registry.list()} == {
        CAPTION_EXTRACTOR,
        OCR_EXTRACTOR,
        OBJECTS_EXTRACTOR,
        PLACE_EXTRACTOR,
    }


def test_validation_runner_records_repeat_and_raw_checks(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    photo = tmp_path / "photo.jpg"
    evidence = tmp_path / "validation.json"
    _make_photo(photo)
    ingested = ingest_object(_photo(photo), db, SCHEMA)

    report = validate_photo_sample(
        [ingested.object_id],
        db,
        SCHEMA,
        evidence,
        backend=FixtureVisionBackend(),
    )

    assert evidence.exists()
    assert report["sample_size"] == 1
    assert report["checks"] == {
        "raw_unchanged": True,
        "semantic_result_ids_stable": True,
        "counts_stable_on_repeat": True,
        "integrity_check": [["ok"]],
        "foreign_key_check": [],
        "person_identity_relations": 0,
    }
    assert report["counts"]["after_first"] == report["counts"]["after_repeat"]
