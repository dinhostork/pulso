"""Shared fixtures for the News test suite.

Holds a loopback HTTP server that serves repository fixture bytes.

Used where a test must exercise the real hardened fetcher end to end instead
of a transport double: no `MockTransport`, no internet, and nothing listening
outside 127.0.0.1. `config.settings_test` intentionally permits private
network targets, which is what makes this reachable.
"""

import io
import json
import logging
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


@pytest.fixture
def pulso_caplog(caplog):
    """Capture `pulso` records even though that logger does not propagate.

    Production keeps `propagate=False` (issue #20) so structured records are
    not duplicated into the root handler as plain text. pytest's caplog
    handler lives on the root logger, so it must be attached to the `pulso`
    logger explicitly for the duration of a test that inspects records.
    """

    logger = logging.getLogger("pulso")
    previous_level = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)


class CapturedPulsoLog:
    """Records and their rendering through the configured JSON formatter."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        self.stream = io.StringIO()

    @property
    def lines(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line]

    @property
    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    def entry(self, message: str) -> dict:
        return next(entry for entry in self.entries if entry["message"] == message)

    def entries_for(self, message: str) -> list[dict]:
        return [entry for entry in self.entries if entry["message"] == message]

    def text(self) -> str:
        """Everything an operator would see, plus every captured record value."""

        return "\n".join(
            self.lines
            + [f"{key}={value!s}" for record in self.records for key, value in vars(record).items()]
        )


def configured_pulso_formatter() -> logging.Formatter:
    """The formatter Django actually installed for the `pulso` tree."""

    for handler in logging.getLogger("pulso").handlers:
        if handler.formatter is not None:
            return handler.formatter
    raise AssertionError("the pulso logger has no formatting handler configured")


@pytest.fixture
def pulso_formatter():
    """The formatter Django installed for the `pulso` tree."""

    return configured_pulso_formatter()


@pytest.fixture
def pulso_json_log():
    """Capture `pulso` records and their real formatted JSON output.

    The `pulso` logger does not propagate (issue #20), so handlers are attached
    to it directly for the duration of the test.
    """

    logger = logging.getLogger("pulso")
    captured = CapturedPulsoLog()
    rendering = logging.StreamHandler(captured.stream)
    rendering.setFormatter(configured_pulso_formatter())

    class Recorder(logging.Handler):
        def emit(self, record):
            captured.records.append(record)

    recorder = Recorder()
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(rendering)
    logger.addHandler(recorder)
    try:
        yield captured
    finally:
        logger.removeHandler(rendering)
        logger.removeHandler(recorder)
        logger.setLevel(previous_level)
