from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mobile_test", ROOT / "scripts" / "mobile_test.py")
assert SPEC and SPEC.loader
MOBILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOBILE)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps({
            "query": MOBILE.DEFAULT_QUERY,
            "results": [{"object_id": "hos_obj_" + "a" * 32, "safe_ref": "human-os://object/test"}],
            "warnings": [],
            "request_id": "request-test",
        }).encode()


def test_mobile_search_uses_https_bearer_and_safe_json(monkeypatch) -> None:
    captured = {}

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(MOBILE.urllib.request, "urlopen", open_request)
    token = "ab" * 32
    result = MOBILE.search(MOBILE.DEFAULT_URL, token)
    request = captured["request"]
    assert request.full_url == "https://memory.humonosmemory.com/v1/search"
    assert request.get_header("Authorization") == "Bearer " + token
    assert result["request_id"] == "request-test"
    assert MOBILE._is_safe(result)


def test_mobile_privacy_guard_rejects_paths_and_secret_keys() -> None:
    assert not MOBILE._is_safe({"raw_uri": "file:///private/photo.jpg"})
    assert not MOBILE._is_safe({"value": r"G:\RAW\photo.jpg"})
    assert not MOBILE._is_safe({"token": "not-allowed"})
