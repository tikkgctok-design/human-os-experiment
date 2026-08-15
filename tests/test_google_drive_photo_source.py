import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from human_os.google_drive_photo_source import (
    GOOGLE_DRIVE_SOURCE_KIND,
    GoogleDrivePhotoMetadataError,
    GoogleDrivePhotoNotFoundError,
    GoogleDrivePhotoSource,
    GoogleDrivePhotoSourceError,
)
from human_os.photo_materialization import materialize_photo
from human_os.photo_source import PhotoSource


class FakeHttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class FakeRequest:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def execute(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


class FakeDriveFiles:
    def __init__(
        self,
        *,
        pages: dict[str | None, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        payloads: dict[str, Any] | None = None,
        list_error: Exception | None = None,
        download_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.pages = pages or {None: {"files": []}}
        self.metadata = metadata or {}
        self.payloads = payloads or {}
        self.list_error = list_error
        self.download_errors = download_errors or {}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> FakeRequest:
        self.list_calls.append(kwargs)
        if self.list_error is not None:
            return FakeRequest(error=self.list_error)
        return FakeRequest(result=self.pages.get(kwargs.get("pageToken")))

    def get(self, **kwargs: Any) -> FakeRequest:
        self.get_calls.append(kwargs)
        file_id = kwargs["fileId"]
        if file_id not in self.metadata:
            return FakeRequest(error=FakeHttpError(404))
        return FakeRequest(result=self.metadata[file_id])

    def get_media(self, **kwargs: Any) -> FakeRequest:
        self.download_calls.append(kwargs)
        file_id = kwargs["fileId"]
        if file_id in self.download_errors:
            return FakeRequest(error=self.download_errors[file_id])
        if file_id not in self.payloads:
            return FakeRequest(error=FakeHttpError(404))
        return FakeRequest(result=self.payloads[file_id])


class FakeDriveService:
    def __init__(self, files: FakeDriveFiles) -> None:
        self.resource = files

    def files(self) -> FakeDriveFiles:
        return self.resource


def _drive_file(
    file_id: str = "driveFile1",
    *,
    name: str = "photo.jpg",
    mime_type: str = "image/jpeg",
    size: str | None = "123",
    md5: str | None = "0123456789abcdef0123456789abcdef",
    modified: str = "2026-08-15T01:02:03.456Z",
    created: str | None = "2025-01-02T03:04:05Z",
    version: str | None = "17",
    parents: list[str] | None = None,
    trashed: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": modified,
        "parents": parents if parents is not None else ["folderA"],
        "trashed": trashed,
    }
    if size is not None:
        result["size"] = size
    if md5 is not None:
        result["md5Checksum"] = md5
    if created is not None:
        result["createdTime"] = created
    if version is not None:
        result["version"] = version
    return result


def _source(files: FakeDriveFiles) -> GoogleDrivePhotoSource:
    return GoogleDrivePhotoSource(
        FakeDriveService(files),
        folder_id="folderA",
        source_namespace="personal-drive",
        page_size=25,
    )


def test_drive_jpeg_maps_to_source_photo_without_treating_md5_as_sha256() -> None:
    metadata = _drive_file()
    source = _source(FakeDriveFiles(pages={None: {"files": [metadata]}}))

    photo = source.list_photos()[0]

    assert isinstance(source, PhotoSource)
    assert photo.source_id == "personal-drive:driveFile1"
    assert photo.source_kind == GOOGLE_DRIVE_SOURCE_KIND
    assert photo.name == "photo.jpg"
    assert photo.mime_type == "image/jpeg"
    assert photo.byte_size == 123
    assert photo.raw_uri == "gdrive://personal-drive/files/driveFile1"
    assert photo.content_hash is None
    assert photo.metadata["drive_file_id"] == "driveFile1"
    assert photo.metadata["drive_md5_checksum"] == metadata["md5Checksum"]
    assert photo.metadata["drive_version"] == "17"
    assert photo.metadata["drive_parents"] == ("folderA",)
    json.dumps(dict(photo.metadata))


def test_rename_folder_move_and_revision_change_do_not_change_identity() -> None:
    original = _drive_file(name="before.jpg", version="17", parents=["folderA"])
    changed = _drive_file(name="after.jpg", version="18", parents=["folderB"])
    first = _source(FakeDriveFiles(pages={None: {"files": [original]}})).list_photos()[0]
    second = GoogleDrivePhotoSource(
        FakeDriveService(FakeDriveFiles(pages={None: {"files": [changed]}})),
        folder_id="folderB",
        source_namespace="personal-drive",
    ).list_photos()[0]

    assert first.source_id == second.source_id
    assert first.raw_uri == second.raw_uri
    assert first.name != second.name
    assert first.metadata["drive_parents"] != second.metadata["drive_parents"]
    assert first.metadata["drive_version"] != second.metadata["drive_version"]


def test_pagination_and_non_image_filtering() -> None:
    first_page = {
        "files": [
            _drive_file("jpeg1"),
            _drive_file("doc1", mime_type="application/vnd.google-apps.document"),
            _drive_file("video1", mime_type="video/mp4"),
        ],
        "nextPageToken": "page-2",
    }
    second_page = {
        "files": [
            _drive_file("png1", name="screen.png", mime_type="image/png"),
            _drive_file("pdf1", name="file.pdf", mime_type="application/pdf"),
        ]
    }
    files = FakeDriveFiles(pages={None: first_page, "page-2": second_page})

    photos = _source(files).list_photos()

    assert [photo.source_id for photo in photos] == [
        "personal-drive:jpeg1",
        "personal-drive:png1",
    ]
    assert len(files.list_calls) == 2
    assert "pageToken" not in files.list_calls[0]
    assert files.list_calls[1]["pageToken"] == "page-2"
    assert "'folderA' in parents" in files.list_calls[0]["q"]


@pytest.mark.parametrize(
    "mime_type",
    ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"],
)
def test_supported_image_mime_types(mime_type: str) -> None:
    metadata = _drive_file(mime_type=mime_type)
    source = _source(FakeDriveFiles(pages={None: {"files": [metadata]}}))

    assert source.list_photos()[0].mime_type == mime_type


def test_unknown_size_and_missing_md5_are_allowed() -> None:
    metadata = _drive_file(size=None, md5=None)
    source = _source(FakeDriveFiles(pages={None: {"files": [metadata]}}))

    photo = source.list_photos()[0]

    assert photo.byte_size is None
    assert photo.content_hash is None
    assert photo.metadata["drive_md5_checksum"] is None


def test_get_photo_loads_current_metadata_and_missing_file_is_normalized() -> None:
    metadata = _drive_file(version="18")
    source = _source(FakeDriveFiles(metadata={"driveFile1": metadata}))

    assert source.get_photo("personal-drive:driveFile1").metadata["drive_version"] == "18"
    with pytest.raises(GoogleDrivePhotoNotFoundError):
        source.get_photo("personal-drive:missing")


def test_deleted_file_and_malformed_metadata_are_rejected() -> None:
    deleted = _drive_file(trashed=True)
    malformed = _drive_file()
    del malformed["modifiedTime"]

    with pytest.raises(GoogleDrivePhotoNotFoundError):
        _source(FakeDriveFiles(metadata={"driveFile1": deleted})).get_photo(
            "personal-drive:driveFile1"
        )
    with pytest.raises(GoogleDrivePhotoMetadataError, match="modifiedTime"):
        _source(FakeDriveFiles(pages={None: {"files": [malformed]}})).list_photos()


def test_list_failure_is_normalized_without_provider_error_details() -> None:
    source = _source(FakeDriveFiles(list_error=RuntimeError("private provider detail")))

    with pytest.raises(GoogleDrivePhotoSourceError) as captured:
        source.list_photos()

    assert str(captured.value) == "Google Drive list failed"
    assert "private provider detail" not in str(captured.value)


def test_open_photo_returns_binary_stream_and_download_failure_is_normalized() -> None:
    metadata = _drive_file()
    good_files = FakeDriveFiles(
        pages={None: {"files": [metadata]}}, payloads={"driveFile1": b"jpeg bytes"}
    )
    good_source = _source(good_files)
    photo = good_source.list_photos()[0]

    with good_source.open_photo(photo.source_id) as stream:
        assert isinstance(stream, io.BytesIO)
        assert stream.read() == b"jpeg bytes"

    bad_files = FakeDriveFiles(
        pages={None: {"files": [metadata]}},
        download_errors={"driveFile1": RuntimeError("private download detail")},
    )
    bad_source = _source(bad_files)
    bad_source.list_photos()
    with pytest.raises(GoogleDrivePhotoSourceError) as captured:
        bad_source.open_photo(photo.source_id)
    assert str(captured.value) == "Google Drive download failed"


def test_materialization_accepts_google_drive_photo_and_cleans_up() -> None:
    payload = b"downloaded photo bytes"
    metadata = _drive_file(size=None, md5=None)
    files = FakeDriveFiles(
        pages={None: {"files": [metadata]}}, payloads={"driveFile1": payload}
    )
    source = _source(files)
    photo = source.list_photos()[0]

    with materialize_photo(source, photo) as materialized:
        path = materialized.path
        assert path.read_bytes() == payload
        assert materialized.temporary is True
        assert materialized.byte_size == len(payload)

    assert not path.exists()
