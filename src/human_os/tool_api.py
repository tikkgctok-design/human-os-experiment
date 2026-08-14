"""Strict ChatGPT-facing HTTP tool adapter over the loopback Human OS bridge."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .bridge import (
    AuditLogger, RateLimiter, _principal_hash,
    load_private_environment as load_bridge_environment,
)
from .tool_client import HumanOSToolClient, ToolConfig, _contains_private_data


OBJECT_ID_PATTERN = re.compile(r"^hos_obj_[0-9a-f]{32}$")
ALLOWED_ENV_KEYS = {
    "HUMAN_OS_TOOL_TOKEN", "HUMAN_OS_BRIDGE_TOKEN", "HUMAN_OS_TOOL_BRIDGE_URL",
    "HUMAN_OS_TOOL_HOST", "HUMAN_OS_TOOL_PORT", "HUMAN_OS_TOOL_TIMEOUT",
    "HUMAN_OS_TOOL_BODY_LIMIT", "HUMAN_OS_TOOL_RATE_LIMIT",
    "HUMAN_OS_TOOL_RATE_WINDOW", "HUMAN_OS_TOOL_AUDIT_LOG",
    "HUMAN_OS_TOOL_REQUIRE_HTTPS",
}


@dataclass(frozen=True)
class ToolAPIConfig:
    external_token: str
    bridge_token: str
    bridge_url: str = "http://127.0.0.1:8787"
    host: str = "127.0.0.1"
    port: int = 8899
    bridge_timeout: float = 10.0
    request_body_limit: int = 64 * 1024
    rate_limit_requests: int = 60
    rate_limit_window_seconds: float = 60.0
    audit_log_path: Path | None = None
    require_forwarded_https: bool = True

    def validate(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("tool API may bind only to 127.0.0.1")
        parsed = urlsplit(self.bridge_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("tool API bridge must be an http://127.0.0.1:<port> origin")
        for name, token in (
            ("external tool token", self.external_token),
            ("internal bridge token", self.bridge_token),
        ):
            if not re.fullmatch(r"[0-9a-fA-F]{64,}", token):
                raise ValueError(f"{name} must contain at least 256 random bits as hex")
        if hmac.compare_digest(self.external_token, self.bridge_token):
            raise ValueError("external tool token and internal bridge token must differ")
        if self.bridge_timeout <= 0 or self.request_body_limit < 1:
            raise ValueError("tool API timeout and body limit must be positive")
        if self.rate_limit_requests < 1 or self.rate_limit_window_seconds <= 0:
            raise ValueError("tool API rate limit values must be positive")

    @classmethod
    def from_environment(cls) -> "ToolAPIConfig":
        audit = os.environ.get("HUMAN_OS_TOOL_AUDIT_LOG")
        config = cls(
            external_token=os.environ.get("HUMAN_OS_TOOL_TOKEN", ""),
            bridge_token=os.environ.get("HUMAN_OS_BRIDGE_TOKEN", ""),
            bridge_url=os.environ.get(
                "HUMAN_OS_TOOL_BRIDGE_URL", "http://127.0.0.1:8787"
            ).rstrip("/"),
            host=os.environ.get("HUMAN_OS_TOOL_HOST", "127.0.0.1"),
            port=int(os.environ.get("HUMAN_OS_TOOL_PORT", "8899")),
            bridge_timeout=float(os.environ.get("HUMAN_OS_TOOL_TIMEOUT", "10")),
            request_body_limit=int(
                os.environ.get("HUMAN_OS_TOOL_BODY_LIMIT", str(64 * 1024))
            ),
            rate_limit_requests=int(
                os.environ.get("HUMAN_OS_TOOL_RATE_LIMIT", "60")
            ),
            rate_limit_window_seconds=float(
                os.environ.get("HUMAN_OS_TOOL_RATE_WINDOW", "60")
            ),
            audit_log_path=Path(audit) if audit else None,
            require_forwarded_https=os.environ.get(
                "HUMAN_OS_TOOL_REQUIRE_HTTPS", "true"
            ).casefold() not in {"0", "false", "no"},
        )
        config.validate()
        return config


def load_private_environment(path: Path) -> None:
    """Load only tool API keys without printing private values."""
    if not path.is_file():
        raise ValueError("private environment file does not exist")
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid private environment line: {number}")
        key, value = line.split("=", 1)
        if key not in ALLOWED_ENV_KEYS:
            raise ValueError(f"unsupported private environment key: {key}")
        os.environ[key] = value


def _status_for_tool_result(payload: dict[str, Any]) -> int:
    error = payload.get("error") if payload.get("ok") is False else None
    error_type = error.get("type") if isinstance(error, dict) else None
    return {
        "invalid_request": HTTPStatus.BAD_REQUEST,
        "not_found": HTTPStatus.NOT_FOUND,
        "unauthorized": HTTPStatus.BAD_GATEWAY,
        "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
        "timeout": HTTPStatus.GATEWAY_TIMEOUT,
        "bridge_unreachable": HTTPStatus.SERVICE_UNAVAILABLE,
        "bridge_error": HTTPStatus.BAD_GATEWAY,
        "malformed_response": HTTPStatus.BAD_GATEWAY,
        "unsafe_response": HTTPStatus.BAD_GATEWAY,
    }.get(error_type, HTTPStatus.OK)


def make_handler(config: ToolAPIConfig) -> type[BaseHTTPRequestHandler]:
    config.validate()
    client = HumanOSToolClient(ToolConfig(
        bridge_url=config.bridge_url,
        bearer_token=config.bridge_token,
        timeout=config.bridge_timeout,
        allow_http_loopback_internal=True,
    ))
    limiter = RateLimiter(config.rate_limit_requests, config.rate_limit_window_seconds)
    audit = AuditLogger(config.audit_log_path)

    class ToolHandler(BaseHTTPRequestHandler):
        server_version = "HumanOSToolAPI/1.0"

        @staticmethod
        def _error(error_type: str, message: str) -> dict[str, Any]:
            return {"ok": False, "error": {"type": error_type, "message": message}}

        def _json(self, status: int, payload: dict[str, Any], request_id: str) -> None:
            if payload.get("ok") is not False and "request_id" in payload:
                payload = {**payload, "request_id": request_id}
            if _contains_private_data(payload):
                status = HTTPStatus.BAD_GATEWAY
                payload = self._error("unsafe_response", "private data was blocked")
            serialized = json.dumps(payload, ensure_ascii=False)
            if config.external_token in serialized or config.bridge_token in serialized:
                status = HTTPStatus.BAD_GATEWAY
                payload = self._error("unsafe_response", "private data was blocked")
                serialized = json.dumps(payload, ensure_ascii=False)
            body = serialized.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", request_id)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorize(self) -> tuple[bool, str]:
            header = self.headers.get("Authorization", "")
            candidate = header[7:] if header.startswith("Bearer ") else ""
            valid = hmac.compare_digest(
                candidate.encode("utf-8"), config.external_token.encode("utf-8")
            )
            return valid, candidate

        def _route(self, method: str, path: str) -> tuple[int, dict[str, Any], int]:
            if config.require_forwarded_https and self.headers.get(
                "X-Forwarded-Proto", ""
            ).casefold() != "https":
                return HTTPStatus.UPGRADE_REQUIRED, self._error(
                    "https_required", "external access requires HTTPS"
                ), 0
            authorized, candidate = self._authorize()
            if not authorized:
                return HTTPStatus.UNAUTHORIZED, self._error(
                    "unauthorized", "valid bearer token required"
                ), 0
            principal = _principal_hash(candidate)
            if not limiter.allow(principal):
                return HTTPStatus.TOO_MANY_REQUESTS, self._error(
                    "rate_limited", "rate limit exceeded"
                ), 0

            if method == "GET" and path == "/bridge/health":
                payload = client.human_os_health()
                return _status_for_tool_result(payload), payload, 0

            if method == "POST" and path == "/v1/search":
                raw_length = self.headers.get("Content-Length", "")
                try:
                    length = int(raw_length)
                except ValueError:
                    return HTTPStatus.BAD_REQUEST, self._error(
                        "invalid_request", "valid Content-Length required"
                    ), 0
                if not 0 < length <= config.request_body_limit:
                    self.close_connection = True
                    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > config.request_body_limit else HTTPStatus.BAD_REQUEST
                    return status, self._error(
                        "invalid_request", "request body size is invalid"
                    ), 0
                try:
                    incoming = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return HTTPStatus.BAD_REQUEST, self._error(
                        "invalid_request", "request body must be valid JSON"
                    ), 0
                if not isinstance(incoming, dict) or set(incoming) - {"query", "limit"}:
                    return HTTPStatus.BAD_REQUEST, self._error(
                        "invalid_request", "only query and limit are allowed"
                    ), 0
                payload = client.human_os_search(
                    incoming.get("query"), incoming.get("limit", 10)
                )
                status = _status_for_tool_result(payload)
                count = len(payload.get("results", [])) if status == 200 else 0
                return status, payload, count

            prefix = "/v1/object/"
            if method == "GET" and path.startswith(prefix):
                object_id = path[len(prefix):]
                if not OBJECT_ID_PATTERN.fullmatch(object_id):
                    return HTTPStatus.BAD_REQUEST, self._error(
                        "invalid_request", "invalid object_id"
                    ), 0
                payload = client.human_os_get_object(object_id)
                return _status_for_tool_result(payload), payload, 0

            return HTTPStatus.NOT_FOUND, self._error(
                "invalid_request", "endpoint is not exposed"
            ), 0

        def _handle(self, method: str) -> None:
            request_id = str(uuid.uuid4())
            started = time.perf_counter()
            raw_path = urlsplit(self.path).path
            path = unquote(raw_path)
            traversal = (
                "\\" in path
                or any(segment == ".." for segment in path.split("/"))
                or "%2f" in raw_path.casefold()
                or "%5c" in raw_path.casefold()
            )
            if traversal:
                status, payload, result_count = HTTPStatus.BAD_REQUEST, self._error(
                    "invalid_request", "path traversal is not allowed"
                ), 0
            else:
                status, payload, result_count = self._route(method, path)
            self._json(status, payload, request_id)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            header = self.headers.get("Authorization", "")
            candidate = header[7:] if header.startswith("Bearer ") else "anonymous"
            audit.write({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "endpoint": path if path in {"/v1/search", "/bridge/health"} or path.startswith("/v1/object/") else "disallowed",
                "http_status": int(status),
                "latency_ms": latency_ms,
                "result_count": result_count,
                "principal_hash": _principal_hash(candidate),
            })

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ToolHandler


def serve(config: ToolAPIConfig) -> None:
    config.validate()
    with ThreadingHTTPServer((config.host, config.port), make_handler(config)) as server:
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-env-file", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.bridge_env_file is not None:
        load_bridge_environment(args.bridge_env_file)
    if args.env_file is not None:
        load_private_environment(args.env_file)
    serve(ToolAPIConfig.from_environment())


if __name__ == "__main__":
    main()
