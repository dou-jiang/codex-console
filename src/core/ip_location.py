from __future__ import annotations

import ipaddress
import json
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable


@dataclass
class IPLocation:
    ip: str
    country: str = ""
    city: str = ""


LookupProvider = Callable[[str], IPLocation]
IPv4Resolver = Callable[[str], str]

_REQUEST_TIMEOUT_SECONDS = 2.5


def lookup_locations(
    hosts: list[str],
    *,
    ip_sb_lookup: LookupProvider | None = None,
    freeip_lookup: LookupProvider | None = None,
    resolver: IPv4Resolver | None = None,
    max_workers: int = 8,
) -> dict[str, IPLocation]:
    if not hosts:
        return {}

    ip_sb_lookup = ip_sb_lookup or lookup_ip_sb
    freeip_lookup = freeip_lookup or lookup_freeipapi
    resolver = resolver or resolve_ipv4

    host_order: list[str] = []
    target_by_host: dict[str, str] = {}
    hosts_by_target: dict[str, list[str]] = {}

    for raw_host in hosts:
        host = str(raw_host).strip()
        if not host or host in target_by_host:
            continue
        host_order.append(host)
        target = _safe_resolve(host, resolver) or host
        target_by_host[host] = target
        hosts_by_target.setdefault(target, []).append(host)

    location_by_target: dict[str, IPLocation] = {}
    ipv4_targets = [target for target in hosts_by_target if _is_ipv4(target)]
    if ipv4_targets:
        worker_count = min(max(1, max_workers), len(ipv4_targets))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_lookup_with_fallback, target, ip_sb_lookup, freeip_lookup): target
                for target in ipv4_targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    location_by_target[target] = _normalize_location(target, future.result())
                except Exception:
                    location_by_target[target] = IPLocation(ip=target)

    for target in hosts_by_target:
        if target not in location_by_target:
            location_by_target[target] = IPLocation(ip=target if _is_ipv4(target) else "")

    results: dict[str, IPLocation] = {}
    for host in host_order:
        target = target_by_host[host]
        location = location_by_target[target]
        resolved_ip = location.ip or (target if _is_ipv4(target) else "")
        results[host] = IPLocation(ip=resolved_ip, country=location.country, city=location.city)

    return results


def resolve_ipv4(host: str) -> str:
    if _is_ipv4(host):
        return host

    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None, socket.AF_INET):
        if family != socket.AF_INET:
            continue
        ip = sockaddr[0]
        if _is_ipv4(ip):
            return ip
    return ""


def lookup_ip_sb(ip: str) -> IPLocation:
    payload = _fetch_json(f"https://api.ip.sb/geoip/{ip}")
    return IPLocation(
        ip=ip,
        country=_as_text(payload.get("country") or payload.get("country_name")),
        city=_as_text(payload.get("city")),
    )


def lookup_freeipapi(ip: str) -> IPLocation:
    payload = _fetch_json(f"https://freeipapi.com/api/json/{ip}")
    return IPLocation(
        ip=ip,
        country=_as_text(payload.get("countryName") or payload.get("country")),
        city=_as_text(payload.get("cityName") or payload.get("city")),
    )


def _lookup_with_fallback(ip: str, ip_sb_lookup: LookupProvider, freeip_lookup: LookupProvider) -> IPLocation:
    primary = _safe_provider_lookup(ip_sb_lookup, ip)
    if primary.country or primary.city:
        return primary

    secondary = _safe_provider_lookup(freeip_lookup, ip)
    if secondary.country or secondary.city:
        return secondary

    if primary.ip:
        return primary
    if secondary.ip:
        return secondary
    return IPLocation(ip=ip)


def _safe_provider_lookup(provider: LookupProvider, ip: str) -> IPLocation:
    try:
        return _normalize_location(ip, provider(ip))
    except Exception:
        return IPLocation(ip=ip)


def _safe_resolve(host: str, resolver: IPv4Resolver) -> str:
    try:
        resolved = resolver(host)
    except Exception:
        return ""
    return resolved if _is_ipv4(resolved) else ""


def _normalize_location(ip: str, location: IPLocation | dict[str, object] | None) -> IPLocation:
    if isinstance(location, IPLocation):
        return IPLocation(
            ip=location.ip or ip,
            country=location.country.strip(),
            city=location.city.strip(),
        )

    if isinstance(location, dict):
        return IPLocation(
            ip=_as_text(location.get("ip")) or ip,
            country=_as_text(location.get("country")),
            city=_as_text(location.get("city")),
        )

    return IPLocation(ip=ip)


def _fetch_json(url: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="ignore")
            payload = json.loads(body) if body else {}
    except Exception:
        return {}

    return payload if isinstance(payload, dict) else {}


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except Exception:
        return False
    return True


def _as_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
