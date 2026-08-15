"""Google Drive adapter for the provider-neutral PhotoSource contract."""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any, BinaryIO, Mapping, Protocol
from urllib.parse import quote

from .photo_source import SourcePhoto

GOOGLE_DRIVE_SOURCE_KIND = "google_drive"
SUPPORTED_GOOGLE_DRIVE_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,"
    "version,parents,trashed"
)


class GoogleDrivePhotoSourceError(Exception):
    """Base error whose message never includes provider credentials or responses."""


class GoogleDrivePhotoNotFoundError(GoogleDrivePhotoSourceError, KeyError):
    """The requested Drive file is missing, deleted, or outside the adapter scope."""


class GoogleDrivePhotoMetadataError(GoogleDrivePhotoSourceError, ValueError):
    """Drive returned metadata that cannot form a valid SourcePhoto."""


class DriveRequest(Protocol):
    def execute(self) -> Any: ...


class DriveFilesResource(Protocol):
    def list(self, **kwargs: Any) -> DriveRequest: ...

    def get(self, **kwargs: Any) -> DriveRequest: ...

    def get_media(self, **kwargs: Any) -> DriveRequest: ...


class DriveService(Protocol):
    def files(self) -> DriveFilesResource: ...


def _http_status(exc: Exception) -> int | None:
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def _parse_timestamp(value: Any, field: str, *, required: bool) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise GoogleDrivePhotoMetadataError(f"Drive metadata field is invalid: {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleDrivePhotoMetadataError(
            f"Drive metadata field is invalid: {field}"
        ) from exc
    if parsed.tzinfo is None:
        raise GoogleDrivePhotoMetadataError(
            f"Drive metadata timestamp has no timezone: {field}"
        )
    return parsed


def _optional_text(metadata: Mapping[str, Any], field: str) -> str | None:
    value = metadata.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GoogleDrivePhotoMetadataError(f"Drive metadata field is invalid: {field}")
    return value


class GoogleDrivePhotoSource:
    """Adapt an authenticated Google Drive service without owning OAuth or the SDK."""

    source_kind = GOOGLE_DRIVE_SOURCE_KIND

    def __init__(
        self,
        service: DriveService,
        *,
        folder_id: str,
        source_namespace: str,
        page_size: int = 1000,
    ) -> None:
        if not _FILE_ID_PATTERN.fullmatch(folder_id):
            raise ValueError("folder_id is invalid")
        if not _NAMESPACE_PATTERN.fullmatch(source_namespace):
            raise ValueError("source_namespace is invalid")
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        self._service = service
        self._folder_id = folder_id
        self._source_namespace = source_namespace
        self._page_size = page_size
        self._known: dict[str, SourcePhoto] = {}

    def _source_id(self, file_id: str) -> str:
        return f"{self._source_namespace}:{file_id}"

    def _file_id(self, source_id: str) -> str:
        prefix = f"{self._source_namespace}:"
        if not source_id.startswith(prefix):
            raise GoogleDrivePhotoNotFoundError("Google Drive photo is not in this source")
        file_id = source_id[len(prefix) :]
        if not _FILE_ID_PATTERN.fullmatch(file_id):
            raise GoogleDrivePhotoNotFoundError("Google Drive photo identity is invalid")
        return file_id

    def _execute(self, request: DriveRequest, operation: str) -> Any:
        try:
            return request.execute()
        except Exception as exc:
            if _http_status(exc) == 404:
                raise GoogleDrivePhotoNotFoundError(
                    "Google Drive photo does not exist"
                ) from exc
            raise GoogleDrivePhotoSourceError(
                f"Google Drive {operation} failed"
            ) from exc

    def _photo(self, metadata: Any) -> SourcePhoto:
        if not isinstance(metadata, Mapping):
            raise GoogleDrivePhotoMetadataError("Drive file metadata is not an object")
        file_id = metadata.get("id")
        name = metadata.get("name")
        mime_type = metadata.get("mimeType")
        if not isinstance(file_id, str) or not _FILE_ID_PATTERN.fullmatch(file_id):
            raise GoogleDrivePhotoMetadataError("Drive metadata field is invalid: id")
        if not isinstance(name, str) or not name:
            raise GoogleDrivePhotoMetadataError("Drive metadata field is invalid: name")
        if mime_type not in SUPPORTED_GOOGLE_DRIVE_IMAGE_MIMES:
            raise GoogleDrivePhotoMetadataError("Drive file is not a supported image")
        if metadata.get("trashed") is True:
            raise GoogleDrivePhotoNotFoundError("Google Drive photo is deleted")

        size_value = metadata.get("size")
        if size_value is None:
            byte_size = None
        elif isinstance(size_value, bool):
            raise GoogleDrivePhotoMetadataError(
                "Drive metadata field is invalid: size"
            )
        else:
            try:
                byte_size = int(size_value)
            except (TypeError, ValueError) as exc:
                raise GoogleDrivePhotoMetadataError(
                    "Drive metadata field is invalid: size"
                ) from exc
            if byte_size < 0:
                raise GoogleDrivePhotoMetadataError(
                    "Drive metadata field is invalid: size"
                )

        modified_at = _parse_timestamp(
            metadata.get("modifiedTime"), "modifiedTime", required=True
        )
        created_at = _parse_timestamp(
            metadata.get("createdTime"), "createdTime", required=False
        )
        parents = metadata.get("parents", [])
        if not isinstance(parents, list) or any(
            not isinstance(parent, str) for parent in parents
        ):
            raise GoogleDrivePhotoMetadataError(
                "Drive metadata field is invalid: parents"
            )
        md5_checksum = _optional_text(metadata, "md5Checksum")
        version = _optional_text(metadata, "version")
        source_id = self._source_id(file_id)
        return SourcePhoto(
            source_id=source_id,
            source_kind=self.source_kind,
            name=name,
            mime_type=mime_type,
            byte_size=byte_size,
            modified_at=modified_at,
            raw_uri=(
                f"gdrive://{quote(self._source_namespace, safe='')}/files/"
                f"{quote(file_id, safe='')}"
            ),
            content_hash=None,
            metadata={
                "provider": "google_drive",
                "drive_file_id": file_id,
                "drive_md5_checksum": md5_checksum,
                "drive_created_time": created_at.isoformat() if created_at else None,
                "drive_modified_time": modified_at.isoformat(),
                "drive_version": version,
                "drive_parents": tuple(parents),
            },
        )

    def list_photos(self) -> tuple[SourcePhoto, ...]:
        resource = self._service.files()
        page_token: str | None = None
        seen_tokens: set[str] = set()
        photos: list[SourcePhoto] = []
        while True:
            options: dict[str, Any] = {
                "q": f"'{self._folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": self._page_size,
            }
            if page_token is not None:
                options["pageToken"] = page_token
            response = self._execute(resource.list(**options), "list")
            if not isinstance(response, Mapping):
                raise GoogleDrivePhotoMetadataError("Drive list response is not an object")
            files = response.get("files", [])
            if not isinstance(files, list):
                raise GoogleDrivePhotoMetadataError("Drive list files field is invalid")
            for metadata in files:
                if not isinstance(metadata, Mapping):
                    raise GoogleDrivePhotoMetadataError(
                        "Drive file metadata is not an object"
                    )
                if metadata.get("mimeType") not in SUPPORTED_GOOGLE_DRIVE_IMAGE_MIMES:
                    continue
                photo = self._photo(metadata)
                photos.append(photo)
                self._known[photo.source_id] = photo

            next_token = response.get("nextPageToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token:
                raise GoogleDrivePhotoMetadataError("Drive nextPageToken is invalid")
            if next_token in seen_tokens:
                raise GoogleDrivePhotoSourceError("Google Drive pagination token repeated")
            seen_tokens.add(next_token)
            page_token = next_token
        return tuple(sorted(photos, key=lambda photo: photo.source_id))

    def get_photo(self, source_id: str) -> SourcePhoto:
        file_id = self._file_id(source_id)
        response = self._execute(
            self._service.files().get(
                fileId=file_id,
                fields=_FILE_FIELDS,
                supportsAllDrives=True,
            ),
            "get",
        )
        photo = self._photo(response)
        if photo.source_id != source_id:
            raise GoogleDrivePhotoMetadataError("Drive returned an unexpected file id")
        self._known[source_id] = photo
        return photo

    def open_photo(self, source_id: str) -> BinaryIO:
        file_id = self._file_id(source_id)
        if source_id not in self._known:
            self.get_photo(source_id)
        payload = self._execute(
            self._service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            ),
            "download",
        )
        if not isinstance(payload, bytes):
            raise GoogleDrivePhotoSourceError(
                "Google Drive download did not return binary content"
            )
        return io.BytesIO(payload)
