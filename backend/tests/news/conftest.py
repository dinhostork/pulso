"""Shared News pytest fixtures, including the loopback HTTP server factory.

The server implementation lives in ``tests.news.fixture_server``. It serves
repository bytes without internet access; ``config.settings_test`` permits
private targets only so the hardened fetcher can reach this local fixture.
"""

import io
import json
import logging

import pytest

from tests.news.fixture_server import FixtureHttpServer


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
