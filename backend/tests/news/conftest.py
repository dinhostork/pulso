"""Shared fixtures for the News test suite.

Holds a loopback HTTP server that serves repository fixture bytes.

Used where a test must exercise the real hardened fetcher end to end instead
of a transport double: no `MockTransport`, no internet, and nothing listening
outside 127.0.0.1. `config.settings_test` intentionally permits private
network targets, which is what makes this reachable.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
SHUTDOWN_TIMEOUT_SECONDS = 5


class FixtureHttpServer:
    """Serve one fixture document on an ephemeral loopback port."""

    def __init__(self, fixture_name: str, content_type: str = "application/rss+xml"):
        self.body = (FIXTURES / fixture_name).read_bytes()
        self.content_type = content_type
        self.requests = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "server is not running"
        return f"http://127.0.0.1:{self._server.server_port}/feed"

    def __enter__(self) -> "FixtureHttpServer":
        server = self
        # HTTP/1.1 with an explicit Content-Length: the fetcher's size bound
        # applies to a declared length, exactly as for a real upstream.
        content_type = self.content_type

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                server.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *args):
                """Keep pytest output clean; the test asserts on behavior."""

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
        return None


@pytest.fixture
def fixture_http_server():
    """The loopback fixture-server class, used as a context manager."""

    return FixtureHttpServer
