"""Synthetic loopback Ollama endpoint for provider tests.

Standard library only. The server is a real HTTP server on ``127.0.0.1``
so the provider's real request/response handling is exercised end to
end, but requests are sent through a no-proxy opener: a machine-level
HTTP proxy must never route loopback traffic in a test, because that
would make the result depend on unrelated machine configuration rather
than on the provider contract.
"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCAL_HOST = "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        delay = float(getattr(self.server, "delay", 0.0) or 0.0)
        if delay:
            time.sleep(delay)
        body = getattr(self.server, "body", b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def log_message(self, format, *args):  # type: ignore[override]
        pass


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    body = b"{}"
    delay = 0.0


class FakeOllamaServer:
    """Serve one canned JSON body over a real loopback HTTP endpoint."""

    def __init__(self, body, delay: float = 0.0) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._server = _Server((_LOCAL_HOST, 0), _Handler)
        self._server.body = body
        self._server.delay = float(delay)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> str:
        return "http://%s:%d" % (_LOCAL_HOST, self.port)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False


def json_body(**kwargs) -> str:
    """Return a one-candidate model response body for the fake server."""
    return json.dumps({"response": json.dumps({"candidates": [kwargs]})})


def install_no_proxy_opener(monkeypatch) -> None:
    """Route ``urllib.request.urlopen`` around any configured proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)


__all__ = ("FakeOllamaServer", "install_no_proxy_opener", "json_body")
