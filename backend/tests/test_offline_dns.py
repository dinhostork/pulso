import socket

import pytest
from django.conf import settings

from config.environment import boolean


@pytest.mark.parametrize("host", ["example.com", "feed.example"])
def test_public_dns_is_disabled(host):
    with pytest.raises(socket.gaierror, match="non-loopback DNS disabled"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_dns_is_available(host):
    assert socket.getaddrinfo(host, 80)


def test_private_fetch_setting_is_only_enabled_for_test_fixtures():
    assert settings.NEWS_FETCH_ALLOW_PRIVATE_NETWORKS is True


def test_development_private_fetch_default_is_false(monkeypatch):
    monkeypatch.delenv("NEWS_FETCH_ALLOW_PRIVATE_NETWORKS", raising=False)
    assert boolean("NEWS_FETCH_ALLOW_PRIVATE_NETWORKS") is False
