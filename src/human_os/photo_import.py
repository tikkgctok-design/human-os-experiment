"""Batch CLI: ingest a folder of JPEG photos and run PHOTO semantic extraction."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Iterable

from .ingestion import IngestObject, ingest_object
from .photo_materialization import materialize_photo
from .photo_source import LocalFolderPhotoSource, PhotoSource, SourcePhoto
from .photo_semantic import PhotoVisionBackend, SemanticRunResult, run_photo_semantics

_LEGACY_SOURCE = "local.photo_import"


@dataclass(frozen=True)
class PhotoImportOutcome:
    filename: str
    object_id: str | None
    ingestion_outcome: str | None
    semantic_status: str | None
    error: str | None


class _LegacyLocalFolderSource:
    """Keep pre-PhotoSource canonical identity for the existing folder CLI."""

    def __init__(self, folder: Path) -> None:
        self._delegate = LocalFolderPhotoSource(
            folder, source_namespace="local-photo-import"
        )

    @staticmethod
    def _legacy(photo: SourcePhoto) -> SourcePhoto:
        return replace(
            photo,
            source_id=photo.raw_uri,
            source_kind=_LEGACY_SOURCE,
            metadata=None,
        )

    def list_photos(self) -> tuple[SourcePhoto, ...]:
        return tuple(self._legacy(photo) for photo in self._delegate.list_photos())

    def _delegate_photo(self, source_id: str) -> SourcePhoto:
        for photo in self._delegate.list_photos():
            if photo.raw_uri == source_id:
                return photo
        raise KeyError(source_id)

    def get_photo(self, source_id: str) -> SourcePhoto:
        return self._legacy(self._delegate_photo(source_id))

    def open_photo(self, source_id: str) -> BinaryIO:
        photo = self._delegate_photo(source_id)
        return self._delegate.open_photo(photo.source_id)


def _semantic_status(results: dict[str, SemanticRunResult]) -> str:
    statuses = {result.status for result in results.values()}
    if "failed" in statuses:
        return "failed"
    if statuses - {"complete"}:
        return "partial"
    return "complete"


def _ingest_and_extract(
    item: IngestObject,
    filename: str,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
    materialized_path: Path | None = None,
) -> PhotoImportOutcome:
    try:
        ingested = ingest_object(item, database_path, schema_path)
    except Exception as exc:
        return PhotoImportOutcome(
            filename, None, None, None, f"{type(exc).__name__}: {exc}"
        )

    try:
        semantic_options = {"backend": backend}
        if materialized_path is not None:
            semantic_options["materialized_path"] = materialized_path
        results = run_photo_semantics(
            ingested.object_id,
            database_path,
            schema_path,
            **semantic_options,
        )
    except Exception as exc:
        return PhotoImportOutcome(
            filename,
            ingested.object_id,
            ingested.outcome,
            None,
            f"{type(exc).__name__}: {exc}",
        )

    return PhotoImportOutcome(
        filename,
        ingested.object_id,
        ingested.outcome,
        _semantic_status(results),
        None,
    )


def import_source_photo(
    source: PhotoSource,
    photo: SourcePhoto,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> PhotoImportOutcome:
    """Ingest one normalized provider photo and run the existing PHOTO pipeline."""
    try:
        with materialize_photo(source, photo) as materialized:
            content = materialized.path.read_bytes() if materialized.temporary else None
            item = IngestObject(
                object_type="photo",
                source=photo.source_kind,
                source_id=photo.source_id,
                raw_uri=photo.raw_uri,
                content=content,
                mime_type=photo.mime_type,
                metadata=dict(photo.metadata or {}),
            )
            return _ingest_and_extract(
                item,
                photo.name,
                database_path,
                schema_path,
                backend=backend,
                materialized_path=materialized.path,
            )
    except Exception as exc:
        return PhotoImportOutcome(
            photo.name, None, None, None, f"{type(exc).__name__}: {exc}"
        )


def import_photo_source(
    source: PhotoSource,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> list[PhotoImportOutcome]:
    """Import every photo exposed by a provider-neutral read-only source."""
    return [
        import_source_photo(source, photo, database_path, schema_path, backend=backend)
        for photo in source.list_photos()
    ]


def import_photo(
    photo: Path,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> PhotoImportOutcome:
    """Ingest one JPEG and run PHOTO semantics; never modifies the source file."""
    item = IngestObject(
        object_type="photo",
        source=_LEGACY_SOURCE,
        raw_uri=photo.resolve().as_uri(),
        mime_type="image/jpeg",
    )
    return _ingest_and_extract(
        item, photo.name, database_path, schema_path, backend=backend
    )


def import_folder(
    folder: Path,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> list[PhotoImportOutcome]:
    """Ingest every top-level .jpg/.jpeg file in folder; never recurses."""
    return import_photo_source(
        _LegacyLocalFolderSource(folder),
        database_path,
        schema_path,
        backend=backend,
    )


def _is_failure(outcome: PhotoImportOutcome) -> bool:
    return outcome.error is not None or outcome.semantic_status == "failed"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    folder: Path = args.folder
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 2

    outcomes = import_folder(folder, args.db, args.schema)
    for outcome in outcomes:
        if outcome.error is not None:
            print(f"{outcome.filename}\tERROR: {outcome.error}")
        else:
            print(
                f"{outcome.filename}\tobject_id={outcome.object_id}\t"
                f"ingest={outcome.ingestion_outcome}\tsemantic={outcome.semantic_status}"
            )

    total = len(outcomes)
    failed = sum(1 for outcome in outcomes if _is_failure(outcome))
    print(f"total={total} succeeded={total - failed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
