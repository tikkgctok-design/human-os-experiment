"""Minimal zero-dependency mobile HTTPS search test for Human OS."""

from __future__ import annotations

import getpass
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_URL = "https://memory.humonosmemory.com"
DEFAULT_QUERY = "найди фотографии со снегом"
TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64,}$")
WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:[\\/]")
BLOCKED_KEYS = {"raw_uri", "raw_path", "database_path", "sqlite_path", "token", "secret"}


def _is_safe(value: Any) -> bool:
    if isinstance(value, dict):
        return all(
            key.casefold() not in BLOCKED_KEYS
            and "token" not in key.casefold()
            and "secret" not in key.casefold()
            and _is_safe(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_is_safe(item) for item in value)
    if isinstance(value, str):
        lowered = value.casefold()
        return "file:///" not in lowered and "sqlite" not in lowered and not WINDOWS_PATH.search(value)
    return True


def search(base_url: str, token: str, query: str = DEFAULT_QUERY, limit: int = 5) -> dict[str, Any]:
    if not base_url.startswith("https://"):
        raise ValueError("Human OS mobile test requires HTTPS")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Bearer token must be at least 256 bits encoded as hex")
    body = json.dumps({"query": query, "limit": limit}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/search",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HumanOS-MobileTest/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Human OS returned HTTP {exc.code}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("Human OS returned an invalid search response")
    if not _is_safe(payload) or token in json.dumps(payload, ensure_ascii=False):
        raise RuntimeError("Unsafe private data was blocked by the mobile client")
    return payload


def main() -> None:
    token = getpass.getpass("Human OS Bearer token (hidden): ").strip()
    try:
        payload = search(DEFAULT_URL, token)
    except (ValueError, RuntimeError, urllib.error.URLError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
