from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from human_os.mobile_web import MobileConfig, MobileSessionStore, make_handler


MOBILE_TOKEN = "ab" * 32
TOOL_TOKEN = "cd" * 32
PHOTO_ID = "hos_obj_" + "a" * 32


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        assert self.path == "/v1/search"
        assert self.headers.get("Authorization") == "Bearer " + TOOL_TOKEN
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = {
            "query": body["query"],
            "results": [{
                "object_id": PHOTO_ID,
                "score": 0.9,
                "occurred_at": None,
                "media_type": "photo",
                "safe_ref": "human-os://object/" + PHOTO_ID,
                "reason": "matched snow",
                "matched_evidence": [{
                    "type": "caption", "value": "snow", "confidence": 0.8,
                    "is_fact": False, "assessment": "candidate",
                }],
            }],
            "warnings": [],
            "request_id": "upstream-request",
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


def call(base, path, *, method="GET", payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_mobile_page_contains_no_server_secret_and_has_security_headers():
    config = MobileConfig(MOBILE_TOKEN, TOOL_TOKEN, "http://127.0.0.1:1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, MobileSessionStore()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = call(f"http://127.0.0.1:{server.server_port}", "/mobile")
        text = body.decode()
        assert status == 200
        assert MOBILE_TOKEN not in text and TOOL_TOKEN not in text
        assert "Спросить Human OS" in text
        assert headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_mobile_session_and_search_are_separate_from_tool_token():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    config = MobileConfig(MOBILE_TOKEN, TOOL_TOKEN, f"http://127.0.0.1:{upstream.server_port}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, MobileSessionStore()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    forwarded = {"X-Forwarded-Proto": "https", "Origin": "https://memory.humonosmemory.com"}
    try:
        denied, _, _ = call(base, "/mobile/search", method="POST", payload={"query": "snow"}, headers=forwarded)
        assert denied == 401
        login, headers, raw = call(base, "/mobile/session", method="POST", payload={"token": MOBILE_TOKEN}, headers=forwarded)
        assert login == 200
        session = json.loads(raw)
        set_cookies = headers.get_all("Set-Cookie")
        cookie = next(value for value in set_cookies if value.startswith("human_os_mobile_session=")).split(";", 1)[0]
        assert all("HttpOnly" in value and "Secure" in value for value in set_cookies)
        assert MOBILE_TOKEN not in raw.decode() and TOOL_TOKEN not in raw.decode()
        auth = {**forwarded, "Cookie": cookie, "X-CSRF-Token": session["csrf_token"]}
        status, _, result_raw = call(base, "/mobile/search", method="POST", payload={"query": "snow", "limit": 5}, headers=auth)
        result = json.loads(result_raw)
        assert status == 200
        assert result["results"][0]["object_id"].startswith("hos_obj_")
        assert MOBILE_TOKEN not in result_raw.decode() and TOOL_TOKEN not in result_raw.decode()
        assert "raw_uri" not in result_raw.decode() and "sqlite" not in result_raw.decode().lower()
    finally:
        server.shutdown(); server.server_close(); thread.join()
        upstream.shutdown(); upstream.server_close(); upstream_thread.join()


def test_mobile_rejects_wrong_token_csrf_and_non_https_forwarding():
    config = MobileConfig(MOBILE_TOKEN, TOOL_TOKEN, "http://127.0.0.1:1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, MobileSessionStore()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        insecure, _, _ = call(base, "/mobile/session", method="POST", payload={"token": MOBILE_TOKEN})
        wrong, _, _ = call(base, "/mobile/session", method="POST", payload={"token": "ef" * 32}, headers={"X-Forwarded-Proto": "https", "Origin": "https://memory.humonosmemory.com"})
        assert insecure == 426
        assert wrong == 401
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_mobile_config_rejects_non_loopback_and_equal_tokens():
    import pytest
    with pytest.raises(ValueError):
        MobileConfig(MOBILE_TOKEN, MOBILE_TOKEN).validate()
    with pytest.raises(ValueError):
        MobileConfig(MOBILE_TOKEN, TOOL_TOKEN, host="0.0.0.0").validate()


def _photo_database(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (20, 40), "navy")
    exif = Image.Exif(); exif[274] = 6
    image.save(raw, exif=exif)
    database = tmp_path / "index.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE objects (
                object_id TEXT PRIMARY KEY, object_type TEXT, raw_uri TEXT,
                content_hash TEXT, occurred_at TEXT, captured_at TEXT, mime_type TEXT
            );
            CREATE TABLE metadata_extractions (
                extraction_id TEXT PRIMARY KEY, object_id TEXT, metadata_json TEXT,
                extracted_at TEXT
            );
            CREATE TABLE semantic_results (
                semantic_result_id TEXT PRIMARY KEY, object_id TEXT, semantic_type TEXT,
                result_text TEXT, result_json TEXT, is_current INTEGER, status TEXT
            );
            """
        )
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        conn.execute(
            "INSERT INTO objects VALUES (?, 'photo', ?, ?, ?, ?, 'image/jpeg')",
            (PHOTO_ID, raw.resolve().as_uri(), digest, "2024-01-02T03:04:05Z", "2024-01-02T03:04:05Z"),
        )
        conn.execute(
            "INSERT INTO metadata_extractions VALUES ('m1', ?, ?, '2024-01-03T00:00:00Z')",
            (PHOTO_ID, json.dumps({"gps": {"latitude": 10.5, "longitude": 20.25}})),
        )
        conn.execute(
            "INSERT INTO semantic_results VALUES ('s1', ?, 'image_caption', 'Blue portrait image', '{}', 1, 'complete')",
            (PHOTO_ID,),
        )
        conn.execute(
            "INSERT INTO semantic_results VALUES ('s2', ?, 'detected_objects', NULL, ?, 1, 'complete')",
            (PHOTO_ID, json.dumps({"detections": [{"label": "person"}, {"label": "car"}]})),
        )
    return database, raw


def test_mobile_photo_preview_is_session_only_safe_cached_and_oriented(tmp_path: Path):
    database, raw = _photo_database(tmp_path)
    raw_before = (hashlib.sha256(raw.read_bytes()).hexdigest(), raw.stat().st_size, raw.stat().st_mtime_ns)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    config = MobileConfig(
        MOBILE_TOKEN, TOOL_TOKEN, f"http://127.0.0.1:{upstream.server_port}",
        database_path=database, preview_cache_dir=tmp_path / "cache",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, MobileSessionStore()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    forwarded = {"X-Forwarded-Proto": "https", "Origin": "https://memory.humonosmemory.com"}
    image_path = f"/mobile/image/{PHOTO_ID}?variant=thumbnail"
    try:
        denied, _, denied_raw = call(base, image_path)
        assert denied == 401
        assert b"raw_uri" not in denied_raw and str(raw).encode() not in denied_raw
        login, headers, raw_login = call(
            base, "/mobile/session", method="POST", payload={"token": MOBILE_TOKEN}, headers=forwarded
        )
        session = json.loads(raw_login)
        cookie = next(
            value for value in headers.get_all("Set-Cookie")
            if value.startswith("human_os_mobile_session=")
        ).split(";", 1)[0]
        status, _, search_raw = call(
            base, "/mobile/search", method="POST", payload={"query": "snow"},
            headers={**forwarded, "Cookie": cookie, "X-CSRF-Token": session["csrf_token"]},
        )
        result = json.loads(search_raw)["results"][0]
        assert status == 200
        assert result["thumbnail_url"] == image_path
        assert result["caption"] == "Blue portrait image"
        assert result["concepts"] == ["person", "car"]
        assert result["gps"] == {"latitude": 10.5, "longitude": 20.25}
        assert "file:///" not in search_raw.decode() and str(raw) not in search_raw.decode()
        image_status, image_headers, image_raw = call(base, image_path, headers={"Cookie": cookie})
        assert image_status == 200 and image_headers["Content-Type"] == "image/jpeg"
        rendered = tmp_path / "rendered.jpg"; rendered.write_bytes(image_raw)
        with Image.open(rendered) as preview:
            assert preview.size == (40, 20)
        cached = list((tmp_path / "cache" / "thumbnail").glob("*.jpg"))
        assert len(cached) == 1
        cache_mtime = cached[0].stat().st_mtime_ns
        repeated_status, _, repeated_raw = call(base, image_path, headers={"Cookie": cookie})
        assert repeated_status == 200 and repeated_raw == image_raw
        assert cached[0].stat().st_mtime_ns == cache_mtime
        assert raw_before == (hashlib.sha256(raw.read_bytes()).hexdigest(), raw.stat().st_size, raw.stat().st_mtime_ns)
    finally:
        server.shutdown(); server.server_close(); thread.join()
        upstream.shutdown(); upstream.server_close(); upstream_thread.join()


def test_trusted_device_refreshes_expired_session_without_mobile_token():
    now = [1_800_000_000.0]
    clock = lambda: now[0]
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    config = MobileConfig(
        MOBILE_TOKEN, TOOL_TOKEN, f"http://127.0.0.1:{upstream.server_port}",
        session_ttl=60, trusted_device_ttl=30 * 24 * 3600,
    )
    sessions = MobileSessionStore(clock)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, sessions))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    forwarded = {"X-Forwarded-Proto": "https", "Origin": "https://memory.humonosmemory.com"}
    try:
        login, headers, raw = call(
            base, "/mobile/session", method="POST", payload={"token": MOBILE_TOKEN}, headers=forwarded
        )
        assert login == 200
        initial = json.loads(raw)
        cookies = [value.split(";", 1)[0] for value in headers.get_all("Set-Cookie")]
        session_cookie = next(value for value in cookies if value.startswith("human_os_mobile_session="))
        device_cookie = next(value for value in cookies if value.startswith("human_os_mobile_device="))
        auth = {**forwarded, "Cookie": session_cookie + "; " + device_cookie,
                "X-CSRF-Token": initial["csrf_token"]}
        first, _, _ = call(base, "/mobile/search", method="POST", payload={"query": "snow"}, headers=auth)
        assert first == 200

        now[0] += 61
        expired, _, expired_raw = call(
            base, "/mobile/search", method="POST", payload={"query": "snow"}, headers=auth
        )
        assert expired == 401
        assert "expired" in json.loads(expired_raw)["error"]["message"]

        refreshed, refresh_headers, refresh_raw = call(
            base, "/mobile/session/refresh", method="POST", payload={},
            headers={**forwarded, "Cookie": device_cookie, "X-Mobile-Refresh": "1"},
        )
        assert refreshed == 200
        assert MOBILE_TOKEN not in refresh_raw.decode() and TOOL_TOKEN not in refresh_raw.decode()
        renewed = json.loads(refresh_raw)
        renewed_cookies = [value.split(";", 1)[0] for value in refresh_headers.get_all("Set-Cookie")]
        renewed_session = next(value for value in renewed_cookies if value.startswith("human_os_mobile_session="))
        renewed_device = next(value for value in renewed_cookies if value.startswith("human_os_mobile_device="))
        second, _, second_raw = call(
            base, "/mobile/search", method="POST", payload={"query": "snow"},
            headers={**forwarded, "Cookie": renewed_session + "; " + renewed_device,
                     "X-CSRF-Token": renewed["csrf_token"]},
        )
        assert second == 200
        assert json.loads(second_raw)["results"][0]["object_id"].startswith("hos_obj_")
    finally:
        server.shutdown(); server.server_close(); thread.join()
        upstream.shutdown(); upstream.server_close(); upstream_thread.join()
