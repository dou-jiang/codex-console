import pytest

from src.core.ip_location import IPLocation, lookup_locations
from src.core.proxy_import import (
    allocate_proxy_names,
    canonicalize_proxy_host,
    parse_proxy_line,
    proxy_host_port_key,
)


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


def test_canonicalize_proxy_host_lowercases_domains_but_not_ipv4():
    assert canonicalize_proxy_host("Example.COM") == "example.com"
    assert canonicalize_proxy_host("1.1.1.1") == "1.1.1.1"


def test_proxy_host_port_key_is_case_insensitive_for_domains():
    assert proxy_host_port_key("Example.COM", 8080) == proxy_host_port_key("example.com", 8080)


def test_lookup_locations_falls_back_and_caches():
    calls = []

    def fake_ip_sb(ip):
        calls.append(("ip_sb", ip))
        raise RuntimeError("boom")

    def fake_freeipapi(ip):
        calls.append(("freeipapi", ip))
        return IPLocation(ip=ip, country="United States", city="Seattle")

    result = lookup_locations(["1.1.1.1", "1.1.1.1"], ip_sb_lookup=fake_ip_sb, freeip_lookup=fake_freeipapi)

    assert result["1.1.1.1"].country == "United States"
    assert calls == [("ip_sb", "1.1.1.1"), ("freeipapi", "1.1.1.1")]


def test_lookup_locations_uses_freeipapi_when_ip_sb_result_is_partial():
    calls = []

    def fake_ip_sb(ip):
        calls.append(("ip_sb", ip))
        return IPLocation(ip=ip, country="United States", city="")

    def fake_freeipapi(ip):
        calls.append(("freeipapi", ip))
        return IPLocation(ip=ip, country="United States", city="Seattle")

    result = lookup_locations(["1.1.1.1"], ip_sb_lookup=fake_ip_sb, freeip_lookup=fake_freeipapi)

    assert result["1.1.1.1"] == IPLocation(ip="1.1.1.1", country="United States", city="Seattle")
    assert calls == [("ip_sb", "1.1.1.1"), ("freeipapi", "1.1.1.1")]


def test_lookup_locations_resolves_domains_and_deduplicates_same_target_ip():
    calls = []

    def fake_resolver(host):
        return {
            "a.example.com": "1.1.1.1",
            "b.example.com": "1.1.1.1",
        }[host]

    def fake_ip_sb(ip):
        calls.append(("ip_sb", ip))
        return IPLocation(ip=ip, country="United States", city="Seattle")

    result = lookup_locations(
        ["a.example.com", "b.example.com"],
        resolver=fake_resolver,
        ip_sb_lookup=fake_ip_sb,
    )

    expected = IPLocation(ip="1.1.1.1", country="United States", city="Seattle")
    assert result["a.example.com"] == expected
    assert result["b.example.com"] == expected
    assert calls == [("ip_sb", "1.1.1.1")]


def test_lookup_locations_swallows_resolver_and_provider_failures():
    def flaky_resolver(host):
        if host == "bad.example.com":
            raise RuntimeError("resolver boom")
        return host

    def broken_provider(ip):
        raise RuntimeError("provider boom")

    result = lookup_locations(
        ["bad.example.com", "8.8.8.8"],
        resolver=flaky_resolver,
        ip_sb_lookup=broken_provider,
        freeip_lookup=broken_provider,
    )

    assert result["bad.example.com"] == IPLocation(ip="", country="", city="")
    assert result["8.8.8.8"] == IPLocation(ip="8.8.8.8", country="", city="")


def test_allocate_proxy_names_falls_back_when_country_or_city_missing():
    names = allocate_proxy_names(
        prefixes_in_db={"代理": 2, "美国-西雅图": 1},
        locations=[
            {"host": "1.1.1.1", "country": "美国", "city": "西雅图"},
            {"host": "2.2.2.2", "country": "美国", "city": ""},
        ],
    )
    assert names == {
        "1.1.1.1": "美国-西雅图-002",
        "2.2.2.2": "代理-003",
    }
