from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "FloodGateBatch/1.0"

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

        @staticmethod
        def _path_id(path: str, suffix: str = ""):
            if suffix and path.endswith(suffix):
                path = path[: -len(suffix)]
            return int(path.rstrip("/").rsplit("/", 1)[-1])

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                del actor
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/gates":
                    self._json(200, {"gates": service.list_gates(role)})
                elif path == "/api/orders":
                    self._json(200, {"orders": service.list_orders(role)})
                elif path.startswith("/api/orders/") and path.endswith("/records"):
                    order_id = self._path_id(path, "/records")
                    self._json(200, {"records": service.list_records(order_id, role)})
                elif path.startswith("/api/orders/"):
                    self._json(200, service.get_order(self._path_id(path), role))
                elif path == "/api/audit":
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/gates":
                    self._json(201, service.register_gate(body, actor, role))
                elif path == "/api/gates/observations":
                    self._json(201, service.submit_observation(body, actor, role))
                elif path == "/api/gates/status":
                    self._json(200, service.update_gate_status(body, actor, role))
                elif path == "/api/capacity":
                    self._json(200, service.capacity_view(body, role))
                elif path == "/api/orders":
                    self._json(201, service.submit_order(body, actor, role))
                elif path.startswith("/api/orders/") and path.endswith("/authorize"):
                    order_id = self._path_id(path, "/authorize")
                    self._json(200, service.authorize(order_id, body, actor, role))
                elif path.startswith("/api/orders/") and path.endswith("/execute"):
                    order_id = self._path_id(path, "/execute")
                    self._json(201, service.execute(order_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
