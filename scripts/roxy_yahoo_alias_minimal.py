#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.yahoo_mail import YahooMailService


def roxy_request(
    api_host: str,
    token: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    base = str(api_host or "http://127.0.0.1:50000").rstrip("/")
    url = f"{base}{path}"
    headers = {"Accept": "application/json", "token": str(token or "").strip()}
    data = None
    payload = {k: v for k, v in (payload or {}).items() if v not in (None, "", [], {})}
    if method.upper() == "GET":
        if payload:
            url = f"{url}?{urlencode(payload, doseq=True)}"
    else:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Roxy {method} {path} HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except URLError as exc:
        raise RuntimeError(f"Roxy {method} {path} connect failed: {exc}") from exc
    parsed = json.loads(raw)
    if parsed.get("code") not in (None, 0, "0"):
        raise RuntimeError(f"Roxy {method} {path} code={parsed.get('code')}: {json.dumps(parsed, ensure_ascii=False)}")
    return parsed


def normalize_ws_endpoint(api_host: str, ws_endpoint: str) -> str:
    raw_ws = str(ws_endpoint or "").strip()
    if not raw_ws:
        return raw_ws
    ws_parsed = urlparse(raw_ws)
    api_parsed = urlparse(str(api_host or "").strip() or "http://127.0.0.1:50000")
    if ws_parsed.hostname not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return raw_ws
    if not api_parsed.hostname or api_parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return raw_ws
    port = f":{ws_parsed.port}" if ws_parsed.port else ""
    rewritten = ws_parsed._replace(netloc=f"{api_parsed.hostname}{port}")
    return urlunparse(rewritten)


def ensure_tcp_reachable(host: str, port: int, timeout: float = 5.0) -> None:
    with socket.create_connection((host, int(port)), timeout=timeout):
        return


def select_mail_page(browser):
    for context in list(getattr(browser, "contexts", []) or []):
        for page in context.pages:
            current_url = str(getattr(page, "url", "") or "")
            if "mail.yahoo.com" in current_url or "login.yahoo.com" in current_url:
                return context, page
    for context in list(getattr(browser, "contexts", []) or []):
        if context.pages:
            return context, context.pages[0]
    contexts = list(getattr(browser, "contexts", []) or [])
    if not contexts:
        raise RuntimeError("connect_over_cdp 未返回任何 browser context")
    context = contexts[0]
    return context, context.new_page()


def main() -> int:
    parser = argparse.ArgumentParser(description="最小脚本：接管 Roxy 已登录 Yahoo 窗口并创建 alias")
    parser.add_argument("--api-host", default="http://127.0.0.1:50000")
    parser.add_argument("--token", default="")
    parser.add_argument("--workspace-id", type=int, default=0)
    parser.add_argument("--dir-id", default="")
    parser.add_argument("--ws-endpoint", default="")
    parser.add_argument("--parent-email", required=True)
    parser.add_argument("--parent-password", required=True)
    parser.add_argument("--parent-app-password", default="")
    parser.add_argument("--domain", default="yahoo.com")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--force-open", action="store_true")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "release" / "roxy_yahoo_alias_minimal.json"))
    args = parser.parse_args()

    ws_endpoint = str(args.ws_endpoint or "").strip()
    if ws_endpoint:
        ws_endpoint = normalize_ws_endpoint(args.api_host, ws_endpoint)
    else:
        missing = []
        if not str(args.token or "").strip():
            missing.append("--token")
        if int(args.workspace_id or 0) <= 0:
            missing.append("--workspace-id")
        if not str(args.dir_id or "").strip():
            missing.append("--dir-id")
        if missing:
            parser.error("未传 --ws-endpoint 时必须提供: " + ", ".join(missing))

        open_resp = roxy_request(
            args.api_host,
            args.token,
            "POST",
            "/browser/open",
            {
                "workspaceId": args.workspace_id,
                "dirId": args.dir_id,
                "forceOpen": bool(args.force_open),
                "headless": bool(args.headless),
            },
        )
        ws_endpoint = normalize_ws_endpoint(args.api_host, str((open_resp.get("data") or {}).get("ws") or ""))
    ws_parsed = urlparse(ws_endpoint)
    if not ws_parsed.hostname or not ws_parsed.port:
        raise RuntimeError(f"无效 ws 地址: {ws_endpoint}")
    ensure_tcp_reachable(ws_parsed.hostname, int(ws_parsed.port), timeout=8.0)

    yahoo = YahooMailService(
        {
            "parent_email": args.parent_email,
            "parent_password": args.parent_password,
            "parent_app_password": args.parent_app_password,
            "domain": args.domain,
            "headless": bool(args.headless),
        }
    )

    sync_playwright = yahoo._ensure_playwright()
    playwright_ctx = sync_playwright()
    pw = playwright_ctx.__enter__()
    try:
        browser = pw.chromium.connect_over_cdp(ws_endpoint)
        _context, page = select_mail_page(browser)
        page.set_default_timeout(45000)
        page.goto(yahoo.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)

        visible = yahoo._extract_visible_text(page).lower()
        if "sign in" in visible and "yahoo" in visible and "mail.yahoo.com" not in str(page.url or ""):
            artifact = yahoo._dump_page_debug_artifacts(page, "roxy_yahoo_alias_not_logged_in")
            raise RuntimeError(f"目标窗口未保持 Yahoo 登录态 | dump={artifact.get('json') if artifact else '-'}")

        created = yahoo._create_and_verify_alias_on_page(
            page,
            domain=args.domain,
            max_attempts=max(2, int(yahoo.config.get("max_retries") or 3)),
        )
        alias_email = str(created.get("alias_email") or "").strip().lower()
        nickname = str(created.get("nickname") or "").strip().lower()
        keyword = str(created.get("keyword") or "").strip().lower()

        payload = {
            "success": True,
            "workspace_id": int(args.workspace_id or 0),
            "dir_id": str(args.dir_id or ""),
            "headless": bool(args.headless),
            "ws_endpoint": ws_endpoint,
            "attach_mode": "direct_ws" if str(args.ws_endpoint or "").strip() else "roxy_open",
            "alias_email": alias_email,
            "nickname": nickname,
            "keyword": keyword,
            "timestamp": int(time.time()),
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            playwright_ctx.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
