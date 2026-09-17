"""Keep the default test suite independent of public DNS."""

import socket

import pytest

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(scope="session", autouse=True)
def local_dns_only():
    original = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            raise socket.gaierror(socket.EAI_NONAME, "non-loopback DNS disabled in tests")
        return original(host, *args, **kwargs)

    socket.getaddrinfo = guarded
    try:
        yield
    finally:
        socket.getaddrinfo = original
