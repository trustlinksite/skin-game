"""Pure-Python WSGI & standalone HTTP server for Skin Defenders + SQLite Leaderboard."""

from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from game_data import get_client_config
from leaderboard import get_top_scores, init_db, submit_score

TEMPLATE_CANDIDATES = [
    os.path.join(BASE_DIR, "templates", "index.html"),
    os.path.join(BASE_DIR, "index.html"),
]
init_db()


def _render_game_html() -> bytes | None:
    template_file = next((p for p in TEMPLATE_CANDIDATES if os.path.isfile(p)), None)
    if template_file is None:
        return None
    with open(template_file, "r", encoding="utf-8") as f:
        html = f.read()
    cfg_json = json.dumps(get_client_config(), ensure_ascii=False)
    return html.replace("/*__PY_CONFIG_HOOK__*/null", cfg_json).encode("utf-8")


NO_CACHE_HEADERS = [
    ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
]


def _cors_headers() -> list[tuple[str, str]]:
    return [
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
    ]


def handle_request(method: str, path: str, body: bytes) -> tuple[int, str, list[tuple[str, str]], bytes]:
    clean_path = path.split("?", 1)[0]

    if method == "OPTIONS":
        return 204, "text/plain; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), b""

    if clean_path in ("/", "/index.html") and method == "GET":
        payload = _render_game_html()
        if payload is None:
            msg = (
                "<!doctype html><html lang='fa' dir='rtl'><meta charset='utf-8'>"
                "<body style='font-family:tahoma,sans-serif;background:#2b192e;color:#ffe5ec;padding:24px'>"
                "<h3>سرور پایتون لیدربرد فعال است ✅</h3>"
                "<p>فایل <code>templates/index.html</code> یا <code>index.html</code> کنار <code>app.py</code> پیدا نشد، "
                "اما API لیدربرد آماده است.</p></body></html>"
            ).encode("utf-8")
            return 200, "text/html; charset=utf-8", NO_CACHE_HEADERS, msg
        return 200, "text/html; charset=utf-8", NO_CACHE_HEADERS, payload

    if clean_path == "/api/health" and method == "GET":
        payload = json.dumps({"ok": True, "engine": "python"}, ensure_ascii=False).encode("utf-8")
        return 200, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), payload

    if clean_path == "/api/config" and method == "GET":
        payload = json.dumps({"ok": True, "config": get_client_config()}, ensure_ascii=False).encode("utf-8")
        return 200, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), payload

    if clean_path == "/api/leaderboard" and method == "GET":
        top = get_top_scores(10)
        payload = json.dumps({"ok": True, "top": top}, ensure_ascii=False).encode("utf-8")
        return 200, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), payload

    if clean_path == "/api/score" and method == "POST":
        try:
            data = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("invalid json object")
        except Exception:
            err = json.dumps({"ok": False, "error": "bad_json"}, ensure_ascii=False).encode("utf-8")
            return 400, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), err

        status, result = submit_score(data)
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        return status, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), payload

    not_found = json.dumps({"ok": False, "error": "not_found"}, ensure_ascii=False).encode("utf-8")
    return 404, "application/json; charset=utf-8", NO_CACHE_HEADERS + _cors_headers(), not_found


def application(environ: dict, start_response: Callable) -> Iterable[bytes]:
    """Standard WSGI callable (cPanel Passenger / Gunicorn / uWSGI compatible)."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        content_length = 0
    content_length = max(0, min(content_length, 65536))
    body = environ["wsgi.input"].read(content_length) if content_length > 0 else b""

    status_code, content_type, extra_headers, payload = handle_request(method, path, body)
    reason = HTTPStatus(status_code).phrase
    headers = [("Content-Type", content_type), ("Content-Length", str(len(payload)))] + extra_headers
    start_response(f"{status_code} {reason}", headers)
    return [payload]


class _GameHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self, head_only: bool = False) -> None:
        try:
            length = 0
            if "Content-Length" in self.headers:
                try:
                    length = max(0, min(int(self.headers["Content-Length"]), 65536))
                except ValueError:
                    length = 0
            body = self.rfile.read(length) if length > 0 else b""
            method = "GET" if head_only else self.command
            status, ctype, extra_headers, payload = handle_request(method, self.path, body)
        except Exception as exc:
            status = 500
            ctype = "application/json; charset=utf-8"
            extra_headers = NO_CACHE_HEADERS + _cors_headers()
            payload = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        if payload and not head_only:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._dispatch(False)

    def do_HEAD(self) -> None:
        self._dispatch(True)

    def do_POST(self) -> None:
        self._dispatch(False)

    def do_OPTIONS(self) -> None:
        self._dispatch(False)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), _GameHandler)
    print(f"Skin Defenders Python Server listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
