"""Derived EXIF and media metadata extraction for canonical Human OS objects."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import ExifTags, Image
from pymediainfo import MediaInfo

from .ids import HUMAN_OS_NAMESPACE
from .schema import ensure_schema

PHOTO_EXTRACTOR_ID = "human-os.photo.pillow@1"
VIDEO_EXTRACTOR_ID = "human-os.video.mediainfo@1"


@dataclass(frozen=True)
class ExtractionResult:
    extraction_id: str
    object_id: str
    blob_id: str
    outcome: str
    status: str
    metadata: dict[str, Any]
    occurred_at: str | None
    diagnostics: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_from_uri(raw_uri: str) -> Path:
    parsed = urlparse(raw_uri)
    if parsed.scheme != "file":
        raise ValueError("metadata extraction currently requires a file URI")
    text = unquote(parsed.path)
    if parsed.netloc:
        text = f"//{parsed.netloc}{text}"
    if len(text) >= 3 and text[0] == "/" and text[2] == ":":
        text = text[1:]
    return Path(text)


def _extraction_id(object_id: str, blob_id: str, extractor_id: str) -> str:
    value = uuid.uuid5(
        HUMAN_OS_NAMESPACE,
        f"metadata:{object_id}:{blob_id}:{extractor_id}",
    )
    return f"hos_meta_{value.hex}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip().strip("\x00")
    return result or None


def _float(value: Any) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]) / float(value[1])
    return float(value)


def _gps_coordinate(values: Any, reference: Any) -> float:
    degrees, minutes, seconds = (_float(value) for value in values)
    coordinate = degrees + minutes / 60 + seconds / 3600
    if _text(reference) in {"S", "W"}:
        coordinate = -coordinate
    return round(coordinate, 8)


def _parse_exif_datetime(value: str, offset: str) -> str:
    naive = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset)
    if not match:
        raise ValueError("invalid EXIF timezone offset")
    delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
    if match[1] == "-":
        delta = -delta
    return naive.replace(tzinfo=timezone(delta)).isoformat()


def _extract_photo(path: Path) -> tuple[dict[str, Any], str | None, str | None, list[tuple[str, str]]]:
    diagnostics: list[tuple[str, str]] = []
    with Image.open(path) as image:
        exif = image.getexif()
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif) if exif else {}
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if exif else {}
        original = _text(exif_ifd.get(0x9003) or exif.get(0x9003))
        digitized = _text(exif_ifd.get(0x9004) or exif.get(0x9004))
        offset = _text(exif_ifd.get(0x9011) or exif.get(0x9011))
        make = _text(exif.get(0x010F))
        model = _text(exif.get(0x0110))
        orientation = exif.get(0x0112)
        gps: dict[str, float] | None = None
        if gps_ifd:
            try:
                latitude = _gps_coordinate(gps_ifd[2], gps_ifd[1])
                longitude = _gps_coordinate(gps_ifd[4], gps_ifd[3])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("GPS coordinate is outside valid range")
                gps = {"latitude": latitude, "longitude": longitude}
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                diagnostics.append(("photo.gps_invalid", str(exc)))
        else:
            diagnostics.append(("photo.gps_missing", "GPS EXIF IFD is absent"))

        occurred_at = None
        occurred_source = None
        if not exif:
            diagnostics.append(("photo.exif_missing", "No EXIF metadata is present"))
        if original and digitized and original != digitized:
            diagnostics.append(
                ("photo.capture_time_conflict", "DateTimeOriginal and DateTimeDigitized differ")
            )
        elif original and offset:
            try:
                occurred_at = _parse_exif_datetime(original, offset)
                occurred_source = "exif.DateTimeOriginal+OffsetTimeOriginal"
            except ValueError as exc:
                diagnostics.append(("photo.capture_time_invalid", str(exc)))
        elif original:
            diagnostics.append(
                ("photo.timezone_missing", "DateTimeOriginal has no explicit timezone offset")
            )
        else:
            diagnostics.append(
                ("photo.capture_time_missing", "DateTimeOriginal is absent")
            )
        if not make and not model:
            diagnostics.append(("photo.camera_missing", "Camera make and model are absent"))

        metadata = {
            "kind": "photo",
            "date_time_original": original,
            "date_time_digitized": digitized,
            "timezone_offset": offset,
            "gps": gps,
            "camera": {"make": make, "model": model},
            "orientation": int(orientation) if orientation is not None else None,
            "dimensions": {"width": image.width, "height": image.height},
        }
    return metadata, occurred_at, occurred_source, diagnostics


def _read_video_tracks(path: Path) -> dict[str, dict[str, Any]]:
    media = MediaInfo.parse(str(path), full=True)
    general = next((track.to_data() for track in media.tracks if track.track_type == "General"), {})
    video = next((track.to_data() for track in media.tracks if track.track_type == "Video"), {})
    return {"general": general, "video": video}


def _parse_video_datetime(value: str) -> str | None:
    text = value.strip()
    utc = text.startswith("UTC ") or text.endswith(" UTC") or text.endswith("Z")
    text = text.removeprefix("UTC ").removesuffix(" UTC").removesuffix("Z")
    parsed = None
    for format_string in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, format_string)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None and utc:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat() if parsed.tzinfo else None


def _parse_iso6709(value: str) -> dict[str, float] | None:
    match = re.search(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", value)
    if not match:
        return None
    latitude, longitude = float(match[1]), float(match[2])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _extract_video(path: Path) -> tuple[dict[str, Any], str | None, str | None, list[tuple[str, str]]]:
    diagnostics: list[tuple[str, str]] = []
    tracks = _read_video_tracks(path)
    general, video = tracks.get("general", {}), tracks.get("video", {})
    date_keys = ("encoded_date", "tagged_date", "recorded_date", "mastered_date")
    raw_dates = list(dict.fromkeys(_text(general.get(key)) for key in date_keys))
    raw_dates = [value for value in raw_dates if value]
    parsed_dates = list(dict.fromkeys(filter(None, (_parse_video_datetime(value) for value in raw_dates))))
    occurred_at = None
    occurred_source = None
    if len(parsed_dates) == 1:
        occurred_at = parsed_dates[0]
        source_key = next(key for key in date_keys if _text(general.get(key)))
        occurred_source = f"mediainfo.{source_key}"
    elif len(parsed_dates) > 1:
        diagnostics.append(("video.creation_time_conflict", "Embedded creation timestamps differ"))
    elif raw_dates:
        diagnostics.append(("video.creation_timezone_missing", "Embedded creation time is not timezone-aware"))
    else:
        diagnostics.append(("video.creation_time_missing", "No embedded creation time is present"))

    gps = None
    gps_raw = None
    for key, value in general.items():
        if ("location" in key.lower() or "gps" in key.lower()) and _text(value):
            parsed = _parse_iso6709(str(value))
            if parsed:
                gps, gps_raw = parsed, str(value)
                break
    if gps is None:
        diagnostics.append(("video.gps_missing", "No supported GPS metadata is present"))
    if not video:
        diagnostics.append(("video.track_missing", "No video track metadata is present"))

    duration = general.get("duration")
    try:
        duration_seconds = float(duration) / 1000 if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None
        diagnostics.append(("video.duration_invalid", "Duration is not numeric"))
    metadata = {
        "kind": "video",
        "creation_time": raw_dates[0] if raw_dates else None,
        "creation_time_candidates": raw_dates,
        "duration_seconds": duration_seconds,
        "dimensions": {
            "width": int(video["width"]) if video.get("width") else None,
            "height": int(video["height"]) if video.get("height") else None,
        },
        "codec": {"format": _text(video.get("format")), "codec_id": _text(video.get("codec_id"))},
        "container": {
            "format": _text(general.get("format")),
            "profile": _text(general.get("format_profile")),
            "codec_id": _text(general.get("codec_id")),
            "mime_type": _text(general.get("internet_media_type")),
        },
        "gps": gps,
        "gps_raw": gps_raw,
    }
    return metadata, occurred_at, occurred_source, diagnostics


def _load_existing(conn: sqlite3.Connection, extraction_id: str) -> ExtractionResult | None:
    row = conn.execute(
        "SELECT object_id, blob_id, status, metadata_json, occurred_at "
        "FROM metadata_extractions WHERE extraction_id = ?",
        (extraction_id,),
    ).fetchone()
    if not row:
        return None
    diagnostics = tuple(
        item[0]
        for item in conn.execute(
            "SELECT code FROM metadata_diagnostics WHERE extraction_id = ? ORDER BY diagnostic_id",
            (extraction_id,),
        )
    )
    return ExtractionResult(
        extraction_id, row[0], row[1], "unchanged", row[2], json.loads(row[3]), row[4], diagnostics
    )


def extract_media_metadata(
    object_id: str,
    database_path: Path,
    schema_path: Path,
    *,
    materialized_path: Path | None = None,
) -> ExtractionResult:
    """Extract derived metadata without changing RAW or Human OS identity."""
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn, schema_path)
        row = conn.execute(
            """
            SELECT o.object_type, o.raw_uri, o.occurred_at, o.content_hash,
                   a.blob_id
            FROM objects o JOIN attachments a ON a.object_id = o.object_id
            WHERE o.object_id = ?
            """,
            (object_id,),
        ).fetchone()
        if not row:
            raise ValueError("object is missing or has no attachment")
        object_type, raw_uri, canonical_occurred, content_hash, blob_id = row
        if object_type not in {"photo", "video"}:
            raise ValueError("metadata extraction supports photo and video objects")
        extractor_id = PHOTO_EXTRACTOR_ID if object_type == "photo" else VIDEO_EXTRACTOR_ID
        extraction_id = _extraction_id(object_id, blob_id, extractor_id)
        existing = _load_existing(conn, extraction_id)
        if existing:
            return existing

        path = materialized_path if materialized_path is not None else _path_from_uri(raw_uri)
        try:
            if object_type == "photo":
                metadata, occurred_at, occurred_source, diagnostics = _extract_photo(path)
            else:
                metadata, occurred_at, occurred_source, diagnostics = _extract_video(path)
            status = "partial" if diagnostics else "complete"
        except Exception as exc:
            metadata = {"kind": object_type}
            occurred_at = occurred_source = None
            diagnostics = [(f"{object_type}.extraction_failed", f"{type(exc).__name__}: {exc}")]
            status = "failed"

        if occurred_at and canonical_occurred and occurred_at != canonical_occurred:
            diagnostics.append(
                ("canonical.occurred_at_conflict", "Derived time conflicts with canonical occurred_at")
            )
            occurred_at = occurred_source = None
            status = "partial"
        extracted_at = _now()
        metadata_json = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with conn:
            conn.execute(
                """
                INSERT INTO metadata_extractions (
                    extraction_id, object_id, blob_id, extractor_id, status,
                    metadata_json, occurred_at, occurred_at_source,
                    occurred_at_confidence, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_id, object_id, blob_id, extractor_id, status,
                    metadata_json, occurred_at, occurred_source,
                    "high" if occurred_at else "none", extracted_at,
                ),
            )
            for code, detail in diagnostics:
                severity = "error" if code.endswith(".extraction_failed") else "warning"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO metadata_diagnostics (
                        extraction_id, severity, code, detail, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (extraction_id, severity, code, detail, extracted_at),
                )
            if occurred_at and canonical_occurred is None:
                conn.execute(
                    "UPDATE objects SET occurred_at = ? WHERE object_id = ?",
                    (occurred_at, object_id),
                )
                conn.execute(
                    """
                    UPDATE object_versions SET occurred_at = ?
                    WHERE object_id = ? AND content_hash = ?
                    """,
                    (occurred_at, object_id, content_hash),
                )
        return ExtractionResult(
            extraction_id,
            object_id,
            blob_id,
            "inserted",
            status,
            metadata,
            occurred_at,
            tuple(code for code, _ in diagnostics),
        )
    finally:
        conn.close()
