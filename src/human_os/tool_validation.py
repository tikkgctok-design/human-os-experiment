"""Controlled validation of the ChatGPT-facing tool over an HTTPS bridge."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_validation import _counts, _request
from .search import search_index
from .search_validation import _raw_path, _snapshot
from .tool_client import HumanOSToolClient, ToolConfig


VALIDATION_QUERIES = (
    "найди где я на снегу",
    "найди фотографии со снегом",
    "найди меня в плавках на снегу",
    "найди фото с вывеской",
)


def validate_tool(
    database_path: Path,
    tool_url: str,
    tool_token: str,
    bridge_url: str,
    bridge_token: str,
    output_path: Path,
    source_address: str | None = None,
) -> dict[str, Any]:
    """Compare tool, bridge, and local ranks while proving storage immutability."""
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

    client = HumanOSToolClient(ToolConfig(
        tool_url, tool_token, timeout=30,
        allow_http_loopback_internal=tool_url.startswith("http://127.0.0.1"),
        source_address=source_address,
    ))
    validations = []
    for query in VALIDATION_QUERIES:
        started = time.perf_counter()
        tool_response = client.human_os_search(query, 10)
        tool_latency = round((time.perf_counter() - started) * 1000, 3)
        bridge_status, bridge_response, bridge_latency = _request(
            bridge_url, bridge_token, "/v1/search", body={"query": query, "limit": 10}
        )
        local_response = search_index(query, database_path, limit=10).to_dict()
        tool_ids = [row["object_id"] for row in tool_response.get("results", [])]
        bridge_ids = [row["object_id"] for row in bridge_response.get("results", [])]
        local_ids = [row["object_id"] for row in local_response.get("results", [])]
        validations.append(
            {
                "query": query,
                "http_status": bridge_status,
                "request_id": tool_response.get("request_id"),
                "tool_latency_ms": tool_latency,
                "bridge_latency_ms": bridge_latency,
                "ranked_object_ids": tool_ids,
                "rank_matches_bridge": tool_ids == bridge_ids,
                "rank_matches_local": tool_ids == local_ids,
                "tool_response": tool_response,
            }
        )

    counts_after = _counts(database_path)
    database_after = _snapshot(database_path)
    raw_unchanged = all(
        before == _snapshot(path) for _, path, before in raw_before
    )
    external_json = json.dumps(validations, ensure_ascii=False)
    swimwear = validations[2]["tool_response"].get("results", [])
    swimwear_honest = bool(swimwear) and all(
        any(
            evidence.get("value") == "unconfirmed: swimwear"
            and evidence.get("is_fact") is False
            and evidence.get("assessment") == "inference"
            for evidence in result.get("matched_evidence", [])
        )
        for result in swimwear
    )
    report = {
        "report_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queries": validations,
        "checks": {
            "all_queries_http_200": all(row["http_status"] == 200 for row in validations),
            "all_queries_have_request_id": all(row["request_id"] for row in validations),
            "all_ranks_match_bridge": all(row["rank_matches_bridge"] for row in validations),
            "all_ranks_match_local": all(row["rank_matches_local"] for row in validations),
            "swimwear_is_inference_not_fact": swimwear_honest,
            "no_raw_path_leakage": not any(
                marker in external_json for marker in ("raw_uri", "file:///", "G:\\\\", "G:/")
            ),
            "no_filesystem_details": not any(
                marker in external_json
                for marker in ("relative_path", "absolute_path", "database_path")
            ),
            "token_not_in_report": (
                tool_token not in external_json and bridge_token not in external_json
            ),
            "database_bytes_size_mtime_unchanged": database_before == database_after,
            "database_counts_unchanged": counts_before == counts_after,
            "semantic_results_unchanged": counts_before["semantic_results"] == counts_after["semantic_results"],
            "semantic_relations_unchanged": counts_before["semantic_relations"] == counts_after["semantic_relations"],
            "raw_files_checked": len(raw_before),
            "raw_bytes_size_mtime_unchanged": raw_unchanged,
        },
        "database_snapshot": {"before": database_before, "after": database_after},
        "counts": {"before": counts_before, "after": counts_after},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failed_checks = [
        name for name, value in report["checks"].items()
        if isinstance(value, bool) and not value
    ]
    if failed_checks:
        raise RuntimeError(
            "tool validation checks failed: " + ", ".join(failed_checks)
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--tool-url", required=True)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8787")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-address")
    args = parser.parse_args()
    tool_token = os.environ.get("HUMAN_OS_TOOL_TOKEN", "")
    bridge_token = os.environ.get("HUMAN_OS_BRIDGE_TOKEN", "")
    if not tool_token or not bridge_token:
        raise SystemExit("HUMAN_OS_TOOL_TOKEN and HUMAN_OS_BRIDGE_TOKEN are required")
    report = validate_tool(
        args.db, args.tool_url, tool_token, args.bridge_url, bridge_token,
        args.output, args.source_address,
    )
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
