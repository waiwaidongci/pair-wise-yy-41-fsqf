from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    self._json(200, {"events": service.audit(role)})
                elif path.startswith("/api/bridges/") and path.endswith("/notices"):
                    bridge_id = int(path.split("/")[3])
                    self._json(200, {"notices": service.list_notices(bridge_id, role)})
                elif path.startswith("/api/bridges/") and path.endswith("/quota"):
                    bridge_id = int(path.split("/")[3])
                    self._json(200, service.quota_status(bridge_id, role))
                elif path.startswith("/api/bridges/") and path.endswith("/detours"):
                    bridge_id = int(path.split("/")[3])
                    self._json(200, {"detours": service.list_detour_routes(bridge_id, role)})
                elif path.startswith("/api/notices/"):
                    notice_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, service.get_notice(notice_id, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path.startswith("/api/bridges/") and path.endswith("/notices"):
                    bridge_id = int(path.split("/")[3])
                    self._json(201, service.create_restriction(bridge_id, body, actor, role))
                elif path.startswith("/api/bridges/") and path.endswith("/detours"):
                    bridge_id = int(path.split("/")[3])
                    self._json(201, service.add_detour_route(bridge_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/approve"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.approve_notice(notice_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/publish"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.publish_notice(notice_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/emergency-publish"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.emergency_publish(notice_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/review"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.review_emergency(notice_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/restore"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.restore_notice(notice_id, body, actor, role))
                elif path.startswith("/api/notices/") and path.endswith("/recover"):
                    notice_id = int(path.split("/")[3])
                    self._json(200, service.recover_notice(notice_id, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
