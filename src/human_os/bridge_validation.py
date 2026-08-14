"""Controlled real-archive validation through the external bridge contract."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .search import safe_object_ref
from .search_validation import VALIDATION_QUERIES, _raw_path, _snapshot


def _counts(database_path: Path) -> dict[str, int]:
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


def _request(
    bridge_url: str,
    token: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], float]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    if bridge_url.startswith("http://127.0.0.1"):
        headers["X-Forwarded-Proto"] = "https"
    request = urllib.request.Request(
        bridge_url.rstrip("/") + path, data=data, headers=headers, method=method
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.load(exc)
    return status, payload, round((time.perf_counter() - started) * 1000, 3)


def validate_bridge(
    database_path: Path,
    bridge_url: str,
    token: str,
    output_path: Path,
) -> dict[str, Any]:
    """Validate HTTPS bridge responses while proving INDEX and RAW immutability."""
    database_before = _snapshot(database_path)
    counts_before = _counts(database_path)
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
    raw_before = []
    for object_id, raw_uri, content_hash in raw_rows:
        path = _raw_path(raw_uri)
        if path is not None and path.is_file():
            snapshot = _snapshot(path)
            if snapshot["sha256"] != content_hash:
                raise RuntimeError(f"RAW hash differs from INDEX: {object_id}")
            raw_before.append((object_id, path, snapshot))

    health_status, health, health_latency = _request(
        bridge_url, token, "/bridge/health"
    )
    responses = []
    for query in VALIDATION_QUERIES:
        status, payload, latency = _request(
            bridge_url, token, "/v1/search", body={"query": query, "limit": 10}
        )
        responses.append(
            {
                "query": query,
                "http_status": status,
                "request_id": payload.get("request_id"),
                "latency_ms": latency,
                "response": payload,
            }
        )

    counts_after = _counts(database_path)
    database_after = _snapshot(database_path)
    raw_checks = []
    for object_id, path, before in raw_before:
        after = _snapshot(path)
        raw_checks.append(
            {
                "object_id": object_id,
                "safe_ref": safe_object_ref(object_id),
                "before": before,
                "after": after,
                "unchanged": before == after,
            }
        )
    external_json = json.dumps(responses, ensure_ascii=False)
    report = {
        "report_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bridge_url": bridge_url,
        "health": {
            "http_status": health_status,
            "latency_ms": health_latency,
            "response": health,
        },
        "queries": responses,
        "checks": {
            "health_ok": health_status == 200
            and health.get("bridge_alive") is True
            and health.get("local_api_reachable") is True
            and health.get("bridge_opens_database") is False,
            "all_queries_http_200": all(item["http_status"] == 200 for item in responses),
            "all_queries_have_request_id": all(item["request_id"] for item in responses),
            "all_queries_returned_results": all(
                item["response"].get("results") for item in responses
            ),
            "no_raw_path_leakage": not any(
                marker in external_json
                for marker in ("raw_uri", "file:///", "G:\\\\", "G:/")
            ),
            "database_bytes_size_mtime_unchanged": database_before == database_after,
            "database_counts_unchanged": counts_before == counts_after,
            "semantic_results_unchanged": (
                counts_before["semantic_results"] == counts_after["semantic_results"]
            ),
            "semantic_relations_unchanged": (
                counts_before["semantic_relations"] == counts_after["semantic_relations"]
            ),
            "raw_files_checked": len(raw_checks),
            "raw_bytes_size_mtime_unchanged": all(
                item["unchanged"] for item in raw_checks
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
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("HUMAN_OS_BRIDGE_TOKEN", "")
    if not token:
        raise SystemExit("HUMAN_OS_BRIDGE_TOKEN is required")
    report = validate_bridge(args.db, args.bridge_url, token, args.output)
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
