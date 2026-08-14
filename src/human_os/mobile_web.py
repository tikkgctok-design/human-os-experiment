"""Loopback-only mobile web gateway for the read-only Human OS Tool API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .media_preview import OBJECT_ID_PATTERN, PhotoPreviewStore

TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64,}$")
WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:[\\/]")
SESSION_COOKIE = "human_os_mobile_session"
TRUSTED_DEVICE_COOKIE = "human_os_mobile_device"
ALLOWED_ENV_KEYS = {
    "HUMAN_OS_MOBILE_TOKEN", "HUMAN_OS_TOOL_TOKEN", "HUMAN_OS_MOBILE_HOST",
    "HUMAN_OS_MOBILE_PORT", "HUMAN_OS_MOBILE_TOOL_URL", "HUMAN_OS_MOBILE_TIMEOUT",
    "HUMAN_OS_MOBILE_BODY_LIMIT", "HUMAN_OS_MOBILE_SESSION_TTL",
    "HUMAN_OS_MOBILE_TRUSTED_DEVICE_TTL",
    "HUMAN_OS_MOBILE_AUDIT_LOG", "HUMAN_OS_MOBILE_PUBLIC_ORIGIN",
    "HUMAN_OS_MOBILE_REQUIRE_HTTPS",
}

PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101826"><title>Human OS</title><style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0b1220;color:#e8eef9}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:linear-gradient(160deg,#0b1220,#152540);padding:calc(18px + env(safe-area-inset-top)) 16px calc(24px + env(safe-area-inset-bottom))}.app{max-width:860px;margin:auto}.brand{font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;color:#8fb7ff}.title{font-size:clamp(2rem,9vw,3.2rem);margin:.2rem 0 1.5rem}.card{background:#111d30dd;border:1px solid #2a3d5a;border-radius:20px;padding:18px;box-shadow:0 18px 45px #0006}label{display:block;font-size:.9rem;color:#b8c8df;margin-bottom:8px}input,textarea,button{font:inherit;width:100%;border-radius:13px}input,textarea{border:1px solid #385071;background:#091321;color:#fff;padding:14px}textarea{min-height:116px;resize:vertical}button{margin-top:12px;padding:14px;border:0;background:#67a1ff;color:#07111f;font-weight:750;cursor:pointer}button:disabled{opacity:.55}.hidden{display:none}.status{min-height:24px;margin:12px 2px;color:#b9c9de}.error{color:#ffaaa5}#answer{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.result{background:#0b1626;border:1px solid #30435e;border-radius:16px;padding:12px;overflow:hidden}.photo-link{display:block;margin:-12px -12px 12px}.photo{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;background:#07101c}.score{color:#82d8b7;margin-top:6px}.date,.location,.caption{font-size:.88rem;color:#c3d2e5;margin-top:7px}.caption{color:#e2eaf5}.evidence{font-size:.88rem;color:#c3d2e5;margin-top:8px}.pill{display:inline-block;padding:2px 8px;border:1px solid #45617f;border-radius:999px;font-size:.75rem;margin:3px 5px 0 0}.foot{font-size:.76rem;color:#8194ad;margin-top:16px}</style></head>
<body><main class="app"><div class="brand">Private memory · Read only</div><h1 class="title">Human OS</h1>
<section id="login" class="card"><label for="access">Mobile access token</label><input id="access" type="password" autocomplete="off" autocapitalize="off" spellcheck="false"><button id="loginBtn">Войти</button><p class="foot">Токен используется один раз для создания защищённой сессии и не сохраняется страницей.</p></section>
<section id="search" class="card hidden"><label for="query">Что найти в памяти?</label><textarea id="query" placeholder="Например: найди фотографии со снегом"></textarea><button id="ask">Спросить Human OS</button><div id="status" class="status" aria-live="polite"></div><div id="answer"></div></section></main>
<script>
'use strict';
let csrf='';const $=id=>document.getElementById(id);
function message(text,error=false){$('status').textContent=text;$('status').className='status'+(error?' error':'')}
async function jsonFetch(path,options={}){const r=await fetch(path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})}});let data;try{data=await r.json()}catch{const e=new Error('Сервер вернул некорректный ответ');e.status=r.status;throw e}if(!r.ok){const e=new Error((data.error&&data.error.message)||('HTTP '+r.status));e.status=r.status;e.type=data.error&&data.error.type;throw e}return data}
function openSearch(data){csrf=data.csrf_token;$('access').value='';$('login').classList.add('hidden');$('search').classList.remove('hidden')}
async function refreshSession(){const data=await jsonFetch('/mobile/session/refresh',{method:'POST',headers:{'X-Mobile-Refresh':'1'},body:'{}'});openSearch(data);return true}
async function withSession(action){try{return await action()}catch(e){if(e.status!==401)throw e;await refreshSession();return action()}}
function render(data){const root=$('answer');root.replaceChildren();if(!data.results.length){root.textContent='Ничего не найдено.';return}for(const item of data.results){const box=document.createElement('article');box.className='result';if(item.thumbnail_url&&item.preview_url){const link=document.createElement('a');link.className='photo-link';link.href=item.preview_url;link.target='_blank';link.rel='noopener';const img=document.createElement('img');img.className='photo';img.src=item.thumbnail_url;img.alt=item.caption||'Найденная фотография';img.loading='lazy';link.append(img);box.append(link)}const head=document.createElement('div');head.textContent=item.reason||'Найдено';box.append(head);const meta=document.createElement('div');meta.className='score';meta.textContent='Совпадение: '+Math.round(Number(item.score||0)*100)+'%';box.append(meta);if(item.occurred_at){const date=document.createElement('div');date.className='date';const parsed=new Date(item.occurred_at);date.textContent='Дата: '+(Number.isNaN(parsed.valueOf())?item.occurred_at:parsed.toLocaleString());box.append(date)}if(item.gps){const location=document.createElement('div');location.className='location';location.textContent='GPS: '+Number(item.gps.latitude).toFixed(5)+', '+Number(item.gps.longitude).toFixed(5);box.append(location)}if(item.caption){const caption=document.createElement('div');caption.className='caption';caption.textContent=item.caption;box.append(caption)}if(item.concepts&&item.concepts.length){const concepts=document.createElement('div');concepts.className='evidence';for(const value of item.concepts){const pill=document.createElement('span');pill.className='pill';pill.textContent=value;concepts.append(pill)}box.append(concepts)}for(const ev of item.matched_evidence||[]){const line=document.createElement('div');line.className='evidence';const pill=document.createElement('span');pill.className='pill';pill.textContent=ev.assessment||'candidate';line.append(pill,document.createTextNode(String(ev.value||'')));box.append(line)}root.append(box)}}
$('loginBtn').onclick=async()=>{const token=$('access').value.trim();if(!token)return message('Введите mobile access token.',true);$('loginBtn').disabled=true;message('Создаю защищённую сессию…');try{const data=await jsonFetch('/mobile/session',{method:'POST',body:JSON.stringify({token})});openSearch(data);$('query').focus();message('Готово. Это устройство запомнено безопасным HttpOnly cookie.')}catch(e){message(e.message,true)}finally{$('loginBtn').disabled=false}};
$('ask').onclick=async()=>{const query=$('query').value.trim();if(!query)return message('Введите запрос.',true);$('ask').disabled=true;message('Ищу в Human OS…');$('answer').replaceChildren();try{const search=()=>jsonFetch('/mobile/search',{method:'POST',headers:{'X-CSRF-Token':csrf},body:JSON.stringify({query,limit:10})});const data=await withSession(search);render(data);message('Готово')}catch(e){message(e.message,true)}finally{$('ask').disabled=false}};
refreshSession().then(()=>message('Защищённая сессия восстановлена.')).catch(()=>{});
</script></body></html>'''


def _unsafe(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            name = key.casefold()
            if name.endswith(("_path", "_uri")) or ("token" in name and name != "csrf_token") or "secret" in name:
                return True
            if _unsafe(item):
                return True
        return False
    if isinstance(value, list):
        return any(_unsafe(item) for item in value)
    if isinstance(value, str):
        lowered = value.casefold()
        return "file:///" in lowered or "sqlite" in lowered or bool(WINDOWS_PATH_PATTERN.search(value))
    return False


@dataclass(frozen=True)
class MobileConfig:
    mobile_token: str
    tool_token: str
    tool_url: str = "http://127.0.0.1:8899"
    host: str = "127.0.0.1"
    port: int = 8990
    timeout: float = 20.0
    body_limit: int = 32 * 1024
    session_ttl: int = 3600
    trusted_device_ttl: int = 30 * 24 * 3600
    audit_log: Path | None = None
    public_origin: str = "https://memory.humonosmemory.com"
    require_forwarded_https: bool = True
    database_path: Path = Path("private/human_os.db")
    preview_cache_dir: Path = Path("private/mobile-preview-cache")

    def validate(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("mobile gateway may bind only to 127.0.0.1")
        for token in (self.mobile_token, self.tool_token):
            if not TOKEN_PATTERN.fullmatch(token):
                raise ValueError("mobile and tool tokens must each contain at least 256 random bits as hex")
        if hmac.compare_digest(self.mobile_token, self.tool_token):
            raise ValueError("mobile and tool tokens must differ")
        parsed = urlsplit(self.tool_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/"):
            raise ValueError("mobile gateway upstream must be loopback HTTP")
        origin = urlsplit(self.public_origin)
        if origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/"):
            raise ValueError("mobile public origin must be an HTTPS origin")
        if self.timeout <= 0 or self.body_limit < 1 or self.session_ttl < 60:
            raise ValueError("mobile limits must be positive")
        if self.trusted_device_ttl < self.session_ttl or self.trusted_device_ttl > 180 * 24 * 3600:
            raise ValueError("trusted device TTL must cover the session and be at most 180 days")


class MobileSessionStore:
    def __init__(self, clock: Any = time.time) -> None:
        self._sessions: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def create(self, ttl: int) -> tuple[str, str]:
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[hashlib.sha256(session.encode()).hexdigest()] = (self.clock() + ttl, csrf)
        return session, csrf

    def verify(self, session: str, csrf: str) -> bool:
        key = hashlib.sha256(session.encode()).hexdigest()
        with self._lock:
            record = self._sessions.get(key)
            if not record or record[0] < self.clock():
                self._sessions.pop(key, None)
                return False
            return hmac.compare_digest(record[1], csrf)

    def verify_session(self, session: str) -> bool:
        key = hashlib.sha256(session.encode()).hexdigest()
        with self._lock:
            record = self._sessions.get(key)
            if not record or record[0] < self.clock():
                self._sessions.pop(key, None)
                return False
            return True


class TrustedDeviceTokens:
    """Issue restart-safe, signed device grants without storing server tokens client-side."""

    def __init__(self, secret: str, clock: Any = time.time) -> None:
        self._key = bytes.fromhex(secret)
        self._clock = clock

    @staticmethod
    def _encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def issue(self, ttl: int) -> str:
        payload = f"v1.{int(self._clock()) + ttl}.{secrets.token_urlsafe(32)}"
        signature = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{self._encode(signature)}"

    def verify(self, token: str) -> bool:
        try:
            version, expires, nonce, signature = token.split(".", 3)
            payload = f"{version}.{expires}.{nonce}"
            expected = self._encode(
                hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
            )
            valid_shape = version == "v1" and len(nonce) >= 32
            valid_time = int(expires) >= int(self._clock())
        except (TypeError, ValueError):
            return False
        return valid_shape and valid_time and hmac.compare_digest(signature, expected)


class Audit:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.Lock()

    def write(self, payload: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")


def make_handler(config: MobileConfig, sessions: MobileSessionStore | None = None) -> type[BaseHTTPRequestHandler]:
    config.validate()
    sessions = sessions or MobileSessionStore()
    trusted_devices = TrustedDeviceTokens(config.mobile_token, sessions.clock)
    previews = PhotoPreviewStore(config.database_path, config.preview_cache_dir)
    audit = Audit(config.audit_log)
    upstream_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class Handler(BaseHTTPRequestHandler):
        server_version = "HumanOSMobile/1.0"

        def _headers(self, content_type: str, length: int, *, cookies: tuple[str, ...] = ()) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)

        def _json(self, status: int, payload: dict[str, Any], request_id: str, *, cookies: tuple[str, ...] = ()) -> None:
            if _unsafe(payload) or config.mobile_token in json.dumps(payload) or config.tool_token in json.dumps(payload):
                status, payload = 502, {"ok": False, "error": {"type": "unsafe_response", "message": "Private data was blocked"}}
            payload = {**payload, "request_id": request_id}
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(raw), cookies=cookies)
            self.send_header("X-Request-ID", request_id)
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return None
            if not 0 < length <= config.body_limit:
                return None
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

        def _https_origin(self) -> bool:
            return (not config.require_forwarded_https) or (
                self.headers.get("X-Forwarded-Proto", "").casefold() == "https"
                and self.headers.get("Origin", config.public_origin) == config.public_origin
            )

        def _session(self) -> tuple[str, str]:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(SESSION_COOKIE)
            return (morsel.value if morsel else "", self.headers.get("X-CSRF-Token", ""))

        def _trusted_device(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(TRUSTED_DEVICE_COOKIE)
            return morsel.value if morsel else ""

        def _enrich_search(self, payload: dict[str, Any]) -> dict[str, Any]:
            results = payload.get("results")
            if not isinstance(results, list):
                return payload
            enriched = []
            for item in results:
                if not isinstance(item, dict):
                    enriched.append(item)
                    continue
                object_id = item.get("object_id")
                if item.get("media_type") == "photo" and isinstance(object_id, str):
                    try:
                        details = previews.details(object_id)
                    except (FileNotFoundError, ValueError, sqlite3.Error):
                        details = None
                    if details:
                        item = {**item, **details}
                enriched.append(item)
            return {**payload, "results": enriched}

        def _image(self, request_id: str) -> int:
            parsed = urlsplit(self.path)
            prefix = "/mobile/image/"
            object_id = parsed.path[len(prefix):]
            query = parse_qs(parsed.query, keep_blank_values=True)
            variant_values = query.get("variant", ["thumbnail"])
            if (
                not OBJECT_ID_PATTERN.fullmatch(object_id)
                or set(query) - {"variant"}
                or len(variant_values) != 1
                or variant_values[0] not in {"thumbnail", "preview"}
            ):
                self._json(400, {"ok": False, "error": {"type": "invalid_request", "message": "Invalid photo preview request"}}, request_id)
                return 400
            session, _ = self._session()
            if not sessions.verify_session(session):
                self._json(401, {"ok": False, "error": {"type": "unauthorized", "message": "Active mobile session required"}}, request_id)
                return 401
            try:
                asset = previews.asset(object_id, variant_values[0])
            except (FileNotFoundError, ValueError, OSError, sqlite3.Error):
                asset = None
            if asset is None:
                self._json(404, {"ok": False, "error": {"type": "not_found", "message": "Photo preview is unavailable"}}, request_id)
                return 404
            raw = asset.path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", asset.content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Vary", "Cookie")
            self.send_header("Content-Disposition", "inline; filename=human-os-photo.jpg")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Request-ID", request_id)
            self.end_headers()
            self.wfile.write(raw)
            return 200

        def _session_cookies(self, session: str, *, renew_device: bool) -> tuple[str, ...]:
            values = [
                f"{SESSION_COOKIE}={session}; Path=/mobile; Max-Age={config.session_ttl}; Secure; HttpOnly; SameSite=Strict"
            ]
            if renew_device:
                device = trusted_devices.issue(config.trusted_device_ttl)
                values.append(
                    f"{TRUSTED_DEVICE_COOKIE}={device}; Path=/mobile; Max-Age={config.trusted_device_ttl}; Secure; HttpOnly; SameSite=Strict"
                )
            return tuple(values)

        def do_GET(self) -> None:  # noqa: N802
            request_id, started = str(uuid.uuid4()), time.perf_counter()
            parsed_path = urlsplit(self.path).path
            if self.path in ("/mobile", "/mobile/"):
                raw = PAGE.encode("utf-8")
                self.send_response(200)
                self._headers("text/html; charset=utf-8", len(raw))
                self.end_headers(); self.wfile.write(raw)
                status = 200
                endpoint = "mobile_page"
            elif parsed_path.startswith("/mobile/image/"):
                status = self._image(request_id)
                endpoint = "mobile_image"
            else:
                status = 404
                self._json(status, {"ok": False, "error": {"type": "not_found", "message": "Endpoint is not exposed"}}, request_id)
                endpoint = "disallowed"
            audit.write({"timestamp": datetime.now(timezone.utc).isoformat(), "request_id": request_id, "endpoint": endpoint, "status": status, "latency_ms": round((time.perf_counter()-started)*1000, 3)})

        def do_POST(self) -> None:  # noqa: N802
            request_id, started, status = str(uuid.uuid4()), time.perf_counter(), 500
            path = urlsplit(self.path).path
            if not self._https_origin():
                status, payload = 426, {"ok": False, "error": {"type": "https_required", "message": "HTTPS is required"}}
                self._json(status, payload, request_id)
            elif path == "/mobile/session":
                incoming = self._read_json()
                candidate = incoming.get("token", "") if incoming else ""
                if not isinstance(candidate, str) or not hmac.compare_digest(candidate, config.mobile_token):
                    status, payload = 401, {"ok": False, "error": {"type": "unauthorized", "message": "Valid mobile access token required"}}
                    self._json(status, payload, request_id)
                else:
                    session, csrf = sessions.create(config.session_ttl)
                    status = 200
                    self._json(
                        status,
                        {"ok": True, "csrf_token": csrf, "expires_in": config.session_ttl,
                         "trusted_device_expires_in": config.trusted_device_ttl},
                        request_id,
                        cookies=self._session_cookies(session, renew_device=True),
                    )
            elif path == "/mobile/session/refresh":
                incoming = self._read_json()
                valid_request = incoming == {} and self.headers.get("X-Mobile-Refresh") == "1"
                if not valid_request or not trusted_devices.verify(self._trusted_device()):
                    status, payload = 401, {"ok": False, "error": {"type": "unauthorized", "message": "Trusted mobile device authorization is missing or expired"}}
                    self._json(status, payload, request_id)
                else:
                    session, csrf = sessions.create(config.session_ttl)
                    status = 200
                    self._json(
                        status,
                        {"ok": True, "csrf_token": csrf, "expires_in": config.session_ttl,
                         "trusted_device_expires_in": config.trusted_device_ttl},
                        request_id,
                        cookies=self._session_cookies(session, renew_device=True),
                    )
            elif path == "/mobile/search":
                session, csrf = self._session()
                if not sessions.verify(session, csrf):
                    status, payload = 401, {"ok": False, "error": {"type": "unauthorized", "message": "Mobile session is missing or expired"}}
                    self._json(status, payload, request_id)
                else:
                    incoming = self._read_json()
                    if not incoming or set(incoming) - {"query", "limit"} or not isinstance(incoming.get("query"), str):
                        status, payload = 400, {"ok": False, "error": {"type": "invalid_request", "message": "Valid query is required"}}
                        self._json(status, payload, request_id)
                    else:
                        body = json.dumps({"query": incoming["query"], "limit": incoming.get("limit", 10)}, ensure_ascii=False).encode()
                        req = urllib.request.Request(config.tool_url + "/v1/search", data=body, method="POST", headers={"Authorization": "Bearer " + config.tool_token, "Content-Type": "application/json", "X-Forwarded-Proto": "https", "User-Agent": "HumanOS-MobileGateway/1.0"})
                        try:
                            with upstream_opener.open(req, timeout=config.timeout) as response:
                                status, payload = response.status, json.load(response)
                        except urllib.error.HTTPError as exc:
                            status = exc.code
                            try: payload = json.load(exc)
                            except Exception: payload = {"ok": False, "error": {"type": "upstream_error", "message": "Human OS search failed"}}
                        except (urllib.error.URLError, TimeoutError, socket.timeout):
                            status, payload = 503, {"ok": False, "error": {"type": "upstream_unavailable", "message": "Human OS is temporarily unavailable"}}
                        if not isinstance(payload, dict):
                            status, payload = 502, {"ok": False, "error": {"type": "invalid_response", "message": "Human OS returned an invalid response"}}
                        if status == 200:
                            payload = self._enrich_search(payload)
                        self._json(status, payload, request_id)
            else:
                status = 404
                self._json(status, {"ok": False, "error": {"type": "not_found", "message": "Endpoint is not exposed"}}, request_id)
            audit.write({"timestamp": datetime.now(timezone.utc).isoformat(), "request_id": request_id, "endpoint": path if path in {"/mobile/session", "/mobile/session/refresh", "/mobile/search"} else "disallowed", "status": status, "latency_ms": round((time.perf_counter()-started)*1000, 3)})

        def log_message(self, *_args: Any) -> None:
            return

    return Handler


def load_environment(path: Path) -> None:
    if not path.is_file():
        raise ValueError("private mobile environment file does not exist")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid private environment line: {number}")
        key, value = line.split("=", 1)
        if key not in ALLOWED_ENV_KEYS:
            raise ValueError(f"unsupported private environment key: {key}")
        os.environ[key] = value


def config_from_environment() -> MobileConfig:
    audit = os.environ.get("HUMAN_OS_MOBILE_AUDIT_LOG")
    config = MobileConfig(
        mobile_token=os.environ.get("HUMAN_OS_MOBILE_TOKEN", ""),
        tool_token=os.environ.get("HUMAN_OS_TOOL_TOKEN", ""),
        tool_url=os.environ.get("HUMAN_OS_MOBILE_TOOL_URL", "http://127.0.0.1:8899").rstrip("/"),
        host=os.environ.get("HUMAN_OS_MOBILE_HOST", "127.0.0.1"),
        port=int(os.environ.get("HUMAN_OS_MOBILE_PORT", "8990")),
        timeout=float(os.environ.get("HUMAN_OS_MOBILE_TIMEOUT", "20")),
        body_limit=int(os.environ.get("HUMAN_OS_MOBILE_BODY_LIMIT", str(32*1024))),
        session_ttl=int(os.environ.get("HUMAN_OS_MOBILE_SESSION_TTL", "3600")),
        trusted_device_ttl=int(os.environ.get("HUMAN_OS_MOBILE_TRUSTED_DEVICE_TTL", str(30*24*3600))),
        audit_log=Path(audit) if audit else None,
        public_origin=os.environ.get("HUMAN_OS_MOBILE_PUBLIC_ORIGIN", "https://memory.humonosmemory.com"),
        require_forwarded_https=os.environ.get("HUMAN_OS_MOBILE_REQUIRE_HTTPS", "true").casefold() not in {"0", "false", "no"},
    )
    config.validate()
    return config


def load_tool_token(path: Path) -> None:
    if not path.is_file():
        raise ValueError("private tool environment file does not exist")
    prefix = "HUMAN_OS_TOOL_TOKEN="
    values = [line[len(prefix):] for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(prefix)]
    if len(values) != 1:
        raise ValueError("private tool token is missing or duplicated")
    os.environ["HUMAN_OS_TOOL_TOKEN"] = values[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--tool-env-file", type=Path, required=True)
    args = parser.parse_args()
    load_tool_token(args.tool_env_file)
    load_environment(args.env_file)
    config = config_from_environment()
    with ThreadingHTTPServer((config.host, config.port), make_handler(config)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
