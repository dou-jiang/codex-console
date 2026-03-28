"""
Random billing profile helper.

Strategy:
1) Try meiguodizhi pages by country.
2) If parsing fails, fall back to local random profile.
"""

from __future__ import annotations

import html
import logging
import os
import random
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.meiguodizhi.com"
ENABLE_EXTERNAL_SOURCE = str(os.getenv("RANDOM_BILLING_ENABLE_EXTERNAL", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

COUNTRY_CURRENCY_MAP: Dict[str, str] = {
    "US": "USD",
    "GB": "GBP",
    "CA": "CAD",
    "AU": "AUD",
    "SG": "SGD",
    "HK": "HKD",
    "JP": "JPY",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
}

COUNTRY_ROUTE_CANDIDATES: Dict[str, List[str]] = {
    "US": [
        "/usa-address",
        "/usa-address/hot-city-Seattle?hl=en",
        "/?hl=en",
    ],
    "GB": ["/uk-address", "/gb-address"],
    "CA": ["/canada-address", "/ca-address"],
    "AU": ["/australia-address", "/au-address"],
    "SG": ["/singapore-address", "/sg-address"],
    "HK": ["/hongkong-address", "/hk-address"],
    "JP": ["/japan-address", "/jp-address"],
    "DE": ["/germany-address", "/de-address"],
    "FR": ["/france-address", "/fr-address"],
    "IT": ["/italy-address", "/it-address"],
    "ES": ["/spain-address", "/es-address"],
}

FIRST_NAMES = [
    "James",
    "Olivia",
    "Noah",
    "Emma",
    "Liam",
    "Sophia",
    "Ethan",
    "Mia",
    "Aiden",
    "Ava",
    "Lucas",
    "Amelia",
    "Henry",
    "Harper",
]

LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Wilson",
    "Anderson",
    "Taylor",
    "Thomas",
]

STREET_NAMES = [
    "Main St",
    "Oak Ave",
    "Maple Dr",
    "Pine St",
    "Cedar Ln",
    "Park Ave",
    "Sunset Blvd",
    "River Rd",
]

STREET_SUFFIXES = ["St", "Ave", "Blvd", "Dr", "Ln", "Way", "Ct", "Pl", "Rd"]
US_STREET_BASES = [
    "Washington",
    "Lincoln",
    "Franklin",
    "Jefferson",
    "Madison",
    "Jackson",
    "Monroe",
    "Adams",
    "Wilson",
    "Lake",
    "Hill",
    "Sunset",
    "Park",
    "Riverside",
    "Highland",
    "Center",
    "Valley",
    "Cedar",
    "Pine",
    "Maple",
    "Willow",
    "Cherry",
    "Elm",
    "Locust",
    "Meadow",
]

US_STATE_CITY_POOL: Dict[str, Dict[str, List[str] | str]] = {
    "CA": {"name": "California", "cities": ["Los Angeles", "San Francisco", "San Diego", "San Jose"], "zip_prefix": "9"},
    "NY": {"name": "New York", "cities": ["New York", "Brooklyn", "Buffalo", "Rochester"], "zip_prefix": "1"},
    "TX": {"name": "Texas", "cities": ["Houston", "Dallas", "Austin", "San Antonio"], "zip_prefix": "7"},
    "FL": {"name": "Florida", "cities": ["Miami", "Orlando", "Tampa", "Jacksonville"], "zip_prefix": "3"},
    "WA": {"name": "Washington", "cities": ["Seattle", "Spokane", "Tacoma", "Bellevue"], "zip_prefix": "9"},
    "IL": {"name": "Illinois", "cities": ["Chicago", "Aurora", "Naperville", "Rockford"], "zip_prefix": "6"},
    "PA": {"name": "Pennsylvania", "cities": ["Philadelphia", "Pittsburgh", "Allentown", "Erie"], "zip_prefix": "1"},
    "OH": {"name": "Ohio", "cities": ["Columbus", "Cleveland", "Cincinnati", "Toledo"], "zip_prefix": "4"},
    "GA": {"name": "Georgia", "cities": ["Atlanta", "Savannah", "Augusta", "Macon"], "zip_prefix": "3"},
    "NC": {"name": "North Carolina", "cities": ["Charlotte", "Raleigh", "Greensboro", "Durham"], "zip_prefix": "2"},
    "VA": {"name": "Virginia", "cities": ["Virginia Beach", "Richmond", "Norfolk", "Arlington"], "zip_prefix": "2"},
    "MA": {"name": "Massachusetts", "cities": ["Boston", "Worcester", "Cambridge", "Springfield"], "zip_prefix": "0"},
    "NJ": {"name": "New Jersey", "cities": ["Newark", "Jersey City", "Paterson", "Edison"], "zip_prefix": "0"},
    "MI": {"name": "Michigan", "cities": ["Detroit", "Grand Rapids", "Lansing", "Ann Arbor"], "zip_prefix": "4"},
    "AZ": {"name": "Arizona", "cities": ["Phoenix", "Tucson", "Mesa", "Chandler"], "zip_prefix": "8"},
    "CO": {"name": "Colorado", "cities": ["Denver", "Colorado Springs", "Aurora", "Fort Collins"], "zip_prefix": "8"},
    "NV": {"name": "Nevada", "cities": ["Las Vegas", "Reno", "Henderson", "North Las Vegas"], "zip_prefix": "8"},
    "OR": {"name": "Oregon", "cities": ["Portland", "Salem", "Eugene", "Gresham"], "zip_prefix": "9"},
    "MN": {"name": "Minnesota", "cities": ["Minneapolis", "Saint Paul", "Rochester", "Bloomington"], "zip_prefix": "5"},
    "MO": {"name": "Missouri", "cities": ["Kansas City", "St. Louis", "Springfield", "Columbia"], "zip_prefix": "6"},
    "TN": {"name": "Tennessee", "cities": ["Nashville", "Memphis", "Knoxville", "Chattanooga"], "zip_prefix": "3"},
    "IN": {"name": "Indiana", "cities": ["Indianapolis", "Fort Wayne", "Evansville", "South Bend"], "zip_prefix": "4"},
    "WI": {"name": "Wisconsin", "cities": ["Milwaukee", "Madison", "Green Bay", "Kenosha"], "zip_prefix": "5"},
    "MD": {"name": "Maryland", "cities": ["Baltimore", "Columbia", "Germantown", "Rockville"], "zip_prefix": "2"},
    "SC": {"name": "South Carolina", "cities": ["Charleston", "Columbia", "Greenville", "Myrtle Beach"], "zip_prefix": "2"},
    "AL": {"name": "Alabama", "cities": ["Birmingham", "Montgomery", "Huntsville", "Mobile"], "zip_prefix": "3"},
    "LA": {"name": "Louisiana", "cities": ["New Orleans", "Baton Rouge", "Shreveport", "Lafayette"], "zip_prefix": "7"},
    "OK": {"name": "Oklahoma", "cities": ["Oklahoma City", "Tulsa", "Norman", "Edmond"], "zip_prefix": "7"},
    "KY": {"name": "Kentucky", "cities": ["Louisville", "Lexington", "Bowling Green", "Owensboro"], "zip_prefix": "4"},
    "UT": {"name": "Utah", "cities": ["Salt Lake City", "Provo", "West Valley City", "Ogden"], "zip_prefix": "8"},
}

LOCAL_ADDRESS_POOL: Dict[str, List[Dict[str, str]]] = {
    "US": [
        {"city": "San Jose", "state": "CA", "postal": "95112"},
        {"city": "Austin", "state": "TX", "postal": "78701"},
        {"city": "Seattle", "state": "WA", "postal": "98101"},
        {"city": "Miami", "state": "FL", "postal": "33101"},
        {"city": "Boston", "state": "MA", "postal": "02108"},
    ],
    "GB": [
        {"city": "London", "state": "London", "postal": "SW1A 1AA"},
        {"city": "Manchester", "state": "England", "postal": "M1 1AE"},
    ],
    "CA": [
        {"city": "Toronto", "state": "ON", "postal": "M5V 2T6"},
        {"city": "Vancouver", "state": "BC", "postal": "V6B 1A1"},
    ],
    "AU": [
        {"city": "Sydney", "state": "NSW", "postal": "2000"},
        {"city": "Melbourne", "state": "VIC", "postal": "3000"},
    ],
    "SG": [{"city": "Singapore", "state": "SG", "postal": "018956"}],
    "HK": [{"city": "Hong Kong", "state": "HK", "postal": "000000"}],
    "JP": [
        {"city": "Tokyo", "state": "Tokyo", "postal": "100-0001"},
        {"city": "Osaka", "state": "Osaka", "postal": "530-0001"},
    ],
    "DE": [
        {"city": "Berlin", "state": "BE", "postal": "10115"},
        {"city": "Munich", "state": "BY", "postal": "80331"},
    ],
    "FR": [
        {"city": "Paris", "state": "IDF", "postal": "75001"},
        {"city": "Lyon", "state": "ARA", "postal": "69001"},
    ],
    "IT": [
        {"city": "Rome", "state": "RM", "postal": "00118"},
        {"city": "Milan", "state": "MI", "postal": "20121"},
    ],
    "ES": [
        {"city": "Madrid", "state": "MD", "postal": "28001"},
        {"city": "Barcelona", "state": "CT", "postal": "08001"},
    ],
}


def _normalize_country(country: Optional[str]) -> str:
    code = str(country or "").strip().upper()
    if not code:
        return "US"
    if code in COUNTRY_CURRENCY_MAP:
        return code
    return "US"


def _request_text(url: str, proxy: Optional[str]) -> str:
    headers = {
        "User-Agent": "codex-console2/random-billing",
        "Accept": "text/html,application/xhtml+xml",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if proxy:
        # 显式代理：按用户传入值请求。
        resp = cffi_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=25,
            impersonate="chrome110",
        )
    else:
        # 无显式代理：禁用环境变量代理，避免被系统 HTTP(S)_PROXY 污染。
        with cffi_requests.Session() as session:
            try:
                session.trust_env = False
            except Exception:
                pass
            resp = session.get(
                url,
                headers=headers,
                proxies=None,
                timeout=25,
                impersonate="chrome110",
            )
    resp.raise_for_status()
    return str(resp.text or "")


def _extract_random_url(page_html: str, page_url: str) -> Optional[str]:
    text = str(page_html or "")
    patterns = [
        r'<a[^>]+href="([^"]+)"[^>]*>\s*随机地址\s*</a>',
        r"<a[^>]+href='([^']+)'[^>]*>\s*随机地址\s*</a>",
        r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        href = str(match.group(1) or "").strip()
        if not href:
            continue
        return urljoin(page_url, href)
    return None


def _extract_by_patterns(text: str, patterns: List[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        value = html.unescape(str(match.group(1) or "").strip())
        value = re.sub(r"\s+", " ", value).strip()
        if value and len(value) <= 120:
            return value
    return ""


def _extract_text_after_label(page_text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}\s*\n\s*([^\n]{{2,80}})"
    match = re.search(pattern, page_text, flags=re.IGNORECASE)
    if not match:
        return ""
    value = str(match.group(1) or "").strip()
    if value in {"街道", "城市", "州", "邮编", "全名"}:
        return ""
    return value


def _build_us_line1() -> str:
    number = random.randint(18, 9999)
    base = random.choice(US_STREET_BASES)
    suffix = random.choice(STREET_SUFFIXES)
    line1 = f"{number} {base} {suffix}"
    if random.random() < 0.28:
        line1 += f" Apt {random.randint(1, 999)}"
    return line1


def _build_us_postal(prefix: str) -> str:
    safe_prefix = str(prefix or "").strip()
    if not safe_prefix or not safe_prefix[0].isdigit():
        safe_prefix = str(random.randint(0, 9))
    return f"{safe_prefix[0]}{random.randint(0, 9999):04d}"


def _build_local_geo_profile(country_code: str, reason: Optional[str] = None, *, fallback_source: bool = False) -> Dict[str, str]:
    if country_code == "US":
        state_code, state_obj = random.choice(list(US_STATE_CITY_POOL.items()))
        city = str(random.choice(list(state_obj.get("cities", []) or ["Seattle"])))
        state_name = str(state_obj.get("name", state_code))
        postal_code = _build_us_postal(str(state_obj.get("zip_prefix", "9")))
        profile = {
            "billing_name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
            "country_code": "US",
            "currency": "USD",
            "address_line1": _build_us_line1(),
            "address_city": city,
            "address_state": state_code,
            "address_state_name": state_name,
            "postal_code": postal_code,
            "source": "local_geo_fallback" if fallback_source else "local_geo",
        }
    else:
        pool = LOCAL_ADDRESS_POOL.get(country_code) or LOCAL_ADDRESS_POOL["US"]
        picked = random.choice(pool)
        number = random.randint(18, 9999)
        line1 = f"{number} {random.choice(STREET_NAMES)}"
        profile = {
            "billing_name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
            "country_code": country_code,
            "currency": COUNTRY_CURRENCY_MAP.get(country_code, "USD"),
            "address_line1": line1,
            "address_city": picked.get("city", ""),
            "address_state": picked.get("state", ""),
            "postal_code": picked.get("postal", ""),
            "source": "local_geo_fallback" if fallback_source else "local_geo",
        }
    if reason:
        profile["fallback_reason"] = str(reason)[:220]
    return profile


def _parse_profile_from_html(page_html: str, country_code: str) -> Optional[Dict[str, str]]:
    raw = str(page_html or "")
    if not raw:
        return None
    compact = raw.replace("\r", "\n")
    text_only = re.sub(r"<[^>]+>", "\n", compact)
    text_only = html.unescape(text_only)
    text_only = re.sub(r"[ \t]+", " ", text_only)
    text_only = re.sub(r"\n+", "\n", text_only)

    name = _extract_by_patterns(
        compact,
        [
            r'"(?:full_?name|name)"\s*:\s*"([^"]+)"',
            r'name=["\'](?:full_?name|name)["\'][^>]*value=["\']([^"\']+)["\']',
            r'id=["\'](?:full_?name|name)["\'][^>]*value=["\']([^"\']+)["\']',
        ],
    ) or _extract_text_after_label(text_only, "全名")

    line1 = _extract_by_patterns(
        compact,
        [
            r'"(?:street|address|address_line1|line1)"\s*:\s*"([^"]+)"',
            r'name=["\'](?:street|address|address_line1|line1)["\'][^>]*value=["\']([^"\']+)["\']',
        ],
    ) or _extract_text_after_label(text_only, "街道")

    city = _extract_by_patterns(
        compact,
        [
            r'"(?:city|locality)"\s*:\s*"([^"]+)"',
            r'name=["\'](?:city|locality)["\'][^>]*value=["\']([^"\']+)["\']',
        ],
    ) or _extract_text_after_label(text_only, "城市")

    state = _extract_by_patterns(
        compact,
        [
            r'"(?:state|province|administrativeArea)"\s*:\s*"([^"]+)"',
            r'name=["\'](?:state|province|administrativeArea)["\'][^>]*value=["\']([^"\']+)["\']',
        ],
    ) or _extract_text_after_label(text_only, "州")

    postal = _extract_by_patterns(
        compact,
        [
            r'"(?:postal|zip|zipcode|postalCode)"\s*:\s*"([^"]+)"',
            r'name=["\'](?:postal|zip|zipcode|postalCode)["\'][^>]*value=["\']([^"\']+)["\']',
        ],
    ) or _extract_text_after_label(text_only, "邮编")

    profile = {
        "billing_name": name,
        "country_code": country_code,
        "currency": COUNTRY_CURRENCY_MAP.get(country_code, "USD"),
        "address_line1": line1,
        "address_city": city,
        "address_state": state,
        "postal_code": postal,
        "source": "meiguodizhi",
    }
    required = ("billing_name", "address_line1", "address_city", "address_state", "postal_code")
    if all(str(profile.get(key) or "").strip() for key in required):
        return profile
    return None


def _build_local_profile(country_code: str, reason: Optional[str] = None) -> Dict[str, str]:
    return _build_local_geo_profile(country_code, reason=reason, fallback_source=True)


def _iter_country_pages(country_code: str) -> List[str]:
    pages: List[str] = []
    for path in COUNTRY_ROUTE_CANDIDATES.get(country_code, []):
        if not path.startswith("/"):
            path = "/" + path
        pages.append(urljoin(BASE_URL, path))
    if not pages:
        pages.append(urljoin(BASE_URL, "/usa-address"))
    # 兜底再补一层首页，部分站点路径偶发调整时可避免直接全量失败。
    pages.append(urljoin(BASE_URL, "/?hl=en"))
    # 去重并保持顺序
    ordered_unique: List[str] = []
    seen = set()
    for item in pages:
        if item in seen:
            continue
        seen.add(item)
        ordered_unique.append(item)
    return ordered_unique


def generate_random_billing_profile(country: Optional[str], proxy: Optional[str] = None) -> Dict[str, str]:
    country_code = _normalize_country(country)
    # 默认本地生成，确保可用性与速度；需要外部源时可配置 RANDOM_BILLING_ENABLE_EXTERNAL=true。
    if not ENABLE_EXTERNAL_SOURCE:
        return _build_local_geo_profile(country_code)

    pages = _iter_country_pages(country_code)
    attempts: List[str] = []
    proxy_candidates: List[Optional[str]] = []
    for value in (proxy, None):
        if value not in proxy_candidates:
            proxy_candidates.append(value)

    for page_url in pages:
        for proxy_item in proxy_candidates:
            try:
                page_html = _request_text(page_url, proxy_item)
                random_url = _extract_random_url(page_html, page_url)
                html_candidates = [(page_html, page_url)]
                if random_url:
                    try:
                        random_html = _request_text(random_url, proxy_item)
                        html_candidates.insert(0, (random_html, random_url))
                    except Exception as random_exc:
                        attempts.append(f"{random_url}: {random_exc}")

                for html_content, used_url in html_candidates:
                    parsed = _parse_profile_from_html(html_content, country_code)
                    if parsed:
                        parsed["source_url"] = used_url
                        if proxy_item:
                            parsed["proxy_used"] = "on"
                        return parsed
                attempts.append(f"{page_url}: parse_empty")
            except Exception as exc:
                attempts.append(f"{page_url}: {exc}")
                continue

    reason = "; ".join(attempts[-3:]) if attempts else "unknown_error"
    logger.warning("random billing fallback triggered: country=%s reason=%s", country_code, reason)
    return _build_local_geo_profile(country_code, reason=reason, fallback_source=True)
