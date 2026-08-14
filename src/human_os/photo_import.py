"""Batch CLI: ingest a folder of JPEG photos and run PHOTO semantic extraction."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .ingestion import IngestObject, ingest_object
from .photo_semantic import PhotoVisionBackend, SemanticRunResult, run_photo_semantics

_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})


@dataclass(frozen=True)
class PhotoImportOutcome:
    filename: str
    object_id: str | None
    ingestion_outcome: str | None
    semantic_status: str | None
    error: str | None


def _jpeg_files(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.casefold() in _JPEG_SUFFIXES
    )


def _semantic_status(results: dict[str, SemanticRunResult]) -> str:
    statuses = {result.status for result in results.values()}
    if "failed" in statuses:
        return "failed"
    if statuses - {"complete"}:
        return "partial"
    return "complete"


def import_photo(
    photo: Path,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> PhotoImportOutcome:
    """Ingest one JPEG and run PHOTO semantics; never modifies the source file."""
    try:
        item = IngestObject(
            object_type="photo",
            source="local.photo_import",
            raw_uri=photo.resolve().as_uri(),
            mime_type="image/jpeg",
        )
        ingested = ingest_object(item, database_path, schema_path)
    except Exception as exc:
        return PhotoImportOutcome(
            photo.name, None, None, None, f"{type(exc).__name__}: {exc}"
        )

    try:
        results = run_photo_semantics(
            ingested.object_id, database_path, schema_path, backend=backend
        )
    except Exception as exc:
        return PhotoImportOutcome(
            photo.name, ingested.object_id, ingested.outcome, None,
            f"{type(exc).__name__}: {exc}",
        )

    return PhotoImportOutcome(
        photo.name, ingested.object_id, ingested.outcome,
        _semantic_status(results), None,
    )


def import_folder(
    folder: Path,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
) -> list[PhotoImportOutcome]:
    """Ingest every top-level .jpg/.jpeg file in folder; never recurses."""
    return [
        import_photo(photo, database_path, schema_path, backend=backend)
        for photo in _jpeg_files(folder)
    ]


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
