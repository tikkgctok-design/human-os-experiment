"""Localhost-only HTTP v1 transport for Human OS read-only retrieval."""

from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .search import (
    SearchFilters,
    SearchResponse,
    get_object_record,
    retrieval_health,
    safe_object_ref,
    search_index,
)


def _redact_local_paths(value: Any) -> Any:
    """Defense in depth for future evidence payloads that may contain local paths."""
    if isinstance(value, dict):
        return {key: _redact_local_paths(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"file:///[^\s\"']+", "[local-path-redacted]", value)
        return re.sub(r"(?i)\b[a-z]:[\\/][^\r\n]*", "[local-path-redacted]", value)
    return value


def _api_evidence(evidence: Any) -> dict[str, Any]:
    """Expose exact evidence references without stored RAW paths in provenance."""
    return {
        "evidence_id": evidence.evidence_id,
        "evidence_kind": evidence.evidence_kind,
        "semantic_result_id": evidence.semantic_result_id,
        "semantic_type": evidence.semantic_type,
        "extractor_name": evidence.extractor_name,
        "extractor_version": evidence.extractor_version,
        "confidence": evidence.confidence,
        "matched_concepts": list(evidence.matched_concepts),
        "snippet": evidence.snippet,
        "provenance": {
            "source_blob_id": evidence.source_blob_id,
            "source_content_hash": evidence.source_content_hash,
        },
        "assertion": "derived_evidence",
    }


def api_search_response(response: SearchResponse) -> dict[str, Any]:
    """Map the shared ranked result to the path-safe public localhost contract."""
    results = []
    for hit in response.results:
        candidate = bool(hit.missing_concepts)
        results.append(
            {
                "object_id": hit.object_id,
                "score": hit.score,
                "occurred_at": hit.occurred_at,
                "matched_evidence": [_api_evidence(item) for item in hit.evidence],
                "reason": hit.reason,
                "provenance": {
                    "semantic_result_ids": [
                        item.semantic_result_id
                        for item in hit.evidence
                        if item.semantic_result_id is not None
                    ],
                    "source_content_hashes": sorted(
                        {item.source_content_hash for item in hit.evidence}
                    ),
                },
                "safe_ref": safe_object_ref(hit.object_id),
                "assessment": "candidate" if candidate else "derived_evidence_match",
                "missing_evidence": list(hit.missing_concepts),
                "is_fact": False,
            }
        )
    return {
        "query": response.query,
        "search_version": response.search_version,
        "requested_concepts": list(response.requested_concepts),
        "unsupported_concepts": list(response.unsupported_concepts),
        "best_effort": response.best_effort,
        "explanation": response.explanation,
        "results": results,
    }


def _search(payload: dict[str, Any], database_path: Path) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    raw_filters = payload.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raise ValueError("filters must be an object")
    limit = payload.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an integer")
    filters = SearchFilters(
        time_from=raw_filters.get("time_from"),
        time_to=raw_filters.get("time_to"),
        place=raw_filters.get("place"),
        media_type=raw_filters.get("media_type", "photo"),
    )
    return api_search_response(
        search_index(query, database_path, filters=filters, limit=limit)
    )


def make_handler(database_path: Path) -> type[BaseHTTPRequestHandler]:
    class RetrievalHandler(BaseHTTPRequestHandler):
        server_version = "HumanOSRetrieval/1.0"

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(_redact_local_paths(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, code: str, message: str) -> None:
            self._json(status, {"error": {"code": code, "message": message}})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/v1/health":
                    self._json(HTTPStatus.OK, retrieval_health(database_path))
                    return
                prefix = "/v1/object/"
                if path.startswith(prefix):
                    object_id = path[len(prefix):]
                    try:
                        record = get_object_record(object_id, database_path)
                    except ValueError as exc:
                        self._error(HTTPStatus.BAD_REQUEST, "invalid_object_id", str(exc))
                        return
                    if record is None:
                        self._error(HTTPStatus.NOT_FOUND, "object_not_found", "object does not exist")
                        return
                    self._json(HTTPStatus.OK, record)
                    return
                self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint does not exist")
            except (FileNotFoundError, OSError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "index_unavailable", "index unavailable")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            if urlparse(self.path).path != "/v1/search":
                self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint does not exist")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("request body must be between 1 byte and 1 MiB")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                result = _search(payload, database_path)
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
                return
            except (FileNotFoundError, OSError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "index_unavailable", "index unavailable")
                return
            self._json(HTTPStatus.OK, result)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return RetrievalHandler


def serve(database_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host != "127.0.0.1":
        raise ValueError("v1 retrieval API may bind only to 127.0.0.1")
    with ThreadingHTTPServer((host, port), make_handler(database_path)) as server:
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.db, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
