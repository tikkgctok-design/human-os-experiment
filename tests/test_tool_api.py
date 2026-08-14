import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from human_os.tool_api import ToolAPIConfig, load_private_environment, make_handler


BRIDGE_TOKEN = "ab" * 32
TOOL_TOKEN = "cd" * 32
OBJECT_ID = "hos_obj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _Bridge(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {BRIDGE_TOKEN}":
            return True
        self._json(401, {"error": {"code": "unauthorized"}})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/bridge/health":
            self._json(200, {
                "bridge_alive": True, "local_api_reachable": True,
                "bridge_opens_database": False, "request_id": "bridge-health",
            })
        elif self.path == f"/v1/object/{OBJECT_ID}":
            self._json(200, {
                "object_id": OBJECT_ID, "object_type": "photo",
                "occurred_at": None, "captured_at": None, "mime_type": "image/jpeg",
                "safe_ref": f"human-os://object/{OBJECT_ID}",
                "provenance": {"blob_id": "blob-a", "content_hash": "a" * 64},
                "evidence": [], "request_id": "bridge-object",
            })
        else:
            self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "0"))
        query = json.loads(self.rfile.read(length))["query"]
        self._json(200, {
            "query": query,
            "results": [{
                "object_id": OBJECT_ID, "score": 0.9, "occurred_at": None,
                "safe_ref": f"human-os://object/{OBJECT_ID}",
                "reason": "matched snow", "assessment": "derived_evidence_match",
                "missing_evidence": [],
                "matched_evidence": [{
                    "semantic_type": "image_caption", "snippet": "snow",
                    "confidence": 0.8, "assertion": "derived_evidence",
                }],
            }],
            "unsupported_concepts": [], "explanation": None,
            "request_id": "bridge-search",
        })

    def log_message(self, format: str, *args: object) -> None:
        return


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
def _tool_api(bridge_url: str, tmp_path: Path, **overrides):
    values = {
        "external_token": TOOL_TOKEN,
        "bridge_token": BRIDGE_TOKEN,
        "bridge_url": bridge_url,
        "audit_log_path": tmp_path / "tool-audit.jsonl",
        "rate_limit_requests": 100,
    }
    values.update(overrides)
    config = ToolAPIConfig(**values)
    with _server(make_handler(config)) as url:
        yield url, config


def _call(
    url: str, path: str, *, method: str = "GET", token: str | None = TOOL_TOKEN,
    body: bytes | None = None,
) -> tuple[int, dict, dict]:
    headers = {"X-Forwarded-Proto": "https"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)


def test_end_to_end_contract_auth_and_rank_preservation(tmp_path: Path) -> None:
    with _server(_Bridge) as bridge_url, _tool_api(bridge_url, tmp_path) as (url, _):
        status, search, headers = _call(
            url, "/v1/search", method="POST",
            body=json.dumps({"query": "snow", "limit": 10}).encode(),
        )
        health_status, health, _ = _call(url, "/bridge/health")
        object_status, item, _ = _call(url, f"/v1/object/{OBJECT_ID}")
    assert status == health_status == object_status == 200
    assert [row["object_id"] for row in search["results"]] == [OBJECT_ID]
    assert search["results"][0]["matched_evidence"][0]["assessment"] == "candidate"
    assert search["request_id"] != "bridge-search"
    assert headers["X-Request-ID"] == search["request_id"]
    assert health["bridge_alive"] is True
    assert item["object"]["object_id"] == OBJECT_ID


def test_external_auth_https_allowlist_body_limit_and_rate_limit(tmp_path: Path) -> None:
    with _server(_Bridge) as bridge_url, _tool_api(
        bridge_url, tmp_path, request_body_limit=32,
        rate_limit_requests=1, rate_limit_window_seconds=60,
    ) as (url, _):
        missing, _, _ = _call(url, "/bridge/health", token=None)
        invalid, _, _ = _call(url, "/bridge/health", token="ef" * 32)
        first, _, _ = _call(url, "/bridge/health")
        limited, limited_body, _ = _call(url, "/bridge/health")
        disallowed, _, _ = _call(url, "/v1/health")
        traversal, _, _ = _call(url, "/v1/object/%2e%2e%2fsecret")
        oversized, _, _ = _call(url, "/v1/search", method="POST", body=b"x" * 100)
        insecure_request = urllib.request.Request(
            url + "/bridge/health",
            headers={"Authorization": f"Bearer {TOOL_TOKEN}"},
        )
        with pytest.raises(urllib.error.HTTPError) as insecure:
            urllib.request.urlopen(insecure_request, timeout=3)
    assert missing == invalid == 401
    assert first == 200 and limited == 429
    assert limited_body["error"]["type"] == "rate_limited"
    # Auth/rate limiting happens before route disclosure for an exhausted principal.
    assert disallowed == oversized == 429
    assert traversal == 400
    assert insecure.value.code == 426


def test_malformed_unknown_fields_invalid_id_and_no_secret_logging(tmp_path: Path) -> None:
    with _server(_Bridge) as bridge_url, _tool_api(bridge_url, tmp_path) as (url, config):
        malformed, _, _ = _call(url, "/v1/search", method="POST", body=b"{bad")
        extra, _, _ = _call(
            url, "/v1/search", method="POST",
            body=json.dumps({"query": "snow", "filters": {}}).encode(),
        )
        invalid_id, _, _ = _call(url, "/v1/object/not-valid")
        _, response, _ = _call(
            url, "/v1/search", method="POST",
            body=json.dumps({"query": "snow"}).encode(),
        )
    audit = config.audit_log_path.read_text(encoding="utf-8")
    external = json.dumps(response)
    assert malformed == extra == invalid_id == 400
    assert TOOL_TOKEN not in audit and BRIDGE_TOKEN not in audit
    assert "snow" not in audit
    assert TOOL_TOKEN not in external and BRIDGE_TOKEN not in external
    assert "raw_uri" not in external and "file:///" not in external


def test_config_and_private_environment_guards(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        ToolAPIConfig(TOOL_TOKEN, BRIDGE_TOKEN, host="0.0.0.0").validate()
    with pytest.raises(ValueError, match="bridge"):
        ToolAPIConfig(TOOL_TOKEN, BRIDGE_TOKEN, bridge_url="https://example.com").validate()
    with pytest.raises(ValueError, match="must differ"):
        ToolAPIConfig(TOOL_TOKEN, TOOL_TOKEN).validate()
    env = tmp_path / "tool.env"
    env.write_text(
        f"HUMAN_OS_TOOL_TOKEN={TOOL_TOKEN}\nHUMAN_OS_TOOL_PORT=8899\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HUMAN_OS_TOOL_TOKEN", raising=False)
    load_private_environment(env)
    assert "HUMAN_OS_TOOL_TOKEN" in __import__("os").environ
