"""Loopback HTTP transport for the launcher.

Security contract:
- binds exclusively to 127.0.0.1 (no external interface);
- per-launch token required (header ``X-LAE-Token``) on every route
  except ``/api/health`` and the launcher's own static UI files; the
  token is never placed in a URL, log, or persistent storage;
- Host and Origin are restricted to loopback origin (DNS-rebinding and
  cross-origin request rejection);
- methods, content types, body sizes, and payload schemas are validated
  before any backend operation;
- errors are typed and bounded (no stack traces).
"""
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import (
    AdapterError,
    Launcher,
    MAX_BODY_BYTES,
    TOKEN_HEADER,
    VERSION,
    _utcnow,
)

ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_JSON_BODY = MAX_BODY_BYTES

_BOOTSTRAP_MARK = "</head>"
_BOOTSTRAP_SCRIPT = (
    "<script>window.__LAE_BOOTSTRAP__={token:%s,instanceId:%s,"
    "version:%s};</script>")


def _split_path_query(target: str):
    """Split ``/path?query`` without urllib (narrow import boundary)."""
    if not isinstance(target, str):
        return ("", "")
    raw = target.split("?", 1)
    path = raw[0]
    query = raw[1] if len(raw) > 1 else ""
    return (path, query)


def _query_value(query: str, name: str, default: str = "") -> str:
    if not isinstance(query, str):
        return default
    for pair in query.split("&"):
        if "=" in pair:
            key, _, value = pair.partition("=")
            if key == name:
                return value
    return default


def _make_handler(launcher: Launcher):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "LAE-Launcher/0.1"
        protocol_version = "HTTP/1.1"

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass

        def log_message(self, format, *args):  # never log tokens/requests
            return

        def _send(self, code, body, content_type="application/json",
                  extra_headers=None):
            payload = body if isinstance(body, bytes) else \
                body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra_headers or []):
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _error(self, code, kind, message):
            self._send_json(code, {"error": {"kind": kind,
                                             "message": message}})

        def _origin_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host and host not in ALLOWED_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin:
                # "scheme://host[:port]/..." -> host, without urllib.
                remainder = origin.split("://", 1)
                after = remainder[1] if len(remainder) > 1 else origin
                origin_host = after.split("/", 1)[0].split(":")[0].lower()
                if origin_host and origin_host not in ALLOWED_HOSTS:
                    return False
            return True

        def _authenticated(self):
            token = self.headers.get(TOKEN_HEADER)
            if not token:
                self._error(401, "authentication_error",
                            "missing authentication token")
                return False
            if not launcher.check_token(token):
                self._error(403, "authentication_error",
                            "invalid authentication token")
                return False
            return True

        def _read_json(self):
            length = self.headers.get("Content-Length")
            if length is None:
                self._error(411, "validation_error",
                            "content length required")
                return None
            try:
                size = int(length)
            except (TypeError, ValueError):
                self._error(400, "validation_error",
                            "invalid content length")
                return None
            if size <= 0 or size > MAX_JSON_BODY:
                self._error(413, "validation_error", "request too large")
                return None
            content_type = self.headers.get("Content-Type") or ""
            if "application/json" not in content_type.lower():
                self._error(415, "validation_error",
                            "content type must be application/json")
                return None
            try:
                raw = self.rfile.read(size)
            except OSError:
                self._error(400, "validation_error", "unreadable body")
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._error(400, "validation_error",
                            "body is not valid JSON")
                return None
            if not isinstance(data, dict):
                self._error(400, "validation_error",
                            "body must be a JSON object")
                return None
            return data

        # -- GET ---------------------------------------------------------

        def do_GET(self):
            if not self._origin_ok():
                self._error(403, "authentication_error",
                            "request origin rejected")
                return
            path, _query = _split_path_query(self.path)
            if path == "/api/health":
                self._send_json(200, launcher.health())
                return
            if path == "/api/journals":
                if not self._authenticated():
                    return
                self._send_json(200, launcher.journals())
                return
            if path == "/api/state":
                if not self._authenticated():
                    return
                self._send_json(200, launcher.state())
                return
            if path == "/api/events":
                if not self._authenticated():
                    return
                self._serve_events()
                return
            if path == "/api/vault/sync/status":
                if not self._authenticated():
                    return
                self._handle_vault_sync_status()
                return
            if path.startswith("/api/"):
                if not self._authenticated():
                    return
                self._error(404, "validation_error", "unknown route")
                return
            self._serve_static(path)

        def _serve_static(self, path):
            web_root = launcher._web_root
            if not web_root:
                self._send(200, _placeholder_page(launcher),
                           "text/html; charset=utf-8")
                return
            root = Path(web_root).resolve()
            if path in ("", "/"):
                path = "/index.html"
            candidate = (root / path.lstrip("/")).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self._error(403, "validation_error",
                            "path outside web root")
                return
            if not candidate.is_file():
                self._error(404, "validation_error", "not found")
                return
            if candidate.name == "index.html":
                try:
                    html = candidate.read_text(encoding="utf-8")
                except OSError:
                    self._error(500, "backend_error",
                                "page unavailable")
                    return
                bootstrap = _BOOTSTRAP_SCRIPT % (
                    json.dumps(launcher.token),
                    json.dumps(launcher.instance_id),
                    json.dumps(VERSION))
                html = html.replace(_BOOTSTRAP_MARK,
                                    bootstrap + _BOOTSTRAP_MARK, 1)
                self._send(200, html, "text/html; charset=utf-8")
                return
            content_type = _content_type(candidate)
            try:
                body = candidate.read_bytes()
            except OSError:
                self._error(500, "backend_error", "asset unavailable")
                return
            self._send(200, body, content_type)

        def _serve_events(self):
            _path, query = _split_path_query(self.path)
            after = 0
            try:
                after = int(_query_value(query, "after", "0"))
            except (TypeError, ValueError):
                after = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    batch = launcher.events.wait_since(after, 20.0)
                    if not batch:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for event in batch:
                        after = event["seq"]
                        self.wfile.write((
                            "id: %d\nevent: message\ndata: %s\n\n"
                            % (event["seq"],
                               json.dumps(event, ensure_ascii=False))
                        ).encode("utf-8"))
                    self.wfile.flush()
            except (OSError, ValueError):
                pass

        # -- POST --------------------------------------------------------

        def do_POST(self):
            if not self._origin_ok():
                self._error(403, "authentication_error",
                            "request origin rejected")
                return
            path, _query = _split_path_query(self.path)
            if not self._authenticated():
                return
            if path == "/api/folders/pick":
                self._handle_pick()
                return
            if path == "/api/run":
                self._handle_run()
                return
            if path == "/api/approve":
                self._handle_approve()
                return
            if path == "/api/proposal/edit":
                self._handle_edit()
                return
            if path == "/api/cancel":
                self._handle_cancel()
                return
            if path == "/api/consent":
                self._handle_consent()
                return
            if path == "/api/config":
                self._handle_config()
                return
            if path == "/api/translate":
                self._handle_translate()
                return
            if path == "/api/folders/reveal":
                self._handle_reveal()
                return
            if path == "/api/vault/index":
                self._handle_index_vault()
                return
            if path == "/api/vault/sync/authorize":
                self._handle_vault_sync_authorize()
                return
            if path == "/api/vault/sync/revoke":
                self._handle_vault_sync_revoke()
                return
            if path == "/api/vault/sync/reconcile":
                self._handle_vault_sync_reconcile()
                return
            if path == "/api/vault/sync/decide":
                self._handle_vault_sync_decide()
                return
            if path == "/api/run/accept":
                self._handle_accept()
                return
            if path == "/api/run/fix":
                self._handle_fix()
                return
            if path == "/api/run/stop":
                self._handle_stop()
                return
            self._error(404, "validation_error", "unknown route")

        def _handle_pick(self):
            data = self._read_json()
            if data is None:
                return
            purpose = data.get("purpose")
            try:
                status, display, reason = launcher.pick_folder(purpose)
            except ValueError:
                self._error(422, "validation_error",
                            "purpose must be a known folder purpose")
                return
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, {
                "purpose": purpose, "status": status,
                "display": display, "reason": reason,
                "validated_at": _utcnow()})

        def _handle_run(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.start_run(
                    data.get("input_text", ""),
                    data.get("source_mode") or "personal_vault",
                    retrieval_mode=data.get("retrieval_mode") or "lexical",
                    app_language=data.get("app_language") or "en",
                    staging_language=data.get("staging_language") or "en",
                    audit_language=data.get("audit_language") or "en",
                    source_type=data.get("source_type", "chat_input"),
                    input_id=data.get("input_id"),
                )
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(202, result)

        def _handle_approve(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.approve(
                    data.get("proposal_hash"),
                    data.get("query_unit_id"),
                    data.get("decision"))
            except AdapterError as exc:
                status_code = 409 if (exc.kind == "conflict"
                                      or "pending" in exc.message
                                      or "already" in exc.message
                                      or "cancelled" in exc.message) else 400
                self._error(status_code, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_consent(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.consent(
                    data.get("consent_id"), data.get("decision"))
            except AdapterError as exc:
                self._error(409, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_cancel(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.request_cancel(data.get("run_id"))
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_edit(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.edit_proposal(
                    title=data.get("title"),
                    body=data.get("body"),
                    reason=data.get("reason"),
                    client_hash=data.get("proposal_hash"))
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_config(self):
            data = self._read_json()
            if data is None:
                return
            try:
                result = launcher.update_config(data)
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_translate(self):
            data = self._read_json()
            if data is None:
                return
            text = data.get("text", "")
            target_lang = data.get("target_lang", "en")
            source_lang = data.get("source_lang", "")
            source_kind = data.get("source_kind", "dynamic_text")
            source_id = data.get("source_id", "")
            try:
                result = launcher.translate_text(
                    text,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    source_kind=source_kind,
                    source_id=source_id,
                )
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_reveal(self):
            data = self._read_json()
            if data is None:
                return
            purpose = data.get("purpose", "")
            try:
                result = launcher.reveal_folder(purpose)
            except Exception as exc:
                self._error(400, "validation_error", str(exc))
                return
            self._send_json(200, result)

        def _handle_index_vault(self):
            try:
                result = launcher.index_vault()
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_vault_sync_status(self):
            status = launcher.get_vault_sync_status()
            self._send_json(200, status)

        def _handle_vault_sync_authorize(self):
            data = self._read_json()
            if data is None:
                data = {}
            vault_root = data.get("vault_root")
            try:
                result = launcher.authorize_vault_sync(vault_root=vault_root)
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_vault_sync_revoke(self):
            result = launcher.revoke_vault_sync()
            self._send_json(200, result)

        def _handle_vault_sync_reconcile(self):
            try:
                result = launcher.reconcile_vault_sync()
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_vault_sync_decide(self):
            data = self._read_json()
            if data is None:
                return
            run_id = data.get("run_id")
            vault_id = data.get("vault_id")
            decision = data.get("decision")
            if not isinstance(run_id, str) or not run_id.strip():
                self._error(400, "validation_error", "run_id is required")
                return
            if not isinstance(vault_id, str) or not vault_id.strip():
                self._error(400, "validation_error", "vault_id is required")
                return
            if decision not in ("approve", "reject"):
                self._error(400, "validation_error", "decision must be 'approve' or 'reject'")
                return
            try:
                result = launcher.decide_vault_sync(run_id, vault_id, decision)
            except AdapterError as exc:
                if exc.kind == "not_found":
                    code = 404
                elif exc.kind == "conflict":
                    code = 409
                elif exc.kind == "unprocessable_entity":
                    code = 422
                else:
                    code = 400
                self._error(code, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_accept(self):
            data = self._read_json()
            if data is None:
                data = {}
            run_id = data.get("run_id", "")
            proposal_hash = data.get("proposal_hash", "")
            try:
                result = launcher.accept_proposal(run_id=run_id, proposal_hash=proposal_hash)
            except AdapterError as exc:
                status_code = 409 if (exc.kind == "conflict"
                                      or "already" in exc.message
                                      or "cancelled" in exc.message
                                      or "timed out" in exc.message) else 400
                self._error(status_code, exc.kind, exc.message)
                return
            self._send_json(200, result)

        def _handle_fix(self):
            data = self._read_json()
            if data is None:
                return
            try:
                run_id = str(data.get("run_id") or "")
                raw_input = data.get("input_text")
                raw_source = data.get("source_mode")
                raw_retrieval = data.get("retrieval_mode")
                result = launcher.fix_paused_run(
                    run_id=run_id,
                    input_text=str(raw_input) if raw_input is not None else None,
                    source_mode=str(raw_source) if raw_source is not None else None,
                    retrieval_mode=str(raw_retrieval) if raw_retrieval is not None else None,
                )
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(202, result)

        def _handle_stop(self):
            data = self._read_json()
            if data is None:
                data = {}
            try:
                result = launcher.stop_paused_run(run_id=data.get("run_id", ""))
            except AdapterError as exc:
                self._error(400, exc.kind, exc.message)
                return
            self._send_json(200, result)

    return _Handler



class LauncherServer:
    """Loopback HTTP server wrapping a Launcher application core."""

    def __init__(self, launcher: Launcher, port: int = 0,
                 web_root=None) -> None:
        self.launcher = launcher
        if web_root is not None:
            launcher._web_root = web_root
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", port), _make_handler(launcher))
        self._httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:" + str(self.port) + "/"

    def serve_in_thread(self):
        worker = threading.Thread(target=self._httpd.serve_forever,
                                  daemon=True)
        worker.start()
        return worker

    def serve(self, open_browser: bool = True):
        worker = self.serve_in_thread()
        if open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass
        return worker

    def shutdown(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }.get(suffix, "application/octet-stream")


def _placeholder_page(launcher: Launcher) -> str:
    bootstrap = _BOOTSTRAP_SCRIPT % (
        json.dumps(launcher.token),
        json.dumps(launcher.instance_id),
        json.dumps(VERSION))
    return ("<!doctype html><html lang=\"en\"><head><meta "
            "charset=\"utf-8\"><title>LA-E</title>"
            + bootstrap
            + "</head><body><p>Build the frontend (npm run build) to "
              "load the application.</p></body></html>")


def run_launcher(web_root=None, open_browser: bool = True,
                 port: int = 0, **launcher_kwargs):
    """Start the launcher and serve until interrupted."""
    launcher = Launcher(web_root=web_root, **launcher_kwargs)
    server = LauncherServer(launcher, port=port, web_root=web_root)
    server.serve(open_browser=open_browser)
    print(f"LivingAuthenticity Environment launcher listening on {server.url} (loopback only)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return server


__all__ = ["LauncherServer", "run_launcher", "_make_handler"]
