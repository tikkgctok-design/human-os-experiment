"""Provider-neutral discovery and read-only access for external photo sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping, Protocol, runtime_checkable


JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
LOCAL_FOLDER_SOURCE_KIND = "local_folder"
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SourcePhoto:
    """Normalized immutable descriptor produced by any photo provider."""

    source_id: str
    source_kind: str
    name: str
    mime_type: str
    byte_size: int
    modified_at: datetime
    raw_uri: str
    content_hash: str | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_kind or not self.name or not self.raw_uri:
            raise ValueError("source identity, kind, name and raw URI are required")
        if self.byte_size < 0:
            raise ValueError("byte_size must not be negative")
        if self.modified_at.tzinfo is None:
            raise ValueError("modified_at must be timezone-aware")
        if self.metadata is not None:
            object.__setattr__(self, "metadata", _freeze(self.metadata))


@runtime_checkable
class PhotoSource(Protocol):
    """Minimal contract shared by local folders and future remote providers."""

    def list_photos(self) -> tuple[SourcePhoto, ...]: ...

    def get_photo(self, source_id: str) -> SourcePhoto: ...

    def open_photo(self, source_id: str) -> BinaryIO: ...


class LocalFolderPhotoSource:
    """Non-recursive JPEG source backed by a folder opened strictly read-only."""

    source_kind = LOCAL_FOLDER_SOURCE_KIND

    def __init__(self, folder: Path, *, source_namespace: str) -> None:
        if not _NAMESPACE_PATTERN.fullmatch(source_namespace):
            raise ValueError(
                "source_namespace must contain only letters, digits, '.', '_' or '-'"
            )
        self._folder = Path(folder).resolve()
        if not self._folder.is_dir():
            raise NotADirectoryError(self._folder)
        self._source_namespace = source_namespace

    def _source_id(self, relative_path: str) -> str:
        return f"{self._source_namespace}:{relative_path}"

    def _discover(self) -> tuple[tuple[SourcePhoto, Path], ...]:
        discovered: list[tuple[SourcePhoto, Path]] = []
        for path in self._folder.iterdir():
            if not path.is_file() or path.suffix.casefold() not in JPEG_SUFFIXES:
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(self._folder).as_posix()
            except ValueError:
                # Do not allow a symlink to make a local source escape its root.
                continue
            stat = resolved.stat()
            discovered.append(
                (
                    SourcePhoto(
                        source_id=self._source_id(relative),
                        source_kind=self.source_kind,
                        name=resolved.name,
                        mime_type="image/jpeg",
                        byte_size=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                        raw_uri=resolved.as_uri(),
                        metadata={"relative_path": relative},
                    ),
                    resolved,
                )
            )
        discovered.sort(
            key=lambda item: (
                str(item[0].metadata["relative_path"]).casefold(),
                str(item[0].metadata["relative_path"]),
            )
        )
        return tuple(discovered)

    def list_photos(self) -> tuple[SourcePhoto, ...]:
        return tuple(photo for photo, _ in self._discover())

    def _find(self, source_id: str) -> tuple[SourcePhoto, Path]:
        for photo, path in self._discover():
            if photo.source_id == source_id:
                return photo, path
        raise KeyError(source_id)

    def get_photo(self, source_id: str) -> SourcePhoto:
        return self._find(source_id)[0]

    def open_photo(self, source_id: str) -> BinaryIO:
        """Return a binary stream opened with ``rb``; the caller must close it."""
        return self._find(source_id)[1].open("rb")
