import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from human_os.bridge import BridgeConfig, load_private_environment, make_handler
from human_os.search_api import make_handler as make_local_handler


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"
TOKEN = "ab" * 32
OBJECT_ID = "hos_obj_44444444444444444444444444444444"


def _database(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "index.sqlite"
    raw = tmp_path / "private raw snow.jpg"
    raw.write_bytes(b"immutable real-like raw")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    with sqlite3.connect(database) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO objects (
                object_id, object_type, source, source_id, occurred_at, captured_at,
                raw_uri, content_hash, mime_type, metadata_json, created_at
            ) VALUES (?, 'photo', 'fixture', 'snow', ?, ?, ?, ?, 'image/jpeg', '{}', ?)
            """,
            (
                OBJECT_ID, "2024-03-23T13:08:58+11:00",
                "2024-03-23T13:08:58+11:00", raw.as_uri(), digest,
                "2026-08-12T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO blobs VALUES ('blob-snow', ?, ?, 'image/jpeg', ?)",
            (digest, raw.stat().st_size, "2026-08-12T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO attachments VALUES (?, 'blob-snow', ?)",
            (OBJECT_ID, "2026-08-12T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO semantic_results (
                semantic_result_id, object_id, source_blob_id, source_content_hash,
                extractor_name, extractor_version, semantic_type, status, confidence,
                result_json, result_text, diagnostics_json, provenance_json, is_current,
                created_at
            ) VALUES (
                'semantic-snow', ?, 'blob-snow', ?, 'fixture.caption', '1.0.0',
                'image_caption', 'complete', 0.9, ?, ?, '[]', ?, 1, ?
            )
            """,
            (
                OBJECT_ID, digest,
                json.dumps({"debug_path": r"G:\RAW\private.jpg"}),
                "A person standing on snow.",
                json.dumps({"raw_uri": raw.as_uri(), "model": "fixture"}),
                "2026-08-12T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO semantic_relations VALUES (
                'relation-snow', ?, 'semantic-snow',
                'object_has_semantic_result', 'semantic-snow', 0.9, ?
            )
            """,
            (OBJECT_ID, "2026-08-12T00:00:00+00:00"),
        )
    return database, raw


@contextmanager
def _server(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextmanager
def _local_api(database: Path):
    with _server(make_local_handler(database)) as url:
        yield url


@contextmanager
def _bridge(upstream: str, tmp_path: Path, **overrides):
    values = {
        "bearer_token": TOKEN,
        "upstream_base_url": upstream,
        "audit_log_path": tmp_path / "audit.jsonl",
        "rate_limit_requests": 100,
    }
    values.update(overrides)
    config = BridgeConfig(**values)
    with _server(make_handler(config)) as url:
        yield url, config


def _call(
    url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = TOKEN,
    body: bytes | None = None,
) -> tuple[int, dict, dict]:
    headers = {"X-Forwarded-Proto": "https"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)


def _search(url: str, query: str) -> tuple[int, dict, dict]:
    return _call(
        url, "/v1/search", method="POST",
        body=json.dumps({"query": query}).encode("utf-8"),
    )


def test_valid_bearer_health_search_zero_results_and_local_ranking(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(local_url, tmp_path) as (bridge_url, _):
        health_status, health, _ = _call(bridge_url, "/bridge/health")
        bridge_status, bridged, _ = _search(bridge_url, "человек на снегу")
        zero_status, zero, _ = _search(bridge_url, "uniquevolcanoqxyz")
        local_request = urllib.request.Request(
            local_url + "/v1/search",
            data=json.dumps({"query": "человек на снегу"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(local_request, timeout=3) as response:
            local = json.load(response)

    assert health_status == 200
    assert health["bridge_alive"] is True
    assert health["local_api_reachable"] is True
    assert health["bridge_opens_database"] is False
    assert bridge_status == zero_status == 200
    assert [item["object_id"] for item in bridged["results"]] == [
        item["object_id"] for item in local["results"]
    ]
    assert zero["results"] == []
    assert bridged["request_id"]


def test_missing_and_invalid_token_are_401(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(local_url, tmp_path) as (url, _):
        missing, _, _ = _call(url, "/bridge/health", token=None)
        invalid, _, _ = _call(url, "/bridge/health", token="cd" * 32)
    assert missing == invalid == 401


def test_rate_limit_returns_429(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(
        local_url, tmp_path, rate_limit_requests=1, rate_limit_window_seconds=60
    ) as (url, _):
        first, _, _ = _call(url, "/bridge/health")
        second, payload, _ = _call(url, "/bridge/health")
    assert first == 200
    assert second == 429
    assert payload["error"]["code"] == "rate_limited"


def test_malformed_json_and_oversized_body(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(
        local_url, tmp_path, request_body_limit=32
    ) as (url, _):
        malformed, payload, _ = _call(
            url, "/v1/search", method="POST", body=b"{broken"
        )
        oversized, too_large, _ = _call(
            url, "/v1/search", method="POST", body=b"{" + b"x" * 100 + b"}"
        )
    assert malformed == 400 and payload["error"]["code"] == "malformed_json"
    assert oversized == 413 and too_large["error"]["code"] == "invalid_request_size"


def test_disallowed_endpoint_path_traversal_and_invalid_object_id(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(local_url, tmp_path) as (url, _):
        disallowed, _, _ = _call(url, "/v1/health")
        traversal, traversal_body, _ = _call(url, "/v1/object/%2e%2e%2fsecret")
        invalid, invalid_body, _ = _call(url, "/v1/object/not-an-object")
    assert disallowed == 404
    assert traversal == 400 and traversal_body["error"]["code"] == "invalid_path"
    assert invalid == 400 and invalid_body["error"]["code"] == "invalid_object_id"


class _SlowHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        time.sleep(0.2)
        body = b'{"results":[]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, format: str, *args) -> None:
        return


def test_upstream_timeout_and_unavailable(tmp_path: Path) -> None:
    with _server(_SlowHandler) as slow_url, _bridge(
        slow_url, tmp_path, upstream_timeout=0.03
    ) as (bridge_url, _):
        timeout_status, timeout_body, _ = _search(bridge_url, "snow")
    closed = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    unavailable_url = f"http://127.0.0.1:{closed.server_address[1]}"
    closed.server_close()
    with _bridge(unavailable_url, tmp_path) as (bridge_url, _):
        unavailable_status, unavailable_body, _ = _search(bridge_url, "snow")
    assert timeout_status == 504
    assert timeout_body["error"]["code"] == "upstream_timeout"
    assert unavailable_status == 503
    assert unavailable_body["error"]["code"] == "upstream_unavailable"


def test_no_path_or_token_leak_and_database_raw_semantics_unchanged(tmp_path: Path) -> None:
    database, raw = _database(tmp_path)
    db_before = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)
    raw_before = (raw.read_bytes(), raw.stat().st_size, raw.stat().st_mtime_ns)
    with sqlite3.connect(database) as conn:
        semantic_before = (
            conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0],
        )
    with _local_api(database) as local_url, _bridge(local_url, tmp_path) as (url, config):
        first_status, first, _ = _search(url, "снег")
        second_status, second, _ = _search(url, "снег")
        object_status, object_payload, _ = _call(url, f"/v1/object/{OBJECT_ID}")
    with sqlite3.connect(database) as conn:
        semantic_after = (
            conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0],
        )
    external = json.dumps([first, second, object_payload], ensure_ascii=False)
    audit = config.audit_log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines()]

    assert first_status == second_status == object_status == 200
    assert {key for record in records for key in record} == {
        "timestamp", "request_id", "endpoint", "http_status", "latency_ms",
        "result_count", "principal_hash",
    }
    assert first["results"] == second["results"]
    assert "raw_uri" not in external and "file:///" not in external
    assert "G:\\RAW" not in external and str(raw) not in external
    assert TOKEN not in audit and "снег" not in audit and "person standing" not in audit
    assert semantic_after == semantic_before
    assert (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns) == db_before
    assert (raw.read_bytes(), raw.stat().st_size, raw.stat().st_mtime_ns) == raw_before


def test_https_loopback_token_and_git_privacy_guards(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        BridgeConfig(bearer_token=TOKEN, host="0.0.0.0").validate()
    with pytest.raises(ValueError, match="256"):
        BridgeConfig(bearer_token="weak").validate()
    with pytest.raises(ValueError, match="upstream"):
        BridgeConfig(bearer_token=TOKEN, upstream_base_url="https://example.com").validate()

    env_file = tmp_path / "bridge.env"
    env_file.write_text(f"HUMAN_OS_BRIDGE_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("HUMAN_OS_BRIDGE_TOKEN", raising=False)
    load_private_environment(env_file)
    assert os.environ["HUMAN_OS_BRIDGE_TOKEN"] == TOKEN

    database, _ = _database(tmp_path)
    with _local_api(database) as local_url, _bridge(local_url, tmp_path) as (url, _):
        request = urllib.request.Request(
            url + "/bridge/health",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with pytest.raises(urllib.error.HTTPError) as insecure:
            urllib.request.urlopen(request, timeout=3)
    assert insecure.value.code == 426

    assert "private/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.env" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "private/bridge.env"], cwd=ROOT
    )
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert ignored.returncode == 0
    assert not any(path.endswith(".env") or path.startswith("private/") for path in tracked)
