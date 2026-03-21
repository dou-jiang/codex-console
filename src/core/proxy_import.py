from __future__ import annotations

import re
from dataclasses import dataclass


_PROTOCOLS = {"http", "socks5"}
_IPV4_REGEX = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}$"
)
_DOMAIN_REGEX = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    re.IGNORECASE,
)

_PROTOCOL_PATTERN = re.compile(
    r"^(?P<protocol>http|socks5)://(?P<host>[^:@]+):(?P<port>\d+):(?P<username>[^:@]+):(?P<password>[^:@]+)$",
    re.IGNORECASE,
)
_CREDENTIALS_PREFIX_PATTERN = re.compile(
    r"^(?P<username>[^:@]+):(?P<password>[^:@]+)@(?P<host>[^:@]+):(?P<port>\d+)$"
)
_HOST_PORT_WITH_TRAILING_AUTH_PATTERN = re.compile(
    r"^(?P<host>[^:@]+):(?P<port>\d+)@(?P<username>[^:@]+):(?P<password>[^:@]+)$"
)
_HOST_PORT_WITH_COLON_AUTH_PATTERN = re.compile(
    r"^(?P<host>[^:@]+):(?P<port>\d+):(?P<username>[^:@]+):(?P<password>[^:@]+)$"
)
_HOST_PORT_PATTERN = re.compile(r"^(?P<host>[^:@]+):(?P<port>\d+)$")


@dataclass
class ParsedProxyLine:
    line_no: int
    raw_line: str
    type: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_proxy_line(raw_line: str, default_type: str, line_no: int) -> ParsedProxyLine:
    trimmed = raw_line.strip()
    if not trimmed:
        raise ValueError("empty proxy line")

    for builder in (
        _build_protocol_prefixed,
        _build_username_password_at,
        _build_host_port_at,
        _build_host_port_with_auth,
        _build_host_port,
    ):
        result = builder(
            trimmed,
            default_type,
            line_no,
        )
        if result is not None:
            return result

    raise ValueError("unsupported proxy line format")


def _build_protocol_prefixed(line: str, default_type: str, line_no: int) -> ParsedProxyLine | None:
    match = _PROTOCOL_PATTERN.fullmatch(line)
    if not match:
        return None
    protocol = match.group("protocol").lower()
    host = _validate_host(match.group("host"))
    port = _validate_port(match.group("port"))
    username = _validate_credential(match.group("username"))
    password = _validate_credential(match.group("password"))
    return ParsedProxyLine(
        line_no=line_no,
        raw_line=line,
        type=protocol,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def _build_username_password_at(line: str, default_type: str, line_no: int) -> ParsedProxyLine | None:
    match = _CREDENTIALS_PREFIX_PATTERN.fullmatch(line)
    if not match:
        return None
    return _build_authenticated(
        match, default_type, line, line_no,
    )


def _build_host_port_at(line: str, default_type: str, line_no: int) -> ParsedProxyLine | None:
    match = _HOST_PORT_WITH_TRAILING_AUTH_PATTERN.fullmatch(line)
    if not match:
        return None
    return _build_authenticated(
        match, default_type, line, line_no,
    )


def _build_host_port_with_auth(line: str, default_type: str, line_no: int) -> ParsedProxyLine | None:
    match = _HOST_PORT_WITH_COLON_AUTH_PATTERN.fullmatch(line)
    if not match:
        return None
    return _build_authenticated(
        match, default_type, line, line_no,
    )


def _build_host_port(line: str, default_type: str, line_no: int) -> ParsedProxyLine | None:
    match = _HOST_PORT_PATTERN.fullmatch(line)
    if not match:
        return None
    host = _validate_host(match.group("host"))
    port = _validate_port(match.group("port"))
    return ParsedProxyLine(
        line_no=line_no,
        raw_line=line,
        type=_normalize_protocol(default_type),
        host=host,
        port=port,
    )


def _build_authenticated(match: re.Match[str], default_type: str, line: str, line_no: int) -> ParsedProxyLine:
    host = _validate_host(match.group("host"))
    port = _validate_port(match.group("port"))
    username = _validate_credential(match.group("username"))
    password = _validate_credential(match.group("password"))
    return ParsedProxyLine(
        line_no=line_no,
        raw_line=line,
        type=_normalize_protocol(default_type),
        host=host,
        port=port,
        username=username,
        password=password,
    )


def _validate_host(host: str) -> str:
    if not host or ":" in host or "@" in host:
        raise ValueError("invalid host")
    if _IPv4 := _IPV4_REGEX.fullmatch(host):
        return host
    if "." in host and all(ch.isdigit() or ch == "." for ch in host):
        raise ValueError("invalid host")
    if _DOMAIN_REGEX.fullmatch(host):
        return host
    raise ValueError("invalid host")


def _validate_port(port: str) -> int:
    if not port.isdecimal():
        raise ValueError("invalid port")
    value = int(port)
    if not (1 <= value <= 65535):
        raise ValueError("invalid port")
    return value


def _validate_credential(value: str) -> str:
    if not value or ":" in value or "@" in value:
        raise ValueError("invalid credential")
    return value


def _normalize_protocol(value: str) -> str:
    if not value:
        raise ValueError("missing protocol")
    normalized = value.strip().lower()
    if normalized not in _PROTOCOLS:
        raise ValueError("unsupported protocol")
    return normalized
