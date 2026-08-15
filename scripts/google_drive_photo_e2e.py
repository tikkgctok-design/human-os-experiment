"""Manual bounded read-only Google Drive PHOTO pipeline validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from human_os.google_drive_validation import (  # noqa: E402
    MAX_LIVE_PHOTOS,
    validate_google_drive_photos,
)

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def build_authorized_drive_service(client_config: Path, token_path: Path) -> Any:
    """Create Drive v3 service; Google imports stay outside production runtime."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Live validation requires google-api-python-client and "
            "google-auth-oauthlib in the active environment"
        ) from exc

    if not client_config.is_file():
        raise FileNotFoundError("OAuth client config does not exist")
    credentials = None
    if token_path.is_file():
        credentials = Credentials.from_authorized_user_file(
            str(token_path), [DRIVE_READONLY_SCOPE]
        )
    if credentials is None or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_config), [DRIVE_READONLY_SCOPE]
            )
            credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_token = token_path.with_suffix(token_path.suffix + ".tmp")
        temporary_token.write_text(credentials.to_json(), encoding="utf-8")
        os.replace(temporary_token, token_path)
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder-id", default=os.environ.get("HUMAN_OS_GOOGLE_DRIVE_FOLDER_ID")
    )
    parser.add_argument(
        "--source-namespace",
        default=os.environ.get("HUMAN_OS_GOOGLE_DRIVE_SOURCE_NAMESPACE"),
    )
    parser.add_argument(
        "--client-config",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("HUMAN_OS_GOOGLE_DRIVE_CLIENT_CONFIG"))
            else None
        ),
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("HUMAN_OS_GOOGLE_DRIVE_TOKEN"))
            else None
        ),
    )
    parser.add_argument("--limit", type=int, default=MAX_LIVE_PHOTOS)
    parser.add_argument(
        "--schema", type=Path, default=ROOT / "schema" / "human_os.sqlite.sql"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.folder_id:
        parser.error("--folder-id is required; whole-Drive discovery is forbidden")
    if not args.source_namespace:
        parser.error("--source-namespace is required")
    if args.client_config is None:
        parser.error("--client-config is required")
    if args.token is None:
        parser.error("--token is required")
    if not 1 <= args.limit <= MAX_LIVE_PHOTOS:
        parser.error(f"--limit must be between 1 and {MAX_LIVE_PHOTOS}")

    try:
        service = build_authorized_drive_service(args.client_config, args.token)
        report = validate_google_drive_photos(
            service,
            folder_id=args.folder_id,
            source_namespace=args.source_namespace,
            schema_path=args.schema,
            limit=args.limit,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
