"""End-to-end read-only retrieval validation against a bounded real archive index."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .search import safe_object_ref
from .search_api import ThreadingHTTPServer, make_handler


VALIDATION_QUERIES = (
    "найди фотографии со снегом",
    "найди фотографии, где человек на снегу",
    "найди меня в плавках на снегу",
    "найди фото с вывеской",
)


def _raw_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    text = unquote(parsed.path)
    if parsed.netloc:
        text = f"//{parsed.netloc}{text}"
    if len(text) >= 3 and text[0] == "/" and text[2] == ":":
        text = text[1:]
    return Path(text)


def _snapshot(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "byte_size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _db_counts(database_path: Path) -> dict[str, int]:
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only = ON")
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "objects", "blobs", "blob_locations", "attachments", "relations",
                "object_versions", "metadata_extractions", "semantic_results",
                "semantic_relations", "event_evidence",
            )
        }


def _api_queries(database_path: Path, limit: int) -> list[dict[str, Any]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(database_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/search"
        responses = []
        for query in VALIDATION_QUERIES:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps({"query": query, "limit": limit}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                responses.append(json.load(response))
        return responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def validate_search(database_path: Path, output_path: Path, *, limit: int = 10) -> dict[str, Any]:
    """Run the Issue #1 queries and prove that INDEX and referenced RAW stay unchanged."""
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        raw_rows = conn.execute(
            """
            SELECT DISTINCT o.object_id, o.raw_uri, o.content_hash
            FROM objects o JOIN semantic_results s ON s.object_id = o.object_id
            WHERE o.object_type = 'photo' AND s.is_current = 1
            ORDER BY o.object_id
            """
        ).fetchall()
    raw_files = []
    for object_id, raw_uri, content_hash in raw_rows:
        path = _raw_path(raw_uri)
        if path is not None and path.is_file():
            before = _snapshot(path)
            if before["sha256"] != content_hash:
                raise RuntimeError(f"RAW hash differs from INDEX: {object_id}")
            raw_files.append((object_id, raw_uri, path, before))

    database_before = _snapshot(database_path)
    counts_before = _db_counts(database_path)
    responses = _api_queries(database_path, limit)
    counts_after = _db_counts(database_path)
    database_after = _snapshot(database_path)
    raw_checks = []
    for object_id, raw_uri, path, before in raw_files:
        after = _snapshot(path)
        raw_checks.append({
            "object_id": object_id, "safe_ref": safe_object_ref(object_id), "before": before,
            "after": after, "unchanged": before == after,
        })

    report = {
        "report_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database_path.resolve()),
        "queries": responses,
        "checks": {
            "database_bytes_size_mtime_unchanged": database_before == database_after,
            "database_counts_unchanged": counts_before == counts_after,
            "raw_files_checked": len(raw_checks),
            "raw_bytes_size_mtime_unchanged": all(item["unchanged"] for item in raw_checks),
            "all_queries_returned_results": all(item["results"] for item in responses),
            "swimwear_is_explicit_best_effort": (
                responses[2]["best_effort"]
                and "swimwear" in responses[2]["unsupported_concepts"]
            ),
        },
        "database_snapshot": {"before": database_before, "after": database_after},
        "counts": {"before": counts_before, "after": counts_after},
        "raw_checks": raw_checks,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    report = validate_search(args.db, args.output, limit=args.limit)
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
