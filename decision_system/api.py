"""Minimal REST API on the standard library http.server.

POST /requests                 {"workflow": "...", "data": {...}, "idempotency_key": "..."}
GET  /requests/<id>            current state
GET  /requests/<id>/history    state change history
GET  /requests/<id>/audit      full audit trail
GET  /requests/<id>/explanation  human readable decision explanation
POST /requests/<id>/retry      resume a RETRY_PENDING request
POST /requests/<id>/review     {"reviewer": "...", "decision": "approve|reject", "reason": "..."}
GET  /workflows                configured workflows
POST /admin/reload             re-read workflow configs
GET  /health
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import IdempotencyConflict, InvalidOperation, NotFound
from .schema import ValidationError
from .store import StoreError
from .workflow import ConfigError


class HttpError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep test/demo output quiet

        # -- plumbing -----------------------------------------------------

        def _send(self, status, body, headers=None):
            raw = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise HttpError(400, "body is not valid JSON")
            if not isinstance(body, dict):
                raise HttpError(400, "body must be a JSON object")
            return body

        def _dispatch(self, method):
            try:
                self._route(method)
            except HttpError as exc:
                self._send(exc.status, exc.body)
            except ValidationError as exc:
                self._send(422, {"error": "validation failed", "details": exc.errors})
            except IdempotencyConflict as exc:
                self._send(409, {"error": str(exc)})
            except NotFound as exc:
                self._send(404, {"error": f"request {exc} not found"})
            except InvalidOperation as exc:
                self._send(409, {"error": str(exc)})
            except ConfigError as exc:
                self._send(400, {"error": str(exc)})
            except StoreError as exc:
                # nothing was half-saved; the client can resend with the same key
                self._send(503, {"error": "storage failure, safe to retry", "retryable": True})
            except Exception as exc:  # pragma: no cover
                self._send(500, {"error": "internal error", "detail": str(exc)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # -- routes -------------------------------------------------------

        def _route(self, method):
            path = self.path.split("?")[0].rstrip("/") or "/"

            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok"})
            if method == "GET" and path == "/workflows":
                return self._send(200, {"workflows": engine.registry.names()})
            if method == "POST" and path == "/admin/reload":
                return self._send(200, {"loaded": engine.registry.reload()})

            if method == "POST" and path == "/requests":
                body = self._json_body()
                if "workflow" not in body or "data" not in body:
                    raise HttpError(400, "body needs 'workflow' and 'data'")
                key = self.headers.get("Idempotency-Key") or body.get("idempotency_key")
                result = engine.submit(body["workflow"], body["data"], key)
                status = 200 if result.replayed else 201
                return self._send(status, result.record,
                                  {"Idempotent-Replay": "true"} if result.replayed else None)

            m = re.fullmatch(r"/requests/([A-Za-z0-9]+)(?:/(\w+))?", path)
            if not m:
                raise HttpError(404, "no such route")
            request_id, action = m.groups()

            if method == "GET" and action is None:
                return self._send(200, engine.get(request_id))
            if method == "GET" and action == "history":
                return self._send(200, {"history": engine.history(request_id)})
            if method == "GET" and action == "audit":
                return self._send(200, {"audit": engine.audit(request_id)})
            if method == "GET" and action == "explanation":
                return self._send(200, engine.explain(request_id))
            if method == "POST" and action == "retry":
                return self._send(200, engine.retry(request_id))
            if method == "POST" and action == "review":
                body = self._json_body()
                return self._send(200, engine.review(
                    request_id, body.get("reviewer"), body.get("decision"), body.get("reason")))

            raise HttpError(404, "no such route")

    return Handler


def serve(engine, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    return server
