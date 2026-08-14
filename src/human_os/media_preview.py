"""Read-only, object-id based PHOTO previews for the authenticated mobile gateway."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image, ImageOps


OBJECT_ID_PATTERN = re.compile(r"^hos_obj_[0-9a-f]{32}$")
VARIANTS = {"thumbnail": (640, 640, 82), "preview": (1800, 1800, 88)}


def _file_path(raw_uri: str) -> Path:
    parsed = urlparse(raw_uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        raise ValueError("canonical photo does not have a local file URI")
    value = unquote(parsed.path)
    if parsed.netloc:
        value = f"//{parsed.netloc}{value}"
    if len(value) >= 3 and value[0] == "/" and value[2] == ":":
        value = value[1:]
    return Path(value)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True)
class PreviewAsset:
    path: Path
    content_type: str = "image/jpeg"


class PhotoPreviewStore:
    """Resolve RAW privately and expose only derived, orientation-correct JPEGs."""

    def __init__(self, database_path: Path, cache_dir: Path) -> None:
        self.database_path = database_path
        self.cache_dir = cache_dir
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        resolved = self.database_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError("canonical INDEX is unavailable")
        conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _photo(self, object_id: str) -> sqlite3.Row | None:
        if not OBJECT_ID_PATTERN.fullmatch(object_id):
            raise ValueError("invalid object_id")
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT object_id, object_type, raw_uri, content_hash, occurred_at,
                       captured_at, mime_type
                FROM objects WHERE object_id = ?
                """,
                (object_id,),
            ).fetchone()

    def details(self, object_id: str) -> dict[str, Any] | None:
        row = self._photo(object_id)
        if row is None or row["object_type"] != "photo":
            return None
        with closing(self._connect()) as conn:
            metadata_row = conn.execute(
                """
                SELECT metadata_json FROM metadata_extractions
                WHERE object_id = ? ORDER BY extracted_at DESC, extraction_id DESC LIMIT 1
                """,
                (object_id,),
            ).fetchone()
            semantic_rows = conn.execute(
                """
                SELECT semantic_type, result_text, result_json
                FROM semantic_results
                WHERE object_id = ? AND is_current = 1
                  AND status IN ('complete', 'partial')
                ORDER BY semantic_type, semantic_result_id
                """,
                (object_id,),
            ).fetchall()
        metadata = _json(metadata_row["metadata_json"], {}) if metadata_row else {}
        gps = metadata.get("gps") if isinstance(metadata, dict) else None
        safe_gps = None
        if isinstance(gps, dict):
            latitude, longitude = gps.get("latitude"), gps.get("longitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                safe_gps = {"latitude": float(latitude), "longitude": float(longitude)}
        caption = None
        concepts: list[str] = []
        place_candidates: list[dict[str, Any]] = []
        for semantic in semantic_rows:
            kind = semantic["semantic_type"]
            result = _json(semantic["result_json"], {})
            if kind == "image_caption" and semantic["result_text"]:
                caption = str(semantic["result_text"])[:1000]
            elif kind == "detected_objects" and isinstance(result, dict):
                for item in result.get("detections", []):
                    label = item.get("label") if isinstance(item, dict) else None
                    if isinstance(label, str) and label and label not in concepts:
                        concepts.append(label[:100])
            elif kind == "detected_places" and isinstance(result, dict):
                for item in result.get("candidates", []):
                    if not isinstance(item, dict):
                        continue
                    candidate = {
                        key: item[key]
                        for key in ("kind", "latitude", "longitude", "confidence")
                        if key in item and isinstance(item[key], (str, int, float))
                    }
                    if candidate:
                        place_candidates.append(candidate)
        return {
            "occurred_at": row["occurred_at"] or row["captured_at"],
            "gps": safe_gps,
            "place_candidates": place_candidates[:5],
            "caption": caption,
            "concepts": concepts[:20],
            "thumbnail_url": f"/mobile/image/{object_id}?variant=thumbnail",
            "preview_url": f"/mobile/image/{object_id}?variant=preview",
        }

    def asset(self, object_id: str, variant: str) -> PreviewAsset | None:
        if variant not in VARIANTS:
            raise ValueError("invalid preview variant")
        row = self._photo(object_id)
        if row is None or row["object_type"] != "photo":
            return None
        content_hash = row["content_hash"]
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("canonical photo hash is invalid")
        destination = self.cache_dir / variant / f"{content_hash}.jpg"
        if destination.is_file():
            return PreviewAsset(destination)
        source = _file_path(row["raw_uri"])
        if not source.is_file():
            raise FileNotFoundError("canonical photo source is unavailable")
        width, height, quality = VARIANTS[variant]
        with self._lock:
            if destination.is_file():
                return PreviewAsset(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened)
                    if image.mode in {"RGBA", "LA"}:
                        background = Image.new("RGB", image.size, "white")
                        alpha = image.getchannel("A")
                        background.paste(image.convert("RGB"), mask=alpha)
                        image = background
                    else:
                        image = image.convert("RGB")
                    image.thumbnail((width, height), Image.Resampling.LANCZOS)
                    image.save(temporary, format="JPEG", quality=quality, optimize=True)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return PreviewAsset(destination)
