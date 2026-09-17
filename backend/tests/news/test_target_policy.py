"""Offline tests for literal and resolved outbound target refusal."""

import socket

import pytest

from news.adapters.targets import assert_allowed_target
from news.application.ports import FetchError, FetchErrorKind


def _resolver(*addresses):
    return lambda _host, _port: addresses


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_dns_answers_are_allowed(address):
    assert_allowed_target("https://feed.example/x", resolver=_resolver(address))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
    ],
)
def test_non_global_dns_answers_are_blocked(address):
    with pytest.raises(FetchError) as error:
        assert_allowed_target("https://feed.example/x", resolver=_resolver(address))
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET
    assert not error.value.retryable


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://[::1]/x", "http://100.64.0.1/x"])
def test_private_literal_ip_is_blocked_without_dns(url):
    def no_dns(*_):
        raise AssertionError("literal IP must not call DNS")

    with pytest.raises(FetchError) as error:
        assert_allowed_target(url, resolver=no_dns)
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET


def test_mixed_public_and_private_dns_answer_is_blocked():
    with pytest.raises(FetchError) as error:
        assert_allowed_target("https://feed.example/x", resolver=_resolver("8.8.8.8", "10.0.0.5"))
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET


def test_private_override_allows_address_only():
    assert_allowed_target("https://feed.example/x", allow_private=True, resolver=_resolver("::1"))
    assert_allowed_target("http://127.0.0.1/x", allow_private=True)
    with pytest.raises(FetchError) as error:
        assert_allowed_target("ftp://127.0.0.1/x", allow_private=True)
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET


def test_dns_failure_is_retryable_network_error():
    def fail(_host, _port):
        raise socket.gaierror("sensitive-host-name")

    with pytest.raises(FetchError) as error:
        assert_allowed_target("https://feed.example/x", resolver=fail)
    assert error.value.kind == FetchErrorKind.NETWORK
    assert error.value.retryable
    assert "sensitive-host-name" not in error.value.message


def test_empty_dns_answer_is_retryable_network_error():
    with pytest.raises(FetchError) as error:
        assert_allowed_target("https://feed.example/x", resolver=_resolver())
    assert error.value.kind == FetchErrorKind.NETWORK


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "https://example.com:bad/x", "https://x/y\nz"]
)
def test_invalid_targets_fail_without_dns(url):
    def no_dns(*_):
        raise AssertionError("invalid URL must not call DNS")

    with pytest.raises(FetchError) as error:
        assert_allowed_target(url, resolver=no_dns, allow_private=True)
    assert error.value.kind == FetchErrorKind.BLOCKED_TARGET
