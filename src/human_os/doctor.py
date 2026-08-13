"""Read-only diagnostic command reporting local Human OS INDEX and search health."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .schema import SCHEMA_VERSION
from .search import retrieval_health, search_index


_COUNT_TABLES = (
    "objects", "blobs", "attachments", "semantic_results", "metadata_extractions",
)

# A concept that always exists in the search vocabulary, so the probe exercises the
# real read-only query path regardless of what (if anything) the index contains.
_PROBE_QUERY = "person"


def _table_counts(database_path: Path) -> dict[str, int]:
    resolved = database_path.resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _COUNT_TABLES
        }
    finally:
        conn.close()


def run_doctor(database_path: Path) -> dict[str, Any]:
    """Report INDEX/search health read-only. Never writes, migrates or runs extractors."""
    report: dict[str, Any] = {
        "database_path": str(database_path),
        "database_available": False,
        "schema_version": None,
        "schema_up_to_date": None,
        "counts": None,
        "search_available": False,
        "issues": [],
    }

    try:
        health = retrieval_health(database_path)
    except (FileNotFoundError, sqlite3.DatabaseError) as exc:
        report["issues"].append(f"database is unavailable: {exc}")
        report["status"] = "ERROR"
        return report

    report["database_available"] = True
    report["schema_version"] = health["schema_version"]
    report["schema_up_to_date"] = health["schema_version"] == SCHEMA_VERSION
    if not report["schema_up_to_date"]:
        report["issues"].append(
            f"schema_version {health['schema_version']} does not match expected "
            f"{SCHEMA_VERSION}"
        )

    try:
        report["counts"] = _table_counts(database_path)
    except sqlite3.DatabaseError as exc:
        report["issues"].append(f"object counts unavailable: {exc}")

    try:
        search_index(_PROBE_QUERY, database_path, limit=1)
        report["search_available"] = True
    except (FileNotFoundError, sqlite3.DatabaseError, ValueError) as exc:
        report["issues"].append(f"read-only search is unavailable: {exc}")

    report["status"] = "OK" if not report["issues"] else "WARNING"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    report = run_doctor(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
