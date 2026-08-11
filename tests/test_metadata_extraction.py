import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image

from human_os.ingestion import IngestObject, ingest_object
from human_os.metadata_extraction import extract_media_metadata


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


def _save_photo(
    path: Path,
    *,
    original: str | None = None,
    digitized: str | None = None,
    offset: str | None = None,
    gps: bool = False,
) -> None:
    image = Image.new("RGB", (64, 48), (20, 40, 60))
    exif = Image.Exif()
    exif[0x010F] = "Human OS Camera"
    exif[0x0110] = "Synthetic Model"
    exif[0x0112] = 6
    exif_ifd: dict[int, object] = {}
    if original:
        exif_ifd[0x9003] = original
    if digitized:
        exif_ifd[0x9004] = digitized
    if offset:
        exif_ifd[0x9011] = offset
    if exif_ifd:
        exif[0x8769] = exif_ifd
    if gps:
        exif[0x8825] = {
            1: "N",
            2: (48.0, 30.0, 0.0),
            3: "E",
            4: (142.0, 45.0, 0.0),
        }
    image.save(path, format="JPEG", exif=exif)


def _ingest_file(path: Path, db: Path, object_type: str):
    return ingest_object(
        IngestObject(
            object_type=object_type,
            source="metadata.test",
            source_id=path.name,
            raw_uri=path.resolve().as_uri(),
            mime_type="image/jpeg" if object_type == "photo" else "video/mp4",
            metadata={"synthetic": True},
        ),
        db,
        SCHEMA,
    )


def test_photo_exif_is_derived_without_changing_identity_or_raw(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "with-exif.jpg"
    _save_photo(
        photo,
        original="2025:03:04 05:06:07",
        digitized="2025:03:04 05:06:07",
        offset="+11:00",
        gps=True,
    )
    ingested = _ingest_file(photo, db, "photo")
    before = (hashlib.sha256(photo.read_bytes()).hexdigest(), photo.stat().st_mtime_ns)

    extracted = extract_media_metadata(ingested.object_id, db, SCHEMA)

    after = (hashlib.sha256(photo.read_bytes()).hexdigest(), photo.stat().st_mtime_ns)
    assert after == before
    assert extracted.object_id == ingested.object_id
    assert extracted.blob_id == ingested.blob_id
    assert extracted.outcome == "inserted"
    assert extracted.occurred_at == "2025-03-04T05:06:07+11:00"
    assert extracted.metadata["date_time_original"] == "2025:03:04 05:06:07"
    assert extracted.metadata["date_time_digitized"] == "2025:03:04 05:06:07"
    assert extracted.metadata["timezone_offset"] == "+11:00"
    assert extracted.metadata["gps"] == {"latitude": 48.5, "longitude": 142.75}
    assert extracted.metadata["camera"] == {
        "make": "Human OS Camera",
        "model": "Synthetic Model",
    }
    assert extracted.metadata["orientation"] == 6
    assert extracted.metadata["dimensions"] == {"width": 64, "height": 48}
    with sqlite3.connect(db) as conn:
        identity = conn.execute(
            "SELECT object_id, occurred_at FROM objects WHERE object_id = ?",
            (ingested.object_id,),
        ).fetchone()
    assert identity == (ingested.object_id, extracted.occurred_at)


def test_missing_exif_keeps_occurred_at_null_and_records_diagnostics(
    tmp_path: Path,
) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "no-exif.jpg"
    Image.new("RGB", (20, 10)).save(photo, format="JPEG")
    ingested = _ingest_file(photo, db, "photo")

    result = extract_media_metadata(ingested.object_id, db, SCHEMA)

    assert result.occurred_at is None
    assert result.metadata["date_time_original"] is None
    assert "photo.exif_missing" in result.diagnostics
    assert "photo.capture_time_missing" in result.diagnostics
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT occurred_at FROM objects WHERE object_id = ?",
            (ingested.object_id,),
        ).fetchone()[0] is None


def test_conflicting_photo_times_are_not_guessed(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "conflict.jpg"
    _save_photo(
        photo,
        original="2025:03:04 05:06:07",
        digitized="2025:03:05 05:06:07",
        offset="+11:00",
    )
    ingested = _ingest_file(photo, db, "photo")

    result = extract_media_metadata(ingested.object_id, db, SCHEMA)

    assert result.occurred_at is None
    assert "photo.capture_time_conflict" in result.diagnostics


def test_video_metadata_and_gps_are_normalized(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video container")
    ingested = _ingest_file(video, db, "video")

    monkeypatch.setattr(
        "human_os.metadata_extraction._read_video_tracks",
        lambda _: {
            "general": {
                "format": "MPEG-4",
                "format_profile": "Base Media",
                "codec_id": "mp42",
                "internet_media_type": "video/mp4",
                "duration": 3500,
                "encoded_date": "UTC 2025-04-05 06:07:08",
                "comapplequicktimelocationiso6709": "+48.5000+142.7500/",
            },
            "video": {
                "format": "AVC",
                "codec_id": "avc1",
                "width": 1920,
                "height": 1080,
            },
        },
    )

    result = extract_media_metadata(ingested.object_id, db, SCHEMA)

    assert result.occurred_at == "2025-04-05T06:07:08+00:00"
    assert result.metadata["duration_seconds"] == 3.5
    assert result.metadata["dimensions"] == {"width": 1920, "height": 1080}
    assert result.metadata["codec"] == {"format": "AVC", "codec_id": "avc1"}
    assert result.metadata["container"]["format"] == "MPEG-4"
    assert result.metadata["gps"] == {"latitude": 48.5, "longitude": 142.75}


def test_metadata_extraction_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    photo = tmp_path / "repeat.jpg"
    _save_photo(photo, original="2025:03:04 05:06:07", offset="+11:00")
    ingested = _ingest_file(photo, db, "photo")

    first = extract_media_metadata(ingested.object_id, db, SCHEMA)
    second = extract_media_metadata(ingested.object_id, db, SCHEMA)

    assert second.extraction_id == first.extraction_id
    assert second.object_id == first.object_id
    assert second.blob_id == first.blob_id
    assert second.outcome == "unchanged"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_extractions").fetchone()[0] == 1
        diagnostic_count = conn.execute(
            "SELECT COUNT(*) FROM metadata_diagnostics"
        ).fetchone()[0]
        assert diagnostic_count == len(first.diagnostics)


def test_naive_video_creation_time_is_not_used(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "human_os.sqlite"
    video = tmp_path / "naive-time.mp4"
    video.write_bytes(b"synthetic video container")
    ingested = _ingest_file(video, db, "video")
    monkeypatch.setattr(
        "human_os.metadata_extraction._read_video_tracks",
        lambda _: {
            "general": {
                "format": "MPEG-4",
                "duration": 1000,
                "encoded_date": "2025-04-05 06:07:08",
            },
            "video": {"format": "AVC", "width": 640, "height": 480},
        },
    )

    result = extract_media_metadata(ingested.object_id, db, SCHEMA)

    assert result.occurred_at is None
    assert "video.creation_timezone_missing" in result.diagnostics
