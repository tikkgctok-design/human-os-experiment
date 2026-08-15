from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from human_os.photo_source import (
    LOCAL_FOLDER_SOURCE_KIND,
    LocalFolderPhotoSource,
    PhotoSource,
)


def _write(path: Path, content: bytes = b"jpeg fixture") -> Path:
    path.write_bytes(content)
    return path


def _source(folder: Path, namespace: str = "test-photos") -> LocalFolderPhotoSource:
    return LocalFolderPhotoSource(folder, source_namespace=namespace)


def test_discovers_jpeg_and_normalizes_descriptor(tmp_path: Path) -> None:
    photo = _write(tmp_path / "photo.jpg", b"jpeg bytes")

    discovered = _source(tmp_path).list_photos()

    assert len(discovered) == 1
    item = discovered[0]
    assert item.source_kind == LOCAL_FOLDER_SOURCE_KIND
    assert item.name == "photo.jpg"
    assert item.mime_type == "image/jpeg"
    assert item.byte_size == len(b"jpeg bytes")
    assert item.modified_at.tzinfo is not None
    assert item.raw_uri == photo.resolve().as_uri()
    assert item.content_hash is None
    assert item.metadata == {"relative_path": "photo.jpg"}
    assert isinstance(_source(tmp_path), PhotoSource)


def test_ignores_other_files_and_does_not_recurse(tmp_path: Path) -> None:
    _write(tmp_path / "photo.jpg")
    _write(tmp_path / "image.png")
    _write(tmp_path / "notes.txt")
    nested = tmp_path / "nested"
    nested.mkdir()
    _write(nested / "inside.jpeg")

    assert [item.name for item in _source(tmp_path).list_photos()] == ["photo.jpg"]


def test_deterministic_case_insensitive_order(tmp_path: Path) -> None:
    for name in ("z.jpg", "Beta.jpeg", "alpha.jpg"):
        _write(tmp_path / name)

    source = _source(tmp_path)

    assert [item.name for item in source.list_photos()] == [
        "alpha.jpg", "Beta.jpeg", "z.jpg"
    ]
    assert source.list_photos() == source.list_photos()


def test_source_id_is_namespace_plus_relative_path_and_survives_root_move(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(); second_root.mkdir()
    _write(first_root / "same.jpg")
    _write(second_root / "same.jpg")

    first = _source(first_root, "archive-a").list_photos()[0]
    second = _source(second_root, "archive-a").list_photos()[0]
    other_namespace = _source(first_root, "archive-b").list_photos()[0]

    assert first.source_id == "archive-a:same.jpg"
    assert first.source_id == second.source_id
    assert first.source_id != other_namespace.source_id
    assert str(first_root.resolve()) not in first.source_id


def test_get_photo_returns_same_normalized_photo(tmp_path: Path) -> None:
    _write(tmp_path / "one.jpg")
    source = _source(tmp_path)
    listed = source.list_photos()[0]

    assert source.get_photo(listed.source_id) == listed


def test_open_photo_is_binary_read_only_and_does_not_change_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "one.jpg", b"original bytes")
    source = _source(tmp_path)
    item = source.list_photos()[0]
    before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)

    with source.open_photo(item.source_id) as stream:
        assert stream.read() == b"original bytes"
        assert stream.writable() is False

    after = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
    assert after == before


def test_nonexistent_source_id_raises_key_error(tmp_path: Path) -> None:
    _write(tmp_path / "one.jpg")

    with pytest.raises(KeyError):
        _source(tmp_path).get_photo("test-photos:missing.jpg")
    with pytest.raises(KeyError):
        _source(tmp_path).open_photo("test-photos:../one.jpg")


def test_empty_folder_returns_empty_tuple(tmp_path: Path) -> None:
    assert _source(tmp_path).list_photos() == ()


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    for name in ("one.JPG", "two.JpEg", "three.jpeg"):
        _write(tmp_path / name)

    assert [item.name for item in _source(tmp_path).list_photos()] == [
        "one.JPG", "three.jpeg", "two.JpEg"
    ]


def test_source_photo_and_metadata_are_immutable(tmp_path: Path) -> None:
    _write(tmp_path / "one.jpg")
    photo = _source(tmp_path).list_photos()[0]

    with pytest.raises(FrozenInstanceError):
        photo.name = "changed.jpg"  # type: ignore[misc]
    with pytest.raises(TypeError):
        photo.metadata["relative_path"] = "changed.jpg"  # type: ignore[index]
