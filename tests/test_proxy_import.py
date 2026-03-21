import pytest

from src.core.proxy_import import parse_proxy_line


def test_parse_proxy_line_supports_all_formats():
    cases = [
        ("http://1.1.1.1:8080:user:pass", "socks5", ("http", "1.1.1.1", 8080, "user", "pass")),
        ("2.2.2.2:9090:user:pass", "http", ("http", "2.2.2.2", 9090, "user", "pass")),
        ("user:pass@3.3.3.3:3128", "http", ("http", "3.3.3.3", 3128, "user", "pass")),
        ("4.4.4.4:1080@user:pass", "socks5", ("socks5", "4.4.4.4", 1080, "user", "pass")),
        ("proxy.example.com:8000", "http", ("http", "proxy.example.com", 8000, None, None)),
    ]

    for raw, default_type, expected in cases:
        parsed = parse_proxy_line(raw, default_type=default_type, line_no=1)
        assert (parsed.type, parsed.host, parsed.port, parsed.username, parsed.password) == expected


def test_parse_proxy_line_rejects_invalid_input():
    bad_lines = [
        "ftp://1.1.1.1:21:user:pass",
        "1.1.1.1:70000",
        "2001:db8::1:8080",
        "user:na:me@1.1.1.1:8080",
    ]

    for raw in bad_lines:
        with pytest.raises(ValueError):
            parse_proxy_line(raw, default_type="http", line_no=1)


def test_parse_proxy_line_ignores_default_type_for_explicit_protocol():
    parsed = parse_proxy_line(
        "http://1.1.1.1:8080:user:pass",
        default_type="ftp",
        line_no=2,
    )
    assert parsed.type == "http"


def test_parse_proxy_line_rejects_invalid_ip_like_domain():
    with pytest.raises(ValueError):
        parse_proxy_line("999.999.999.999:80", default_type="http", line_no=3)
