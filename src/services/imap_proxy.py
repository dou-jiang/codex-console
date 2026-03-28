from __future__ import annotations

import base64
import imaplib
import ipaddress
import logging
import socket
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ..config.settings import get_settings


logger = logging.getLogger(__name__)

IMAP_PORT = 143
IMAP_SSL_PORT = 993

_PROXY_LOOKUP_ORDER = ("https", "http", "all")
_SOCKS5_VERSION = 5
_SOCKS5_AUTH_NONE = 0
_SOCKS5_AUTH_USERPASS = 2


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    @classmethod
    def from_url(cls, proxy_url: str) -> "ProxyEndpoint":
        normalized_url = (proxy_url or "").strip()
        if not normalized_url:
            raise ValueError("代理 URL 不能为空")

        if "://" not in normalized_url:
            normalized_url = f"http://{normalized_url}"

        parsed = urllib.parse.urlsplit(normalized_url)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"不支持的代理协议: {scheme}")

        if not parsed.hostname or not parsed.port:
            raise ValueError(f"无效的代理 URL: {proxy_url}")

        username = urllib.parse.unquote(parsed.username) if parsed.username else None
        password = urllib.parse.unquote(parsed.password) if parsed.password else None

        return cls(
            scheme=scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=username,
            password=password,
        )


def resolve_effective_proxy_url(
    target_host: str,
    explicit_proxy_url: Optional[str] = None,
) -> Optional[str]:
    if explicit_proxy_url:
        return explicit_proxy_url.strip()

    try:
        settings_proxy_url = get_settings().proxy_url
        if settings_proxy_url:
            return settings_proxy_url
    except Exception as exc:
        logger.debug("读取应用代理配置失败，继续尝试系统代理: %s", exc)

    try:
        if urllib.request.proxy_bypass(target_host):
            logger.debug("目标主机命中 no_proxy，跳过系统代理: %s", target_host)
            return None
    except Exception as exc:
        logger.debug("检查系统代理 bypass 规则失败: %s", exc)

    try:
        proxies = urllib.request.getproxies()
    except Exception as exc:
        logger.debug("读取系统代理配置失败: %s", exc)
        return None

    for key in _PROXY_LOOKUP_ORDER:
        proxy_url = proxies.get(key)
        if proxy_url:
            return proxy_url

    return None


def open_imap_socket(
    host: str,
    port: int,
    timeout: Optional[float] = None,
    proxy_url: Optional[str] = None,
) -> socket.socket:
    effective_proxy_url = resolve_effective_proxy_url(host, proxy_url)
    if not effective_proxy_url:
        return socket.create_connection((host, port), timeout=timeout)

    proxy = ProxyEndpoint.from_url(effective_proxy_url)
    logger.debug(
        "通过代理建立 IMAP socket: %s://%s:%s -> %s:%s",
        proxy.scheme,
        proxy.host,
        proxy.port,
        host,
        port,
    )

    if proxy.scheme in {"http", "https"}:
        return _open_http_proxy_tunnel(proxy, host, port, timeout)

    if proxy.scheme in {"socks5", "socks5h"}:
        return _open_socks5_proxy_tunnel(proxy, host, port, timeout)

    raise ValueError(f"不支持的代理协议: {proxy.scheme}")


def create_imap_client(
    host: str,
    port: int,
    *,
    use_ssl: bool,
    timeout: Optional[float] = None,
    proxy_url: Optional[str] = None,
) -> imaplib.IMAP4:
    client_cls = ProxyAwareIMAP4_SSL if use_ssl else ProxyAwareIMAP4
    return client_cls(host=host, port=port, timeout=timeout, proxy_url=proxy_url)


class ProxyAwareIMAP4(imaplib.IMAP4):
    def __init__(
        self,
        host: str = "",
        port: int = IMAP_PORT,
        timeout: Optional[float] = None,
        proxy_url: Optional[str] = None,
    ):
        self._proxy_url = proxy_url
        super().__init__(host=host, port=port, timeout=timeout)

    def _create_socket(self, timeout: Optional[float]) -> socket.socket:
        return open_imap_socket(self.host, self.port, timeout=timeout, proxy_url=self._proxy_url)


class ProxyAwareIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str = "",
        port: int = IMAP_SSL_PORT,
        *,
        ssl_context: Optional[ssl.SSLContext] = None,
        timeout: Optional[float] = None,
        proxy_url: Optional[str] = None,
    ):
        self._proxy_url = proxy_url
        super().__init__(host=host, port=port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: Optional[float]) -> socket.socket:
        raw_socket = open_imap_socket(self.host, self.port, timeout=timeout, proxy_url=self._proxy_url)
        return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _open_http_proxy_tunnel(
    proxy: ProxyEndpoint,
    target_host: str,
    target_port: int,
    timeout: Optional[float],
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)

    try:
        if proxy.scheme == "https":
            proxy_ssl_context = ssl.create_default_context()
            sock = proxy_ssl_context.wrap_socket(sock, server_hostname=proxy.host)

        connect_request = _build_http_connect_request(proxy, target_host, target_port)
        sock.sendall(connect_request)

        response = _recv_until_double_crlf(sock)
        status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if status_code != 200:
            raise OSError(f"代理 CONNECT 失败: {status_line}")

        return sock
    except Exception:
        sock.close()
        raise


def _build_http_connect_request(proxy: ProxyEndpoint, target_host: str, target_port: int) -> bytes:
    lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: Keep-Alive",
    ]

    if proxy.username is not None:
        password = proxy.password or ""
        credentials = f"{proxy.username}:{password}".encode("utf-8")
        token = base64.b64encode(credentials).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")

    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _recv_until_double_crlf(sock: socket.socket, max_bytes: int = 65536) -> bytes:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise OSError("代理响应头过大")

    if b"\r\n\r\n" not in buffer:
        raise OSError("代理响应不完整")

    return bytes(buffer)


def _open_socks5_proxy_tunnel(
    proxy: ProxyEndpoint,
    target_host: str,
    target_port: int,
    timeout: Optional[float],
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)

    try:
        methods = [_SOCKS5_AUTH_NONE]
        if proxy.username is not None:
            methods.append(_SOCKS5_AUTH_USERPASS)

        sock.sendall(bytes([_SOCKS5_VERSION, len(methods), *methods]))
        version, method = _recv_exact(sock, 2)
        if version != _SOCKS5_VERSION:
            raise OSError(f"SOCKS5 代理握手失败: version={version}")
        if method == 0xFF:
            raise OSError("SOCKS5 代理不接受可用认证方式")

        if method == _SOCKS5_AUTH_USERPASS:
            _authenticate_socks5_userpass(sock, proxy)
        elif method != _SOCKS5_AUTH_NONE:
            raise OSError(f"SOCKS5 返回未知认证方式: {method}")

        request = bytearray([_SOCKS5_VERSION, 0x01, 0x00])
        request.extend(_encode_socks5_address(target_host))
        request.extend(target_port.to_bytes(2, byteorder="big"))
        sock.sendall(request)

        response = _recv_exact(sock, 4)
        if response[0] != _SOCKS5_VERSION:
            raise OSError(f"SOCKS5 CONNECT 响应异常: version={response[0]}")
        if response[1] != 0x00:
            raise OSError(f"SOCKS5 CONNECT 失败: code={response[1]}")

        atyp = response[3]
        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x03:
            addr_len = _recv_exact(sock, 1)[0]
            _recv_exact(sock, addr_len)
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        else:
            raise OSError(f"SOCKS5 返回未知地址类型: {atyp}")
        _recv_exact(sock, 2)

        return sock
    except Exception:
        sock.close()
        raise


def _authenticate_socks5_userpass(sock: socket.socket, proxy: ProxyEndpoint) -> None:
    username = (proxy.username or "").encode("utf-8")
    password = (proxy.password or "").encode("utf-8")

    if len(username) > 255 or len(password) > 255:
        raise ValueError("SOCKS5 用户名或密码长度不能超过 255 字节")

    request = bytearray([0x01, len(username)])
    request.extend(username)
    request.append(len(password))
    request.extend(password)
    sock.sendall(request)

    version, status = _recv_exact(sock, 2)
    if version != 0x01 or status != 0x00:
        raise OSError("SOCKS5 用户名/密码认证失败")


def _encode_socks5_address(host: str) -> bytes:
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        encoded_host = host.encode("idna")
        if len(encoded_host) > 255:
            raise ValueError("SOCKS5 域名长度不能超过 255 字节")
        return bytes([0x03, len(encoded_host)]) + encoded_host

    if ip_obj.version == 4:
        return bytes([0x01]) + ip_obj.packed

    return bytes([0x04]) + ip_obj.packed


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("代理连接意外关闭")
        data.extend(chunk)
    return bytes(data)
