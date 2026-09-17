"""Repository-owned fixture bytes served over bounded loopback HTTP."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
SHUTDOWN_TIMEOUT_SECONDS = 5


class FixtureHttpServer:
    """Serve a replaceable fixture on an ephemeral 127.0.0.1 port."""

    def __init__(self, fixture_name: str, content_type: str = "application/rss+xml"):
        self.body = (FIXTURES / fixture_name).read_bytes()
        self.content_type = content_type
        self.requests = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_fixture(self, fixture_name: str, content_type: str | None = None) -> None:
        """Replace the response bytes while retaining the endpoint URL."""

        with self._lock:
            self.body = (FIXTURES / fixture_name).read_bytes()
            if content_type is not None:
                self.content_type = content_type

    @property
    def url(self) -> str:
        assert self._server is not None, "server is not running"
        return f"http://127.0.0.1:{self._server.server_port}/feed"

    def __enter__(self) -> "FixtureHttpServer":
        fixture_server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                with fixture_server._lock:
                    fixture_server.requests += 1
                    body = fixture_server.body
                    content_type = fixture_server.content_type
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                """Keep pytest output clean; tests assert request counts."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            assert not self._thread.is_alive(), "fixture server did not stop"
        return None
