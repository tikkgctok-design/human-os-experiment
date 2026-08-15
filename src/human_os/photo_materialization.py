"""Temporary, provider-neutral local access for remote photo content."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlparse

from .photo_source import PhotoSource, SourcePhoto

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class MaterializedPhoto:
    """A local execution path whose canonical source identity remains unchanged."""

    path: Path
    temporary: bool
    byte_size: int
    content_hash: str | None


def _local_file(raw_uri: str) -> Path | None:
    parsed = urlparse(raw_uri)
    if parsed.scheme != "file":
        return None
    text = unquote(parsed.path)
    if parsed.netloc:
        text = f"//{parsed.netloc}{text}"
    if len(text) >= 3 and text[0] == "/" and text[2] == ":":
        text = text[1:]
    path = Path(text).resolve()
    return path if path.is_file() else None


def _safe_suffix(name: str) -> str:
    suffix = Path(name).suffix.casefold()
    return suffix if suffix in {".jpg", ".jpeg"} else ".jpg"


@contextmanager
def materialize_photo(
    source: PhotoSource,
    photo: SourcePhoto,
) -> Iterator[MaterializedPhoto]:
    """Yield a local path and remove temporary remote content on every exit path."""
    if photo.content_hash is not None and not _SHA256_PATTERN.fullmatch(
        photo.content_hash
    ):
        raise ValueError("unsupported content_hash format; expected SHA-256")

    local = _local_file(photo.raw_uri)
    if local is not None:
        yield MaterializedPhoto(
            path=local,
            temporary=False,
            byte_size=local.stat().st_size,
            content_hash=photo.content_hash,
        )
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="human-os-photo-", suffix=_safe_suffix(photo.name)
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            with source.open_photo(photo.source_id) as remote:
                while chunk := remote.read(1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise TypeError("photo source must return a binary stream")
                    target.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)

        content_hash = digest.hexdigest()
        if byte_size != photo.byte_size:
            raise ValueError(
                f"materialized byte_size mismatch: expected {photo.byte_size}, got {byte_size}"
            )
        if (
            photo.content_hash is not None
            and content_hash.casefold() != photo.content_hash.casefold()
        ):
            raise ValueError("materialized content_hash mismatch")

        yield MaterializedPhoto(
            path=temporary_path,
            temporary=True,
            byte_size=byte_size,
            content_hash=content_hash,
        )
    finally:
        temporary_path.unlink(missing_ok=True)
