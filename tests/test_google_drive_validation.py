import importlib.util
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from human_os.google_drive_validation import (
    metadata_diagnostic_summary,
    validate_google_drive_photos,
)
from human_os.ingestion import IngestObject, ingest_object
from human_os.metadata_extraction import extract_media_metadata

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"
SCRIPT = ROOT / "scripts" / "google_drive_photo_e2e.py"


class FakeRequest:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


class ReadOnlyDriveFiles:
    def __init__(self, metadata: dict[str, Any], payload: bytes) -> None:
        self.metadata = metadata
        self.payload = payload
        self.list_calls: list[dict[str, Any]] = []
        self.download_count = 0

    def list(self, **kwargs: Any) -> FakeRequest:
        self.list_calls.append(kwargs)
        return FakeRequest({"files": [self.metadata]})

    def get(self, **kwargs: Any) -> FakeRequest:
        assert kwargs["fileId"] == self.metadata["id"]
        return FakeRequest(self.metadata)

    def get_media(self, **kwargs: Any) -> FakeRequest:
        assert kwargs["fileId"] == self.metadata["id"]
        self.download_count += 1
        return FakeRequest(self.payload)


class ReadOnlyDriveService:
    def __init__(self, resource: ReadOnlyDriveFiles) -> None:
        self.resource = resource

    def files(self) -> ReadOnlyDriveFiles:
        return self.resource


def _jpeg() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (18, 12), color=(10, 20, 30)).save(stream, format="JPEG")
    return stream.getvalue()


def _metadata_photo(path: Path, *, complete: bool) -> None:
    image = Image.new("RGB", (18, 12), color=(10, 20, 30))
    if not complete:
        image.save(path, format="JPEG")
        return
    exif = Image.Exif()
    exif[0x010F] = "Test Camera"
    exif[0x0110] = "Test Model"
    exif[0x0112] = 1
    exif[0x8769] = {
        0x9003: "2026:08:15 01:02:03",
        0x9004: "2026:08:15 01:02:03",
        0x9011: "+11:00",
    }
    exif[0x8825] = {
        1: "N",
        2: (48.0, 30.0, 0.0),
        3: "E",
        4: (142.0, 45.0, 0.0),
    }
    image.save(path, format="JPEG", exif=exif)


def _extract_and_summarize(path: Path, db: Path) -> dict[str, Any]:
    ingested = ingest_object(
        IngestObject(
            object_type="photo",
            source="metadata-summary.test",
            source_id=path.name,
            raw_uri=path.resolve().as_uri(),
            mime_type="image/jpeg",
        ),
        db,
        SCHEMA,
    )
    extract_media_metadata(ingested.object_id, db, SCHEMA)

    with sqlite3.connect(db) as conn:
        return metadata_diagnostic_summary(conn, ingested.object_id)


def _script_module() -> Any:
    spec = importlib.util.spec_from_file_location("google_drive_photo_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validation_runs_bounded_pipeline_twice_and_returns_only_safe_identity() -> None:
    payload = _jpeg()
    metadata = {
        "id": "driveFile1",
        "name": "private-name.jpg",
        "mimeType": "image/jpeg",
        "size": str(len(payload)),
        "md5Checksum": "0123456789abcdef0123456789abcdef",
        "modifiedTime": "2026-08-15T01:02:03Z",
        "createdTime": "2025-01-02T03:04:05Z",
        "version": "9",
        "parents": ["folderA"],
        "trashed": False,
    }
    resource = ReadOnlyDriveFiles(metadata, payload)

    report = validate_google_drive_photos(
        ReadOnlyDriveService(resource),
        folder_id="folderA",
        source_namespace="validation-drive",
        schema_path=SCHEMA,
        limit=1,
    )

    assert report["ok"] is True
    assert report["read_only_google_drive"] is True
    assert report["temporary_database"] is True
    assert report["discovery"]["found_count"] == 1
    assert report["discovery"]["selected_count"] == 1
    processed = report["processed"][0]
    assert processed["materialization"] == {
        "temporary": True,
        "content_opened": True,
        "byte_size_matches": True,
        "cleanup": True,
    }
    assert processed["ingestion"]["first_outcome"] == "inserted"
    assert processed["ingestion"]["repeat_outcome"] == "unchanged"
    assert processed["ingestion"]["stable_object_id"] is True
    assert processed["canonical"]["source_id_matches"] is True
    assert processed["canonical"]["raw_uri_matches"] is True
    assert processed["canonical"]["raw_uri_scheme"] == "gdrive"
    assert processed["canonical"]["provider_metadata_preserved"] is True
    assert processed["metadata_status"] != "failed"
    assert processed["metadata_diagnostics"]["extractor"] == (
        "human-os.photo.pillow@1"
    )
    assert processed["metadata_diagnostics"]["backend"] == "Pillow"
    assert processed["metadata_diagnostics"]["error_category"] == (
        "missing_optional_metadata"
    )
    assert processed["semantic"]["result_count"] == 4
    assert report["checks"]["temporary_path_not_persisted"] is True
    assert report["checks"]["integrity_check"] == "ok"
    assert report["checks"]["foreign_key_violations"] == 0
    assert resource.download_count == 3
    assert "'folderA' in parents" in resource.list_calls[0]["q"]

    serialized = json.dumps(report)
    assert "driveFile1" not in serialized
    assert "private-name.jpg" not in serialized
    assert "human-os-photo-" not in serialized
    assert "file:///" not in serialized


def test_metadata_diagnostics_explain_complete_result(tmp_path: Path) -> None:
    photo = tmp_path / "complete.jpg"
    _metadata_photo(photo, complete=True)

    summary = _extract_and_summarize(photo, tmp_path / "complete.sqlite")

    assert summary["status"] == "complete"
    assert summary["error_category"] is None
    assert summary["diagnostics"] == []
    assert set(summary["groups"].values()) == {"found"}


def test_metadata_diagnostics_explain_missing_optional_blocks(tmp_path: Path) -> None:
    photo = tmp_path / "missing.jpg"
    _metadata_photo(photo, complete=False)

    summary = _extract_and_summarize(photo, tmp_path / "missing.sqlite")

    assert summary["status"] == "partial"
    assert summary["error_category"] == "missing_optional_metadata"
    assert summary["groups"]["capture_time"] == "missing"
    assert summary["groups"]["gps"] == "missing"
    assert summary["groups"]["camera"] == "missing"
    assert {item["code"] for item in summary["diagnostics"]} >= {
        "photo.exif_missing",
        "photo.gps_missing",
        "photo.capture_time_missing",
        "photo.camera_missing",
    }


def test_metadata_diagnostics_separate_extractor_error_and_hide_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "error.jpg"
    _metadata_photo(photo, complete=False)
    db = tmp_path / "error.sqlite"
    secret_detail = "private-file-id-and-path"
    monkeypatch.setattr(
        "human_os.metadata_extraction._extract_photo",
        lambda _path: (_ for _ in ()).throw(OSError(secret_detail)),
    )

    summary = _extract_and_summarize(photo, db)

    assert summary["status"] == "failed"
    assert summary["error_category"] == "extractor_error"
    assert summary["error_type"] == "OSError"
    assert all(value == "error" for value in summary["groups"].values())
    serialized = json.dumps(summary)
    assert secret_detail not in serialized
    assert str(photo) not in serialized


def test_validation_rejects_unbounded_limit_before_drive_access() -> None:
    class NoAccessService:
        def files(self) -> Any:
            raise AssertionError("Drive must not be accessed")

    with pytest.raises(ValueError, match="between 1 and 3"):
        validate_google_drive_photos(
            NoAccessService(),
            folder_id="folderA",
            source_namespace="validation-drive",
            schema_path=SCHEMA,
            limit=4,
        )


def test_manual_script_requires_folder_and_uses_readonly_scope() -> None:
    script = _script_module()

    assert script.DRIVE_READONLY_SCOPE == (
        "https://www.googleapis.com/auth/drive.readonly"
    )
    with pytest.raises(SystemExit) as captured:
        script.main([])
    assert captured.value.code == 2


def test_google_dependencies_are_validation_only() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    assert "google-api-python-client" not in project
    assert "google-auth-oauthlib" not in project
    assert "google-api-python-client" in script
    assert "google-auth-oauthlib" in script
