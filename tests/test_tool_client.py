import json
import urllib.error
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from human_os.tool_client import (
    TOOL_DESCRIPTION,
    HumanOSToolClient,
    ToolConfig,
    _load_private_environment,
)
from human_os.openapi_contract import render_schema, validate_schema


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ab" * 32
OBJECT_A = "hos_obj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OBJECT_B = "hos_obj_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class _FixtureBridge(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        assert self.headers.get("User-Agent") == "HumanOS-ToolClient/1.0"
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._json(401, {"error": {"code": "unauthorized"}})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == "/bridge/health":
            self._json(200, {
                "bridge_alive": True, "local_api_reachable": True,
                "bridge_opens_database": False, "request_id": "health-request",
            })
            return
        if self.path == f"/v1/object/{OBJECT_A}":
            self._json(200, {
                "object_id": OBJECT_A, "object_type": "photo",
                "safe_ref": f"human-os://object/{OBJECT_A}",
                "request_id": "object-request",
            })
            return
        self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        query = payload["query"]
        if query == "rate":
            self._json(429, {"error": {"code": "rate_limited"}})
            return
        if query == "server":
            self._json(503, {"error": {"code": "upstream_unavailable"}})
            return
        if query == "malformed":
            body = b"not-json"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if query == "unsafe":
            self._json(200, {"request_id": "unsafe", "results": [], "raw_uri": "G:\\RAW\\x.jpg"})
            return
        if query == "token-leak":
            self._json(200, {"request_id": "unsafe", "results": [], "debug": TOKEN})
            return
        if query == "slow":
            time.sleep(0.2)
        results = [] if query == "zero" else [
            {
                "object_id": OBJECT_B,
                "score": 0.9,
                "occurred_at": "2024-01-02T03:04:05+00:00",
                "safe_ref": f"human-os://object/{OBJECT_B}",
                "reason": "matched snow; missing derived evidence for: swimwear",
                "assessment": "candidate",
                "missing_evidence": ["swimwear"],
                "matched_evidence": [{
                    "semantic_type": "image_caption",
                    "snippet": "A person on snow.",
                    "confidence": 0.91,
                    "assertion": "derived_evidence",
                }, {
                    "semantic_type": "canonical_metadata",
                    "snippet": '{"relative_path":"private/family.jpg","byte_size":123}',
                    "confidence": 1.0,
                    "assertion": "derived_evidence",
                }],
            },
            {
                "object_id": OBJECT_A,
                "score": 0.7,
                "occurred_at": None,
                "safe_ref": f"human-os://object/{OBJECT_A}",
                "reason": "matched snow",
                "assessment": "derived_evidence_match",
                "missing_evidence": [],
                "matched_evidence": [{
                    "semantic_type": "detected_objects",
                    "snippet": "detected objects: snow",
                    "confidence": 0.8,
                    "assertion": "derived_evidence",
                }],
            },
        ]
        try:
            self._json(200, {
                "query": query,
                "results": results,
                "unsupported_concepts": ["swimwear"] if results else [],
                "explanation": "Swimwear is not verified." if results else None,
                "request_id": "search-request",
            })
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _bridge():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _client(url: str, token: str = TOKEN, timeout: float = 1) -> HumanOSToolClient:
    return HumanOSToolClient(ToolConfig(
        bridge_url=url, bearer_token=token, timeout=timeout,
        allow_http_loopback_internal=True,
    ))


def test_search_compacts_evidence_preserves_rank_and_marks_inference() -> None:
    with _bridge() as url:
        result = _client(url).human_os_search("найди меня в плавках на снегу")
    assert [item["object_id"] for item in result["results"]] == [OBJECT_B, OBJECT_A]
    assert result["request_id"] == "search-request"
    assert result["results"][0]["media_type"] == "photo"
    evidence = result["results"][0]["matched_evidence"]
    assert evidence[0] == {
        "type": "caption", "value": "A person on snow.", "confidence": 0.91,
        "is_fact": False, "assessment": "candidate",
    }
    assert evidence[1]["value"] == "canonical metadata matched (private details redacted)"
    assert "relative_path" not in json.dumps(result)
    assert evidence[2]["value"] == "unconfirmed: swimwear"
    assert evidence[2]["assessment"] == "inference"
    assert evidence[2]["is_fact"] is False
    assert "not verified" in result["warnings"][0]


def test_health_get_object_and_zero_results() -> None:
    with _bridge() as url:
        client = _client(url)
        health = client.human_os_health()
        item = client.human_os_get_object(OBJECT_A)
        zero = client.human_os_search("zero")
    assert health["ok"] is True and health["bridge_opens_database"] is False
    assert item["object"]["object_id"] == OBJECT_A
    assert item["request_id"] == "object-request"
    assert zero["results"] == []


@pytest.mark.parametrize(
    ("query", "error_type"),
    [("rate", "rate_limited"), ("server", "bridge_error"),
     ("malformed", "malformed_response"), ("unsafe", "unsafe_response"),
     ("token-leak", "unsafe_response")],
)
def test_normalized_bridge_errors(query: str, error_type: str) -> None:
    with _bridge() as url:
        result = _client(url).human_os_search(query)
    assert result["ok"] is False
    assert result["error"]["type"] == error_type
    assert TOKEN not in json.dumps(result)


def test_invalid_token_timeout_and_unreachable_are_normalized(monkeypatch) -> None:
    with _bridge() as url:
        unauthorized = _client(url, token="cd" * 32).human_os_health()
        timeout = _client(url, timeout=0.02).human_os_search("slow")
    def refused(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    unreachable_client = _client("http://127.0.0.1:1")
    monkeypatch.setattr(unreachable_client, "_urlopen", refused)
    unreachable = unreachable_client.human_os_health()
    assert unauthorized["error"]["type"] == "unauthorized"
    assert timeout["error"]["type"] == "timeout"
    assert unreachable["error"]["type"] == "bridge_unreachable"


def test_input_config_private_env_and_token_leak_guards(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ToolConfig("http://example.com", TOKEN).validate()
    with pytest.raises(ValueError, match="256"):
        ToolConfig("https://example.com", "weak").validate()
    env_file = tmp_path / "tool.env"
    env_file.write_text(
        f"HUMAN_OS_BRIDGE_TOKEN={TOKEN}\n"
        "HUMAN_OS_TOOL_BRIDGE_URL=https://bridge.example\n"
        "HUMAN_OS_TOOL_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HUMAN_OS_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("HUMAN_OS_TOOL_TOKEN", raising=False)
    _load_private_environment(env_file)
    assert ToolConfig.from_environment().bearer_token == TOKEN
    with _bridge() as url:
        client = _client(url)
        assert client.human_os_search("")["error"]["type"] == "invalid_request"
        assert client.human_os_search("x", 0)["error"]["type"] == "invalid_request"
        assert client.human_os_get_object("../secret")["error"]["type"] == "invalid_request"


def test_openapi_tool_schema_is_machine_readable_and_complete() -> None:
    schema = json.loads((ROOT / "tool-schema" / "human_os.openapi.json").read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["description"] == TOOL_DESCRIPTION
    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert operations == {"human_os_search", "human_os_get_object", "human_os_health"}
    assert all(
        operation.get("x-openai-isConsequential") is False
        for path in schema["paths"].values()
        for operation in path.values()
    )
    examples = schema["paths"]["/v1/search"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["query"]["examples"]
    assert examples == [
        "найди где я на снегу", "найди фотографии со снегом",
        "найди фото с вывеской", "найди фотографии из одного места",
    ]
    assert schema["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    validate_schema(schema, require_production_server=False)


def test_openapi_render_requires_real_https_origin(tmp_path: Path) -> None:
    template = ROOT / "tool-schema" / "human_os.openapi.json"
    with pytest.raises(ValueError, match="stable hostname"):
        render_schema(template, "https://human-os.example.com", tmp_path / "bad.json")
    output = tmp_path / "human-os.openapi.json"
    schema = render_schema(template, "https://memory.example.net", output)
    assert schema["servers"][0]["url"] == "https://memory.example.net"
    assert output.is_file()
