import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from human_os.photo_materialization import materialize_photo
from human_os.photo_source import LocalFolderPhotoSource, SourcePhoto


class RemotePhotoSource:
    def __init__(self, photos: tuple[SourcePhoto, ...], payloads: dict[str, bytes]) -> None:
        self.photos = photos
        self.payloads = payloads

    def list_photos(self) -> tuple[SourcePhoto, ...]:
        return self.photos

    def get_photo(self, source_id: str) -> SourcePhoto:
        for photo in self.photos:
            if photo.source_id == source_id:
                return photo
        raise KeyError(source_id)

    def open_photo(self, source_id: str) -> io.BytesIO:
        return io.BytesIO(self.payloads[source_id])


def _remote_photo(source_id: str, payload: bytes, *, content_hash: str | None = None) -> SourcePhoto:
    return SourcePhoto(
        source_id=source_id,
        source_kind="fixture.remote",
        name=f"{source_id}.jpg",
        mime_type="image/jpeg",
        byte_size=len(payload),
        modified_at=datetime.now(timezone.utc),
        raw_uri=f"fixture-drive://photos/{source_id}",
        content_hash=content_hash,
    )


def test_remote_photo_is_materialized_with_identical_content_and_cleaned_up() -> None:
    payload = b"remote jpeg bytes"
    photo = _remote_photo(
        "one", payload, content_hash=hashlib.sha256(payload).hexdigest()
    )
    source = RemotePhotoSource((photo,), {photo.source_id: payload})

    with materialize_photo(source, photo) as materialized:
        temporary_path = materialized.path
        assert materialized.temporary is True
        assert temporary_path.is_file()
        assert temporary_path.read_bytes() == payload
        assert materialized.byte_size == len(payload)
        assert materialized.content_hash == photo.content_hash

    assert not temporary_path.exists()


def test_remote_photo_is_cleaned_up_when_downstream_raises() -> None:
    payload = b"remote jpeg bytes"
    photo = _remote_photo("failure", payload)
    source = RemotePhotoSource((photo,), {photo.source_id: payload})
    temporary_path: Path | None = None

    with pytest.raises(RuntimeError, match="downstream failed"):
        with materialize_photo(source, photo) as materialized:
            temporary_path = materialized.path
            raise RuntimeError("downstream failed")

    assert temporary_path is not None
    assert not temporary_path.exists()


def test_local_file_uses_fast_path_without_creating_temporary_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "local.jpg"
    path.write_bytes(b"local jpeg bytes")
    source = LocalFolderPhotoSource(tmp_path, source_namespace="local-fast-path")
    photo = source.list_photos()[0]

    def fail_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("local fast path must not create a temporary file")

    monkeypatch.setattr("human_os.photo_materialization.tempfile.mkstemp", fail_mkstemp)

    with materialize_photo(source, photo) as materialized:
        assert materialized.path == path.resolve()
        assert materialized.temporary is False

    assert path.read_bytes() == b"local jpeg bytes"


def test_two_remote_photos_have_distinct_live_temporary_paths() -> None:
    first = _remote_photo("first", b"first")
    second = _remote_photo("second", b"second")
    source = RemotePhotoSource(
        (first, second),
        {first.source_id: b"first", second.source_id: b"second"},
    )

    with materialize_photo(source, first) as first_local:
        with materialize_photo(source, second) as second_local:
            first_path = first_local.path
            second_path = second_local.path
            assert first_path != second_path
            assert first_path.is_file() and second_path.is_file()

    assert not first_path.exists() and not second_path.exists()


def test_remote_content_hash_is_validated_and_failed_file_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"changed remote bytes"
    photo = _remote_photo("hash-mismatch", payload, content_hash="0" * 64)
    source = RemotePhotoSource((photo,), {photo.source_id: payload})
    created_paths: list[Path] = []

    from human_os import photo_materialization

    original_mkstemp = photo_materialization.tempfile.mkstemp

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = original_mkstemp(*args, **kwargs)
        created_paths.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(photo_materialization.tempfile, "mkstemp", recording_mkstemp)

    with pytest.raises(ValueError, match="content_hash mismatch"):
        with materialize_photo(source, photo):
            pass

    assert len(created_paths) == 1
    assert not created_paths[0].exists()


def test_drive_md5_is_not_interpreted_as_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"remote bytes"
    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    photo = _remote_photo("drive-md5", payload, content_hash=md5)
    source = RemotePhotoSource((photo,), {photo.source_id: payload})

    def fail_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("unsupported hash must be rejected before materialization")

    monkeypatch.setattr("human_os.photo_materialization.tempfile.mkstemp", fail_mkstemp)

    with pytest.raises(ValueError, match="unsupported content_hash format"):
        with materialize_photo(source, photo):
            pass
