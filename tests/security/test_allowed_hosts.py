import socket

from nanobot.security.network import configure_ssrf_whitelist, validate_url_target


def _private_getaddrinfo(host, *args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("10.0.0.5", 443),
        )
    ]


def test_allowed_hostname_can_resolve_to_private_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _private_getaddrinfo)
    configure_ssrf_whitelist([], ["gitlab.internal"])

    ok, error = validate_url_target("https://gitlab.internal/repo")

    assert ok is True
    assert error == ""
    configure_ssrf_whitelist([])


def test_unlisted_private_hostname_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _private_getaddrinfo)
    configure_ssrf_whitelist([], ["gitlab.internal"])

    ok, error = validate_url_target("https://other.internal/repo")

    assert ok is False
    assert "private/internal" in error
    configure_ssrf_whitelist([])


def test_allowed_hosts_does_not_allow_ip_literals(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _private_getaddrinfo)
    configure_ssrf_whitelist([], ["10.0.0.5"])

    ok, error = validate_url_target("https://10.0.0.5/repo")

    assert ok is False
    assert "private/internal" in error
    configure_ssrf_whitelist([])

