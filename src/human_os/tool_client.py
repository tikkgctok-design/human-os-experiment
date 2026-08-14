"""ChatGPT-facing read-only tool client for the authenticated Human OS bridge."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


TOOL_DESCRIPTION = (
    "Search the user's private Human OS memory using natural-language queries. "
    "Read-only. Returns ranked canonical objects and evidence. Does not modify "
    "memory or expose RAW files."
)
OBJECT_ID_PATTERN = re.compile(r"^hos_obj_[0-9a-f]{32}$")
WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:[\\/]")
RELATIVE_MEDIA_PATH_PATTERN = re.compile(
    r"(?i)(?:[^\s\"']+[\\/])+[^\s\"']+\.(?:jpe?g|png|webp|heic|mp4|mov|m4a|mp3|wav)"
)
SEMANTIC_TYPES = {
    "image_caption": "caption",
    "ocr_text": "ocr",
    "detected_objects": "object",
    "detected_places": "place",
    "media_metadata": "metadata",
    "canonical_metadata": "metadata",
    "object_relation": "metadata",
}


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}


def _contains_private_data(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = key.casefold()
            if (
                normalized_key.endswith(("_path", "_uri"))
                or "token" in normalized_key
                or "secret" in normalized_key
            ):
                return True
            if _contains_private_data(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_private_data(item) for item in value)
    if isinstance(value, str):
        return "file:///" in value.casefold() or bool(WINDOWS_PATH_PATTERN.search(value))
    return False


def _safe_evidence_value(value: str) -> str:
    """Remove path-shaped fixture/canonical metadata from an evidence snippet."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return RELATIVE_MEDIA_PATH_PATTERN.sub("[private-reference-redacted]", value)
    if isinstance(payload, dict):
        blocked = {"raw_uri", "raw_path", "relative_path", "absolute_path", "database_path", "token"}
        payload = {key: item for key, item in payload.items() if key.casefold() not in blocked}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return RELATIVE_MEDIA_PATH_PATTERN.sub("[private-reference-redacted]", value)


def _load_private_environment(path: Path) -> None:
    allowed = {
        "HUMAN_OS_TOOL_TOKEN", "HUMAN_OS_BRIDGE_TOKEN",
        "HUMAN_OS_TOOL_BASE_URL", "HUMAN_OS_TOOL_BRIDGE_URL",
        "HUMAN_OS_TOOL_TIMEOUT",
    }
    if not path.is_file():
        raise ValueError("private environment file does not exist")
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid private environment line: {number}")
        key, value = line.split("=", 1)
        if key not in allowed:
            # A shared bridge.env may contain bridge-only settings; ignore them rather
            # than importing arbitrary process environment.
            if (
                key.startswith("HUMAN_OS_BRIDGE_")
                or key.startswith("HUMAN_OS_TOOL_")
                or key == "HUMAN_OS_LOCAL_API_URL"
            ):
                continue
            raise ValueError(f"unsupported private environment key: {key}")
        os.environ[key] = value


@dataclass(frozen=True)
class ToolConfig:
    bridge_url: str
    bearer_token: str
    timeout: float = 10.0
    allow_http_loopback_internal: bool = False
    source_address: str | None = None

    def validate(self) -> None:
        parsed = urlsplit(self.bridge_url)
        valid_https = parsed.scheme == "https" and bool(parsed.hostname)
        valid_test_http = (
            self.allow_http_loopback_internal
            and parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
        )
        if not (valid_https or valid_test_http):
            raise ValueError("tool bridge URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("tool bridge URL must be an origin without credentials")
        if parsed.path not in ("", "/"):
            raise ValueError("tool bridge URL must not contain a path")
        if not re.fullmatch(r"[0-9a-fA-F]{64,}", self.bearer_token):
            raise ValueError("bridge token must contain at least 256 random bits as hex")
        if self.timeout <= 0:
            raise ValueError("tool timeout must be positive")
        if self.source_address is not None:
            try:
                address = ipaddress.ip_address(self.source_address)
            except ValueError as exc:
                raise ValueError("tool source address must be an IPv4 address") from exc
            if address.version != 4:
                raise ValueError("tool source address must be an IPv4 address")

    @classmethod
    def from_environment(cls) -> "ToolConfig":
        config = cls(
            bridge_url=os.environ.get(
                "HUMAN_OS_TOOL_BASE_URL",
                os.environ.get("HUMAN_OS_TOOL_BRIDGE_URL", ""),
            ),
            bearer_token=os.environ.get(
                "HUMAN_OS_TOOL_TOKEN",
                os.environ.get("HUMAN_OS_BRIDGE_TOKEN", ""),
            ),
            timeout=float(os.environ.get("HUMAN_OS_TOOL_TIMEOUT", "10")),
        )
        config.validate()
        return config


class HumanOSToolClient:
    """Minimal read-only client. It never opens INDEX, filesystem, or RAW."""

    def __init__(self, config: ToolConfig) -> None:
        config.validate()
        self.config = config
        # The tool API's bridge hop is strictly loopback.  Do not allow a
        # machine-wide HTTP(S) proxy or VPN proxy settings to capture it.
        # External HTTPS clients retain urllib's normal proxy behaviour.
        if config.allow_http_loopback_internal:
            self._urlopen = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            ).open
        elif config.source_address:
            source_address = (config.source_address, 0)

            class SourceBoundHTTPSHandler(urllib.request.HTTPSHandler):
                def https_open(self, request):  # type: ignore[no-untyped-def]
                    def connection(host, **kwargs):  # type: ignore[no-untyped-def]
                        return http.client.HTTPSConnection(
                            host, source_address=source_address, **kwargs
                        )

                    return self.do_open(
                        connection,
                        request,
                        context=self._context,
                    )

            self._urlopen = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), SourceBoundHTTPSHandler()
            ).open
        else:
            self._urlopen = urllib.request.urlopen

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.bearer_token}",
            "User-Agent": "HumanOS-ToolClient/1.0",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.allow_http_loopback_internal:
            headers["X-Forwarded-Proto"] = "https"
        request = urllib.request.Request(
            self.config.bridge_url.rstrip("/") + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._urlopen(request, timeout=self.config.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except (TimeoutError, socket.timeout):
            return _error("timeout", "Human OS bridge request timed out")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return _error("timeout", "Human OS bridge request timed out")
            return _error("bridge_unreachable", "Human OS bridge is unreachable")
        if self.config.bearer_token.encode("ascii") in raw:
            return _error("unsafe_response", "Human OS bridge response contained private authentication data")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("malformed_response", "Human OS bridge returned invalid JSON")
        if not isinstance(decoded, dict):
            return _error("malformed_response", "Human OS bridge returned invalid JSON")
        if status == 401:
            return _error("unauthorized", "Human OS bridge rejected the bearer token")
        if status == 429:
            return _error("rate_limited", "Human OS bridge rate limit was exceeded")
        if status == 404:
            return _error("not_found", "Human OS object or endpoint was not found")
        if status >= 500:
            return _error("bridge_error", "Human OS bridge or Local API failed")
        if status >= 400:
            return _error("invalid_request", "Human OS bridge rejected the request")
        if _contains_private_data(decoded):
            return _error("unsafe_response", "Human OS bridge response contained private path data")
        return decoded

    @staticmethod
    def _compact_evidence(evidence: dict[str, Any]) -> dict[str, Any] | None:
        semantic_type = evidence.get("semantic_type")
        value = evidence.get("snippet")
        if not isinstance(semantic_type, str) or not isinstance(value, str):
            return None
        value = (
            "canonical metadata matched (private details redacted)"
            if semantic_type == "canonical_metadata"
            else _safe_evidence_value(value)
        )
        confidence = evidence.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = 0.0
        canonical_fact = evidence.get("assertion") == "canonical_fact"
        # A matched derived caption/object remains candidate evidence even when the
        # overall hit is incomplete. Only synthesized missing-concept markers below
        # are inferences.
        assessment = "fact" if canonical_fact else "candidate"
        return {
            "type": SEMANTIC_TYPES.get(semantic_type, "metadata"),
            "value": value,
            "confidence": round(float(confidence), 6),
            "is_fact": canonical_fact,
            "assessment": assessment,
        }

    def human_os_search(self, query: str, limit: int = 10) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            return _error("invalid_request", "query must be non-empty text")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            return _error("invalid_request", "limit must be an integer between 1 and 100")
        response = self._request("POST", "/v1/search", {"query": query, "limit": limit})
        if response.get("ok") is False:
            return response
        results = response.get("results")
        request_id = response.get("request_id")
        if not isinstance(results, list) or not isinstance(request_id, str):
            return _error("malformed_response", "Human OS bridge search response is invalid")
        compact_results = []
        warnings: list[str] = []
        explanation = response.get("explanation")
        if isinstance(explanation, str) and explanation:
            warnings.append(explanation)
        unsupported = response.get("unsupported_concepts", [])
        if isinstance(unsupported, list) and unsupported:
            warnings.append(
                "No direct semantic evidence for: " + ", ".join(map(str, unsupported))
            )
        for item in results:
            if not isinstance(item, dict):
                return _error("malformed_response", "Human OS bridge result is invalid")
            required = ("object_id", "score", "safe_ref", "reason")
            if any(key not in item for key in required):
                return _error("malformed_response", "Human OS bridge result is invalid")
            object_id = item.get("object_id")
            safe_ref = item.get("safe_ref")
            reason = item.get("reason")
            occurred_at = item.get("occurred_at")
            if (
                not isinstance(object_id, str)
                or not OBJECT_ID_PATTERN.fullmatch(object_id)
                or safe_ref != f"human-os://object/{object_id}"
                or not isinstance(reason, str)
                or not (occurred_at is None or isinstance(occurred_at, str))
            ):
                return _error("malformed_response", "Human OS bridge result is invalid")
            compact_evidence = []
            raw_evidence = item.get("matched_evidence", [])
            if not isinstance(raw_evidence, list):
                return _error("malformed_response", "Human OS bridge evidence is invalid")
            for evidence in raw_evidence:
                if not isinstance(evidence, dict):
                    return _error("malformed_response", "Human OS bridge evidence is invalid")
                if {"type", "value", "confidence", "is_fact", "assessment"} <= set(evidence):
                    if (
                        evidence["type"] not in {"caption", "ocr", "object", "place", "metadata"}
                        or not isinstance(evidence["value"], str)
                        or not isinstance(evidence["confidence"], (int, float))
                        or isinstance(evidence["confidence"], bool)
                        or not 0 <= float(evidence["confidence"]) <= 1
                        or not isinstance(evidence["is_fact"], bool)
                        or evidence["assessment"] not in {"fact", "candidate", "inference"}
                        or evidence["is_fact"] != (evidence["assessment"] == "fact")
                    ):
                        return _error("malformed_response", "Human OS tool evidence is invalid")
                    compact = {
                        key: evidence[key]
                        for key in ("type", "value", "confidence", "is_fact", "assessment")
                    }
                else:
                    compact = self._compact_evidence(evidence)
                if compact is not None:
                    compact_evidence.append(compact)
            missing = item.get("missing_evidence", [])
            if isinstance(missing, list):
                for concept in missing:
                    compact_evidence.append(
                        {
                            "type": "object" if concept == "swimwear" else "metadata",
                            "value": f"unconfirmed: {concept}",
                            "confidence": 0.0,
                            "is_fact": False,
                            "assessment": "inference",
                        }
                    )
            score = item.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return _error("malformed_response", "Human OS bridge score is invalid")
            compact_results.append(
                {
                    "object_id": object_id,
                    "score": round(float(score), 6),
                    "occurred_at": occurred_at,
                    "media_type": "photo",
                    "safe_ref": safe_ref,
                    "reason": reason,
                    "matched_evidence": compact_evidence,
                }
            )
        return {
            "query": query,
            "results": compact_results,
            "warnings": list(dict.fromkeys(warnings)),
            "request_id": request_id,
        }

    def human_os_get_object(self, object_id: str) -> dict[str, Any]:
        if not isinstance(object_id, str) or not OBJECT_ID_PATTERN.fullmatch(object_id):
            return _error("invalid_request", "invalid object_id")
        response = self._request("GET", f"/v1/object/{quote(object_id, safe='')}")
        if response.get("ok") is False:
            return response
        if isinstance(response.get("object"), dict):
            if response["object"].get("object_id") != object_id or not isinstance(
                response.get("request_id"), str
            ):
                return _error("malformed_response", "Human OS tool object response is invalid")
            return response
        request_id = response.pop("request_id", None)
        safe_ref = response.get("safe_ref")
        if (
            not isinstance(request_id, str)
            or response.get("object_id") != object_id
            or safe_ref != f"human-os://object/{object_id}"
        ):
            return _error("malformed_response", "Human OS bridge object response is invalid")
        raw_evidence = response.get("evidence", [])
        if not isinstance(raw_evidence, list):
            return _error("malformed_response", "Human OS bridge object evidence is invalid")
        compact_evidence = []
        for evidence in raw_evidence:
            if not isinstance(evidence, dict):
                return _error("malformed_response", "Human OS bridge object evidence is invalid")
            semantic_type = evidence.get("semantic_type")
            value = evidence.get("text")
            if not isinstance(value, str):
                result = evidence.get("result")
                value = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            compact = self._compact_evidence({
                "semantic_type": semantic_type,
                "snippet": value,
                "confidence": evidence.get("confidence"),
                "assertion": evidence.get("assertion"),
            })
            if compact is not None:
                compact_evidence.append(compact)
        media_type = response.get("object_type")
        if not isinstance(media_type, str):
            return _error("malformed_response", "Human OS bridge object response is invalid")
        return {
            "object": {
                "object_id": object_id,
                "media_type": media_type,
                "occurred_at": response.get("occurred_at"),
                "captured_at": response.get("captured_at"),
                "mime_type": response.get("mime_type"),
                "safe_ref": safe_ref,
                "evidence": compact_evidence,
            },
            "request_id": request_id,
        }

    def human_os_health(self) -> dict[str, Any]:
        response = self._request("GET", "/bridge/health")
        if response.get("ok") is False:
            return response
        required = ("bridge_alive", "local_api_reachable", "bridge_opens_database", "request_id")
        if any(key not in response for key in required):
            return _error("malformed_response", "Human OS bridge health response is invalid")
        return {"ok": True, **response}


def _client() -> HumanOSToolClient:
    return HumanOSToolClient(ToolConfig.from_environment())


def human_os_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search private Human OS memory through the authenticated HTTPS bridge."""
    return _client().human_os_search(query, limit)


def human_os_get_object(object_id: str) -> dict[str, Any]:
    """Get one safe canonical Human OS object view through the HTTPS bridge."""
    return _client().human_os_get_object(object_id)


def human_os_health() -> dict[str, Any]:
    """Check bridge and Local API availability without opening INDEX directly."""
    return _client().human_os_health()


def main() -> None:
    parser = argparse.ArgumentParser(description=TOOL_DESCRIPTION)
    parser.add_argument("--env-file", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    object_parser = subparsers.add_parser("get-object")
    object_parser.add_argument("object_id")
    subparsers.add_parser("health")
    args = parser.parse_args()
    if args.env_file is not None:
        _load_private_environment(args.env_file)
    client = _client()
    if args.operation == "search":
        result = client.human_os_search(args.query, args.limit)
    elif args.operation == "get-object":
        result = client.human_os_get_object(args.object_id)
    else:
        result = client.human_os_health()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
