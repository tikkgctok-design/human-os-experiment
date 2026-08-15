"""Bounded, read-only Google Drive PHOTO pipeline validation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .google_drive_photo_source import DriveService, GoogleDrivePhotoSource
from .photo_import import import_source_photo
from .photo_materialization import materialize_photo
from .photo_semantic import PhotoVisionResult

MAX_LIVE_PHOTOS = 3

_METADATA_BACKENDS = {
    "human-os.photo.pillow@1": "Pillow",
    "human-os.video.mediainfo@1": "MediaInfo",
}


class ValidationPhotoBackend:
    """Exercise production semantic plumbing without installing an AI model."""

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
        parsed: Any
        if task == "<DETAILED_CAPTION>":
            parsed = {task: f"Validation image {width}x{height}."}
        elif task == "<OCR_WITH_REGION>":
            parsed = {task: {"labels": [], "quad_boxes": [], "scores": []}}
        elif task == "<OD>":
            parsed = {task: {"labels": [], "bboxes": [], "scores": []}}
        else:
            raise ValueError("unsupported PHOTO validation task")
        return PhotoVisionResult(
            task=task,
            parsed=parsed,
            runtime={
                "backend": "human-os.google-drive-validation",
                "mode": "local-structural-probe",
            },
        )


def _identity_hint(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    return value.partition(":")[0] or "validation_error"


def _selected(photos: tuple[Any, ...], limit: int) -> tuple[Any, ...]:
    return tuple(
        sorted(
            photos,
            key=lambda photo: (
                photo.mime_type != "image/jpeg",
                photo.source_id,
            ),
        )[:limit]
    )


def _diagnostic_category(code: str) -> str:
    if code.endswith(".extraction_failed"):
        return "extractor_error"
    if code.endswith("_missing"):
        return "missing_metadata"
    if code.endswith("_invalid"):
        return "invalid_metadata"
    if code.endswith("_conflict"):
        return "conflicting_metadata"
    return "metadata_warning"


def _group_status(
    metadata: dict[str, Any], codes: set[str], group: str
) -> str:
    relevant = {code for code in codes if group in code}
    if any(code.endswith(".extraction_failed") for code in codes):
        return "error"
    if any(code.endswith("_conflict") for code in relevant):
        return "conflict"
    if any(code.endswith("_invalid") for code in relevant):
        return "invalid"
    if any(code.endswith("_missing") for code in relevant):
        return "missing"
    if group == "capture_time":
        if "photo.timezone_missing" in codes:
            return "partial"
        found = bool(
            metadata.get("date_time_original")
            or metadata.get("date_time_digitized")
            or metadata.get("creation_time")
        )
    elif group == "camera":
        camera = metadata.get("camera") or {}
        found = bool(camera.get("make") or camera.get("model"))
    elif group == "orientation":
        found = metadata.get("orientation") is not None
    elif group == "dimensions":
        dimensions = metadata.get("dimensions") or {}
        found = (
            dimensions.get("width") is not None
            and dimensions.get("height") is not None
        )
    else:
        found = metadata.get(group) is not None
    return "found" if found else "missing"


def metadata_diagnostic_summary(
    conn: sqlite3.Connection, object_id: str
) -> dict[str, Any]:
    """Return a value-free explanation of the latest metadata status."""
    row = conn.execute(
        """
        SELECT extraction_id, extractor_id, status, metadata_json
        FROM metadata_extractions
        WHERE object_id = ? ORDER BY extracted_at DESC LIMIT 1
        """,
        (object_id,),
    ).fetchone()
    if row is None:
        return {
            "extractor": None,
            "backend": None,
            "status": "missing",
            "error_category": None,
            "error_type": None,
            "groups": {},
            "diagnostics": [],
        }
    extraction_id, extractor_id, status, metadata_json = row
    diagnostics = conn.execute(
        """
        SELECT severity, code, detail FROM metadata_diagnostics
        WHERE extraction_id = ? ORDER BY diagnostic_id
        """,
        (extraction_id,),
    ).fetchall()
    codes = {item[1] for item in diagnostics}
    metadata = json.loads(metadata_json)
    failed = next(
        (item for item in diagnostics if item[1].endswith(".extraction_failed")),
        None,
    )
    error_type = None
    if failed is not None:
        candidate = failed[2].partition(":")[0]
        if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:Error|Exception)", candidate
        ):
            error_type = candidate
    categories = {_diagnostic_category(code) for code in codes}
    error_category = None
    if "extractor_error" in categories:
        error_category = "extractor_error"
    elif status == "partial":
        error_category = (
            "missing_optional_metadata"
            if categories and categories <= {"missing_metadata", "metadata_warning"}
            else "metadata_quality_issue"
        )
    return {
        "extractor": extractor_id,
        "backend": _METADATA_BACKENDS.get(extractor_id, "unknown"),
        "status": status,
        "error_category": error_category,
        "error_type": error_type,
        "groups": {
            group: _group_status(metadata, codes, group)
            for group in (
                "capture_time",
                "gps",
                "camera",
                "orientation",
                "dimensions",
            )
        },
        "diagnostics": [
            {
                "severity": severity,
                "code": code,
                "category": _diagnostic_category(code),
            }
            for severity, code, _detail in diagnostics
        ],
    }


def validate_google_drive_photos(
    service: DriveService,
    *,
    folder_id: str,
    source_namespace: str,
    schema_path: Path,
    limit: int = MAX_LIVE_PHOTOS,
) -> dict[str, Any]:
    """Validate at most three Drive photos without persistent files or Drive writes."""
    if not 1 <= limit <= MAX_LIVE_PHOTOS:
        raise ValueError(f"limit must be between 1 and {MAX_LIVE_PHOTOS}")
    if not schema_path.is_file():
        raise FileNotFoundError("Human OS schema file does not exist")

    source = GoogleDrivePhotoSource(
        service,
        folder_id=folder_id,
        source_namespace=source_namespace,
    )
    discovered = source.list_photos()
    selected = _selected(discovered, limit)
    report: dict[str, Any] = {
        "validation_version": "1.0.0",
        "read_only_google_drive": True,
        "temporary_database": True,
        "discovery": {
            "found_count": len(discovered),
            "selected_count": len(selected),
            "photos": [
                {
                    "identity_hint": _identity_hint(photo.source_id),
                    "mime_type": photo.mime_type,
                    "byte_size": photo.byte_size,
                    "modified_at": photo.modified_at.isoformat(),
                }
                for photo in selected
            ],
        },
        "processed": [],
    }

    with tempfile.TemporaryDirectory(prefix="human-os-gdrive-validation-") as temporary:
        database_path = Path(temporary) / "validation.sqlite"
        backend = ValidationPhotoBackend()
        for photo in selected:
            record: dict[str, Any] = {
                "identity_hint": _identity_hint(photo.source_id),
                "mime_type": photo.mime_type,
            }
            direct_path: Path | None = None
            try:
                with materialize_photo(source, photo) as materialized:
                    direct_path = materialized.path
                    with Image.open(materialized.path) as opened:
                        opened.verify()
                    record["materialization"] = {
                        "temporary": materialized.temporary,
                        "content_opened": True,
                        "byte_size_matches": (
                            photo.byte_size is None
                            or materialized.byte_size == photo.byte_size
                        ),
                    }
                record["materialization"]["cleanup"] = not direct_path.exists()

                first = import_source_photo(
                    source,
                    photo,
                    database_path,
                    schema_path,
                    backend=backend,
                )
                repeat = import_source_photo(
                    source,
                    photo,
                    database_path,
                    schema_path,
                    backend=backend,
                )
                record["ingestion"] = {
                    "first_outcome": first.ingestion_outcome,
                    "repeat_outcome": repeat.ingestion_outcome,
                    "stable_object_id": (
                        first.object_id is not None
                        and first.object_id == repeat.object_id
                    ),
                    "first_error": _safe_error(first.error),
                    "repeat_error": _safe_error(repeat.error),
                }
                if first.object_id is None:
                    raise RuntimeError("photo ingestion did not create an object")

                with closing(sqlite3.connect(database_path)) as conn:
                    canonical = conn.execute(
                        """
                        SELECT source_id, raw_uri, metadata_json
                        FROM objects WHERE object_id = ?
                        """,
                        (first.object_id,),
                    ).fetchone()
                    metadata_diagnostics = metadata_diagnostic_summary(
                        conn, first.object_id
                    )
                    semantic_statuses = [
                        row[0]
                        for row in conn.execute(
                            """
                            SELECT status FROM semantic_results
                            WHERE object_id = ? AND is_current = 1
                            ORDER BY extractor_name
                            """,
                            (first.object_id,),
                        ).fetchall()
                    ]
                if canonical is None:
                    raise RuntimeError("canonical object is missing after ingestion")
                canonical_metadata = json.loads(canonical[2])
                record["canonical"] = {
                    "object_id": first.object_id,
                    "source_id_matches": canonical[0] == photo.source_id,
                    "raw_uri_matches": canonical[1] == photo.raw_uri,
                    "raw_uri_scheme": canonical[1].partition(":")[0],
                    "provider_metadata_preserved": all(
                        canonical_metadata.get(key) == photo.metadata.get(key)
                        for key in (
                            "drive_file_id",
                            "drive_md5_checksum",
                            "drive_modified_time",
                            "drive_version",
                        )
                    ),
                }
                record["metadata_status"] = metadata_diagnostics["status"]
                record["metadata_diagnostics"] = metadata_diagnostics
                record["semantic"] = {
                    "result_count": len(semantic_statuses),
                    "statuses": semantic_statuses,
                    "materialized_path_used": len(semantic_statuses) > 0,
                }
            except Exception as exc:
                record["error_type"] = type(exc).__name__
                if direct_path is not None:
                    record.setdefault("materialization", {})["cleanup"] = (
                        not direct_path.exists()
                    )
            report["processed"].append(record)

        if database_path.exists():
            database_bytes = database_path.read_bytes()
            with closing(sqlite3.connect(database_path)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        else:
            database_bytes = b""
            integrity = "not_created"
            foreign_keys = []
        report["checks"] = {
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "temporary_path_not_persisted": b"human-os-photo-" not in database_bytes,
            "all_materialized_files_cleaned": all(
                item.get("materialization", {}).get("cleanup") is True
                for item in report["processed"]
            ),
            "all_identities_stable": all(
                item.get("ingestion", {}).get("stable_object_id") is True
                for item in report["processed"]
            ),
            "all_drive_uris_preserved": all(
                item.get("canonical", {}).get("raw_uri_scheme") == "gdrive"
                and item.get("canonical", {}).get("raw_uri_matches") is True
                for item in report["processed"]
            ),
        }

    report["ok"] = bool(selected) and not any(
        "error_type" in item for item in report["processed"]
    ) and all(
        (
            report["checks"]["integrity_check"] == "ok",
            report["checks"]["foreign_key_violations"] == 0,
            report["checks"]["temporary_path_not_persisted"],
            report["checks"]["all_materialized_files_cleaned"],
            report["checks"]["all_identities_stable"],
            report["checks"]["all_drive_uris_preserved"],
        )
    )
    return report
