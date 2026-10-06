from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     QuotaConflict, ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BridgeRestriction/1.0"

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
            if isinstance(exc, QuotaConflict):
                status = 409
                payload = exc.to_dict()
            elif isinstance(exc, ValidationError):
                status = 422
                payload = {"error": exc.__class__.__name__, "message": str(exc)}
            elif isinstance(exc, NotFoundError):
                status = 404
                payload = {"error": exc.__class__.__name__, "message": str(exc)}
            elif isinstance(exc, PermissionDenied):
                status = 403
                payload = {"error": exc.__class__.__name__, "message": str(exc)}
            elif isinstance(exc, ConflictError):
                status = 409
                payload = {"error": exc.__class__.__name__, "message": str(exc)}
            elif isinstance(exc, ValueError):
                status = 422
                payload = {"error": "ValueError", "message": str(exc)}
            elif isinstance(exc, DomainError):
                status = 400
                payload = {"error": exc.__class__.__name__, "message": str(exc)}
            else:
                status = 500
                payload = {"error": "InternalError", "message": str(exc)}
            self._json(status, payload)

        def _segments(self, path: str):
            return [s for s in path.split("/") if s]

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                segs = self._segments(path)
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif segs == ["api", "bridges"]:
                    _, role = self._identity()
                    self._json(200, {"bridges": service.list_bridges(role)})
                elif len(segs) == 3 and segs[:2] == ["api", "bridges"]:
                    _, role = self._identity()
                    self._json(200, service.get_bridge(int(segs[2]), role))
                elif len(segs) == 4 and segs[:2] == ["api", "bridges"] \
                        and segs[3] == "context":
                    _, role = self._identity()
                    self._json(200, {"records": service.list_context(int(segs[2]), role)})
                elif segs == ["api", "notices"]:
                    _, role = self._identity()
                    bridge_id = query.get("bridge_id", [None])[0]
                    status = query.get("status", [None])[0]
                    self._json(200, {"notices": service.list_notices(
                        role, int(bridge_id) if bridge_id else None, status)})
                elif len(segs) == 3 and segs[:2] == ["api", "notices"]:
                    _, role = self._identity()
                    self._json(200, service.get_notice(int(segs[2]), role))
                elif len(segs) == 4 and segs[:2] == ["api", "notices"] \
                        and segs[3] == "diversion":
                    _, role = self._identity()
                    self._json(200, service.get_diversion(int(segs[2]), role))
                elif path == "/api/audit":
                    _, role = self._identity()
                    notice_id = query.get("notice_id", [None])[0]
                    self._json(200, {"events": service.audit(
                        role, int(notice_id) if notice_id else None)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                segs = self._segments(path)
                actor, role = self._identity()
                body = self._body()
                if segs == ["api", "bridges"]:
                    self._json(201, service.register_bridge(body, actor, role))
                elif len(segs) == 4 and segs[:2] == ["api", "bridges"] \
                        and segs[3] == "context":
                    self._json(201, service.add_context(int(segs[2]), body, actor, role))
                elif segs == ["api", "notices"]:
                    self._json(201, service.submit_notice(body, actor, role))
                elif len(segs) == 4 and segs[:2] == ["api", "notices"]:
                    notice_id = int(segs[2])
                    action = segs[3]
                    if action == "engineer-release":
                        self._json(200, service.engineer_release(
                            notice_id, body, actor, role))
                    elif action == "supervisor-release":
                        self._json(200, service.supervisor_release(
                            notice_id, body, actor, role))
                    elif action == "emergency-release":
                        self._json(200, service.emergency_release(
                            notice_id, body, actor, role))
                    elif action == "review":
                        self._json(200, service.review_emergency(
                            notice_id, body, actor, role))
                    elif action == "restore":
                        self._json(200, service.restore(
                            notice_id, body, actor, role))
                    elif action == "offload":
                        self._json(200, service.offload(
                            notice_id, body, actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
