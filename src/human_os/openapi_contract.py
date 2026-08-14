"""Validate and render the Human OS OpenAPI contract for a stable HTTPS origin."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


EXPECTED_PATHS = {"/v1/search", "/v1/object/{object_id}", "/bridge/health"}
EXPECTED_OPERATIONS = {
    "human_os_search", "human_os_get_object", "human_os_health"
}


def validate_public_origin(value: str, *, allow_template: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public tool URL must be an HTTPS origin without path or credentials")
    hostname = parsed.hostname.casefold()
    if not allow_template and (
        hostname.endswith(".example.com")
        or hostname.endswith(".example.invalid")
        or hostname in {"localhost", "127.0.0.1"}
    ):
        raise ValueError("public tool URL must be a real stable hostname")
    return value.rstrip("/")


def validate_schema(schema: dict[str, Any], *, require_production_server: bool) -> None:
    if schema.get("openapi") != "3.1.0":
        raise ValueError("OpenAPI 3.1.0 is required")
    paths = schema.get("paths")
    if not isinstance(paths, dict) or set(paths) != EXPECTED_PATHS:
        raise ValueError("OpenAPI must expose exactly the three Human OS paths")
    operations = {
        operation.get("operationId")
        for path in paths.values()
        for method, operation in path.items()
        if method in {"get", "post"} and isinstance(operation, dict)
    }
    if operations != EXPECTED_OPERATIONS:
        raise ValueError("OpenAPI operationId set is invalid")
    servers = schema.get("servers")
    if not isinstance(servers, list) or len(servers) != 1:
        raise ValueError("OpenAPI must define exactly one server")
    validate_public_origin(
        servers[0].get("url", ""), allow_template=not require_production_server
    )
    security = schema.get("components", {}).get("securitySchemes", {}).get("bearerAuth")
    if security != {
        "type": "http", "scheme": "bearer", "bearerFormat": "opaque-256-bit-token"
    }:
        raise ValueError("OpenAPI bearer security scheme is invalid")
    for name in (
        "Evidence", "SearchResult", "SearchToolResponse", "ObjectView",
        "ObjectToolResponse", "HealthToolResponse", "ErrorResponse",
    ):
        definition = schema.get("components", {}).get("schemas", {}).get(name)
        if not isinstance(definition, dict) or definition.get("additionalProperties") is not False:
            raise ValueError(f"OpenAPI schema {name} must be strict")


def render_schema(template: Path, public_origin: str, output: Path) -> dict[str, Any]:
    origin = validate_public_origin(public_origin)
    schema = json.loads(template.read_text(encoding="utf-8"))
    schema["servers"] = [{
        "url": origin,
        "description": "Stable authenticated Human OS read-only tool endpoint",
    }]
    validate_schema(schema, require_production_server=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return schema


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template", type=Path,
        default=Path("tool-schema/human_os.openapi.json"),
    )
    parser.add_argument("--output", type=Path, default=Path("private/human_os.openapi.json"))
    parser.add_argument("--public-url", default=os.environ.get("HUMAN_OS_PUBLIC_BASE_URL", ""))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        schema = json.loads(args.template.read_text(encoding="utf-8"))
        validate_schema(schema, require_production_server=False)
        print("OpenAPI template: ok")
        return
    render_schema(args.template, args.public_url, args.output)
    print(f"OpenAPI import copy written to {args.output}")


if __name__ == "__main__":
    main()
