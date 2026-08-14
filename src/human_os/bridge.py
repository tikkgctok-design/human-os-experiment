"""Authenticated provider-neutral read-only bridge to the localhost Retrieval API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


OBJECT_ID_PATTERN = re.compile(r"^hos_obj_[0-9a-f]{32}$")
WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:[\\/][^\r\n]*")
FILE_URI_PATTERN = re.compile(r"file:///[^\s\"']+")
ALLOWED_AUDIT_FIELDS = {
    "timestamp", "request_id", "endpoint", "http_status", "latency_ms",
    "result_count", "principal_hash",
}
ALLOWED_ENV_KEYS = {
    "HUMAN_OS_BRIDGE_TOKEN", "HUMAN_OS_LOCAL_API_URL", "HUMAN_OS_BRIDGE_HOST",
    "HUMAN_OS_BRIDGE_PORT", "HUMAN_OS_BRIDGE_BODY_LIMIT",
    "HUMAN_OS_BRIDGE_UPSTREAM_TIMEOUT", "HUMAN_OS_BRIDGE_RATE_LIMIT",
    "HUMAN_OS_BRIDGE_RATE_WINDOW", "HUMAN_OS_BRIDGE_AUDIT_LOG",
    "HUMAN_OS_BRIDGE_REQUIRE_HTTPS",
}


@dataclass(frozen=True)
class BridgeConfig:
    bearer_token: str
    upstream_base_url: str = "http://127.0.0.1:8765"
    host: str = "127.0.0.1"
    port: int = 8787
    request_body_limit: int = 64 * 1024
    upstream_timeout: float = 5.0
    rate_limit_requests: int = 60
    rate_limit_window_seconds: float = 60.0
    audit_log_path: Path | None = None
    require_forwarded_https: bool = True

    def validate(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("bridge may bind only to 127.0.0.1")
        parsed = urlsplit(self.upstream_base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("upstream must be an http://127.0.0.1:<port> origin")
        if not re.fullmatch(r"[0-9a-fA-F]{64,}", self.bearer_token):
            raise ValueError("bearer token must contain at least 256 random bits as hex")
        if self.request_body_limit < 1:
            raise ValueError("request body limit must be positive")
        if self.upstream_timeout <= 0:
            raise ValueError("upstream timeout must be positive")
        if self.rate_limit_requests < 1 or self.rate_limit_window_seconds <= 0:
            raise ValueError("rate limit values must be positive")

    @classmethod
    def from_environment(cls) -> "BridgeConfig":
        token = os.environ.get("HUMAN_OS_BRIDGE_TOKEN", "")
        audit = os.environ.get("HUMAN_OS_BRIDGE_AUDIT_LOG")
        config = cls(
            bearer_token=token,
            upstream_base_url=os.environ.get(
                "HUMAN_OS_LOCAL_API_URL", "http://127.0.0.1:8765"
            ).rstrip("/"),
            host=os.environ.get("HUMAN_OS_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("HUMAN_OS_BRIDGE_PORT", "8787")),
            request_body_limit=int(
                os.environ.get("HUMAN_OS_BRIDGE_BODY_LIMIT", str(64 * 1024))
            ),
            upstream_timeout=float(
                os.environ.get("HUMAN_OS_BRIDGE_UPSTREAM_TIMEOUT", "5")
            ),
            rate_limit_requests=int(
                os.environ.get("HUMAN_OS_BRIDGE_RATE_LIMIT", "60")
            ),
            rate_limit_window_seconds=float(
                os.environ.get("HUMAN_OS_BRIDGE_RATE_WINDOW", "60")
            ),
            audit_log_path=Path(audit) if audit else None,
            require_forwarded_https=os.environ.get(
                "HUMAN_OS_BRIDGE_REQUIRE_HTTPS", "true"
            ).casefold() not in {"0", "false", "no"},
        )
        config.validate()
        return config


class RateLimiter:
    def __init__(self, request_limit: int, window_seconds: float) -> None:
        self.request_limit = request_limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, principal: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            requests = self._requests[principal]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.request_limit:
                return False
            requests.append(current)
            return True


class AuditLogger:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        if set(record) != ALLOWED_AUDIT_FIELDS:
            raise ValueError("audit record contains forbidden fields")
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)


def load_private_environment(path: Path) -> None:
    """Load a strict KEY=VALUE private config without echoing secret values."""
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


def _safe_json(value: Any) -> Any:
    """Remove path-bearing keys and redact local paths in every upstream string."""
    if isinstance(value, dict):
        return {
            key: _safe_json(item)
            for key, item in value.items()
            if not (
                key.casefold().endswith(("_path", "_uri"))
                or "token" in key.casefold()
                or "secret" in key.casefold()
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        value = FILE_URI_PATTERN.sub("[local-path-redacted]", value)
        return WINDOWS_PATH_PATTERN.sub("[local-path-redacted]", value)
    return value


def _principal_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _upstream_json(
    config: BridgeConfig,
    method: str,
    path: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        config.upstream_base_url + path,
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=config.upstream_timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError("upstream timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError("upstream timeout") from exc
        raise ConnectionError("upstream unavailable") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectionError("upstream returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectionError("upstream returned invalid JSON")
    return status, _safe_json(payload)


def make_handler(config: BridgeConfig) -> type[BaseHTTPRequestHandler]:
    config.validate()
    limiter = RateLimiter(config.rate_limit_requests, config.rate_limit_window_seconds)
    audit = AuditLogger(config.audit_log_path)

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "HumanOSBridge/1.0"

        def _response(
            self, status: int, payload: dict[str, Any], request_id: str
        ) -> None:
            safe_payload = _safe_json({**payload, "request_id": request_id})
            body = json.dumps(safe_payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", request_id)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _error(code: str, message: str) -> dict[str, Any]:
            return {"error": {"code": code, "message": message}}

        def _authorized_token(self) -> tuple[bool, str]:
            header = self.headers.get("Authorization", "")
            candidate = header[7:] if header.startswith("Bearer ") else ""
            valid = hmac.compare_digest(
                candidate.encode("utf-8"), config.bearer_token.encode("utf-8")
            )
            return valid, candidate

        def _route(self, method: str, path: str) -> tuple[int, dict[str, Any], int]:
            if config.require_forwarded_https and self.headers.get(
                "X-Forwarded-Proto", ""
            ).casefold() != "https":
                return (
                    HTTPStatus.UPGRADE_REQUIRED,
                    self._error("https_required", "external access requires HTTPS"),
                    0,
                )

            authorized, candidate = self._authorized_token()
            if not authorized:
                return (
                    HTTPStatus.UNAUTHORIZED,
                    self._error("unauthorized", "valid bearer token required"),
                    0,
                )
            principal = _principal_hash(candidate)
            if not limiter.allow(principal):
                return (
                    HTTPStatus.TOO_MANY_REQUESTS,
                    self._error("rate_limited", "rate limit exceeded"),
                    0,
                )

            if method == "GET" and path == "/bridge/health":
                try:
                    upstream_status, _ = _upstream_json(
                        config, "GET", "/v1/health"
                    )
                    reachable = upstream_status == HTTPStatus.OK
                except (ConnectionError, TimeoutError):
                    reachable = False
                return (
                    HTTPStatus.OK,
                    {
                        "bridge_alive": True,
                        "local_api_reachable": reachable,
                        "bridge_opens_database": False,
                    },
                    0,
                )

            if method == "POST" and path == "/v1/search":
                content_length = self.headers.get("Content-Length")
                try:
                    length = int(content_length or "")
                except ValueError:
                    return (
                        HTTPStatus.BAD_REQUEST,
                        self._error("invalid_request", "valid Content-Length required"),
                        0,
                    )
                if not 0 < length <= config.request_body_limit:
                    self.close_connection = True
                    status = (
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        if length > config.request_body_limit
                        else HTTPStatus.BAD_REQUEST
                    )
                    return (
                        status,
                        self._error("invalid_request_size", "request body size is invalid"),
                        0,
                    )
                body = self.rfile.read(length)
                try:
                    parsed = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return (
                        HTTPStatus.BAD_REQUEST,
                        self._error("malformed_json", "request body must be valid JSON"),
                        0,
                    )
                if not isinstance(parsed, dict):
                    return (
                        HTTPStatus.BAD_REQUEST,
                        self._error("invalid_request", "request body must be a JSON object"),
                        0,
                    )
                canonical_body = json.dumps(
                    parsed, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                try:
                    status, payload = _upstream_json(
                        config, "POST", "/v1/search", canonical_body
                    )
                except TimeoutError:
                    return (
                        HTTPStatus.GATEWAY_TIMEOUT,
                        self._error("upstream_timeout", "Local API timed out"),
                        0,
                    )
                except ConnectionError:
                    return (
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        self._error("upstream_unavailable", "Local API unavailable"),
                        0,
                    )
                result_count = len(payload.get("results", [])) if status == 200 else 0
                return status, payload, result_count

            prefix = "/v1/object/"
            if method == "GET" and path.startswith(prefix):
                object_id = path[len(prefix):]
                if not OBJECT_ID_PATTERN.fullmatch(object_id):
                    return (
                        HTTPStatus.BAD_REQUEST,
                        self._error("invalid_object_id", "invalid object_id"),
                        0,
                    )
                try:
                    status, payload = _upstream_json(
                        config, "GET", f"/v1/object/{object_id}"
                    )
                except TimeoutError:
                    return (
                        HTTPStatus.GATEWAY_TIMEOUT,
                        self._error("upstream_timeout", "Local API timed out"),
                        0,
                    )
                except ConnectionError:
                    return (
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        self._error("upstream_unavailable", "Local API unavailable"),
                        0,
                    )
                return status, payload, 0

            return (
                HTTPStatus.NOT_FOUND,
                self._error("endpoint_not_allowed", "endpoint is not exposed"),
                0,
            )

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
                status, payload, result_count = (
                    HTTPStatus.BAD_REQUEST,
                    self._error("invalid_path", "path traversal is not allowed"),
                    0,
                )
            else:
                status, payload, result_count = self._route(method, path)
            self._response(status, payload, request_id)
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            auth_header = self.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else "anonymous"
            audit.write(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "endpoint": path if path in {
                        "/v1/search", "/bridge/health"
                    } or path.startswith("/v1/object/") else "disallowed",
                    "http_status": int(status),
                    "latency_ms": latency_ms,
                    "result_count": result_count,
                    "principal_hash": _principal_hash(token),
                }
            )

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

    return BridgeHandler


def serve(config: BridgeConfig) -> None:
    config.validate()
    with ThreadingHTTPServer((config.host, config.port), make_handler(config)) as server:
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.env_file is not None:
        load_private_environment(args.env_file)
    serve(BridgeConfig.from_environment())


if __name__ == "__main__":
    main()
