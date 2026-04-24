#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.init_db import initialize_database
from src.database.models import EmailService as EmailServiceModel
from src.database.session import get_db
from src.services.yahoo_mail import YahooMailService


def mask_proxy(proxy_url: str | None) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return "-"
    if "@" not in raw:
        return raw
    try:
        scheme, rest = raw.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else "***"
        return f"{scheme}://{user}:***@{host}"
    except Exception:
        return raw


def resolve_service(service_id: int) -> EmailServiceModel:
    with get_db() as db:
        query = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yahoo_mail",
            EmailServiceModel.enabled == True,
        )
        if service_id:
            query = query.filter(EmailServiceModel.id == service_id)
            service = query.first()
        else:
            service = query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).first()
    if not service:
        raise RuntimeError("未找到启用中的 Yahoo 邮箱服务")
    return service


def inspect_selector(page, selector: str) -> Dict[str, Any]:
    try:
        locator = page.locator(selector).first
        count = locator.count()
        visible = bool(count and locator.is_visible())
        text = ""
        tag = ""
        if count:
            try:
                tag = str(locator.evaluate("el => el.tagName.toLowerCase()") or "")
            except Exception:
                tag = ""
            try:
                text = str(locator.inner_text(timeout=500) or "")[:200]
            except Exception:
                text = ""
        return {
            "selector": selector,
            "count": count,
            "visible": visible,
            "tag": tag,
            "text": text,
        }
    except Exception as exc:
        return {
            "selector": selector,
            "count": 0,
            "visible": False,
            "error": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="抓 Yahoo 登录页运行态截图/HTML dump")
    parser.add_argument("--service-id", type=int, default=0)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--direct", action="store_true", help="强制直连")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "release" / "yahoo_probe"))
    parser.add_argument("--wait-ms", type=int, default=2500)
    args = parser.parse_args()

    initialize_database()
    service = resolve_service(args.service_id)
    config = dict(service.config or {})
    if args.direct:
        config["proxy_url"] = None
    elif args.proxy:
        config["proxy_url"] = args.proxy

    yahoo = YahooMailService(config)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    requests_log: List[Dict[str, Any]] = []
    responses_log: List[Dict[str, Any]] = []

    email_selectors = [
        '#login-username',
        'input[name="username"]',
        'input[type="email"]',
        'input[autocomplete="username"]',
        'input[id*="username" i]',
        'input[name*="email" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱" i]',
        'input[aria-label*="email" i]',
        'input[aria-label*="邮箱" i]',
    ]
    next_selectors = [
        '#login-signin',
        'button[name="signin"]',
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Next")',
        'button:has-text("Sign in")',
        'button:has-text("登录")',
        'button:has-text("下一步")',
    ]

    playwright_ctx, launch_kwargs = yahoo._launch_browser(headless=False)
    pw = playwright_ctx.__enter__()
    browser = None
    try:
        browser = pw.chromium.launch(**launch_kwargs)
        if hasattr(yahoo, "_create_browser_context"):
            context = yahoo._create_browser_context(browser)
        else:
            context = browser.new_context(viewport={"width": 1440, "height": 960})
        page = context.new_page()
        page.set_default_timeout(45000)

        page.on("request", lambda req: requests_log.append({
            "method": req.method,
            "url": req.url,
            "resource_type": req.resource_type,
            "headers": dict(req.headers),
        }))
        page.on("response", lambda resp: responses_log.append({
            "status": resp.status,
            "url": resp.url,
            "headers": dict(resp.headers),
        }))

        goto_error = None
        try:
            page.goto(yahoo.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            goto_error = str(exc)

        page.wait_for_timeout(args.wait_ms)

        body_text = ""
        html = ""
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass
        try:
            html = page.content()
        except Exception:
            pass

        screenshot_path = outdir / "yahoo_login_runtime.png"
        html_path = outdir / "yahoo_login_runtime.html"
        json_path = outdir / "yahoo_login_runtime.json"
        text_path = outdir / "yahoo_login_runtime.txt"

        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        html_path.write_text(html or "", encoding="utf-8")
        text_path.write_text(body_text or "", encoding="utf-8")

        result = {
            "service_id": service.id,
            "service_name": service.name,
            "login_url": yahoo.LOGIN_URL,
            "final_url": page.url,
            "title": title,
            "goto_error": goto_error,
            "proxy_used": bool(launch_kwargs.get("proxy")),
            "proxy_masked": mask_proxy((launch_kwargs.get("proxy") or {}).get("server") if isinstance(launch_kwargs.get("proxy"), dict) else config.get("proxy_url")),
            "email_selectors": [inspect_selector(page, selector) for selector in email_selectors],
            "next_selectors": [inspect_selector(page, selector) for selector in next_selectors],
            "body_text_head": (body_text or "")[:4000],
            "request_count": len(requests_log),
            "response_count": len(responses_log),
            "requests": requests_log[:50],
            "responses": responses_log[:50],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "artifacts": {
                "screenshot": str(screenshot_path),
                "html": str(html_path),
                "text": str(text_path),
            },
        }
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright_ctx.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
