#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.init_db import initialize_database
from src.database.models import EmailService as EmailServiceModel
from src.database.models import Proxy as ProxyModel
from src.database.session import get_db
from src.services import EmailServiceType, YahooMailService
from src.web.routes.registration import _normalize_email_service_config


def mask_proxy(proxy_url: Optional[str]) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return "-"
    if "@" not in raw:
        return raw
    try:
        scheme, rest = raw.split("://", 1)
        credentials, host = rest.rsplit("@", 1)
        username = credentials.split(":", 1)[0] if ":" in credentials else "***"
        return f"{scheme}://{username}:***@{host}"
    except ValueError:
        return raw


def resolve_service(service_id: int) -> EmailServiceModel:
    with get_db() as db:
        query = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yahoo_mail",
            EmailServiceModel.enabled == True,
        )
        if service_id:
            query = query.filter(EmailServiceModel.id == int(service_id))
        service = query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).first()
    if not service:
        raise SystemExit("未找到可用 Yahoo 邮箱服务")
    return service


def load_proxies(include_disabled: bool = False) -> List[ProxyModel]:
    with get_db() as db:
        query = db.query(ProxyModel)
        if not include_disabled:
            query = query.filter(ProxyModel.enabled == True)
        return list(query.order_by(ProxyModel.is_default.desc(), ProxyModel.priority.asc(), ProxyModel.id.asc()).all())


def proxy_to_url(proxy: ProxyModel) -> str:
    return str(proxy.proxy_url or "").strip()


def build_context(service: EmailServiceModel, proxy_url: str, headless: bool) -> YahooMailService:
    config = _normalize_email_service_config(EmailServiceType.YAHOO_MAIL, service.config or {}, proxy_url)
    config["proxy_url"] = proxy_url
    config["headless"] = bool(headless)
    return YahooMailService(config)


def classify_runtime_block(text: str, url: str) -> Optional[str]:
    lower = str(text or "").lower()
    if "no longer be accessible from mainland china" in lower:
        return "region_block_cn"
    if "http error 407" in lower or "proxy authentication required" in lower:
        return "proxy_auth_407"
    if "err_empty_response" in lower:
        return "proxy_empty_response"
    if "this page isn’t working" in lower or "this page isn't working" in lower:
        return "browser_error_page"
    if "something went wrong" in lower and "/account/challenge/fail" in str(url or "").lower():
        if "different device" in lower:
            return "challenge_fail_different_device"
        return "challenge_fail"
    return None


def score_for_status(status: str) -> int:
    mapping = {
        "mailbox_ready": 100,
        "challenge_selector": 70,
        "password_page": 60,
        "login_page": 40,
        "challenge_fail_different_device": 10,
        "challenge_fail": 5,
        "region_block_cn": -20,
        "proxy_auth_407": -30,
        "proxy_empty_response": -40,
        "timeout": -50,
        "exception": -60,
    }
    return mapping.get(status, 0)


def probe_single_proxy(
    service: EmailServiceModel,
    proxy_url: str,
    *,
    headless: bool,
    output_dir: Path,
    password_wait_seconds: int,
) -> Dict[str, Any]:
    yahoo = build_context(service, proxy_url, headless=headless)
    result: Dict[str, Any] = {
        "proxy_masked": mask_proxy(proxy_url),
        "proxy_url": proxy_url,
        "status": "unknown",
        "detail": "",
        "score": 0,
        "final_url": "",
        "artifacts": {},
    }
    requests_log: List[Dict[str, Any]] = []
    responses_log: List[Dict[str, Any]] = []
    playwright_ctx, launch_kwargs = yahoo._launch_browser(headless=headless)
    pw = playwright_ctx.__enter__()
    browser = None
    page = None
    try:
        browser = pw.chromium.launch(**launch_kwargs)
        context = yahoo._create_browser_context(browser)
        page = context.new_page()
        page.set_default_timeout(max(30000, int(yahoo.config["timeout"]) * 1000))
        page.on("request", lambda req: requests_log.append({"method": req.method, "url": req.url, "resource_type": req.resource_type}))
        page.on("response", lambda resp: responses_log.append({"status": resp.status, "url": resp.url}))

        try:
            page.goto(yahoo.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            result["status"] = "exception"
            result["detail"] = str(exc)
            if page is not None:
                artifact = yahoo._dump_page_debug_artifacts(page, f"proxy_screen_exception_{int(time.time())}")
                if artifact:
                    result["artifacts"] = artifact
            return result

        page.wait_for_timeout(2500)
        page_text = yahoo._extract_visible_text(page)
        result["final_url"] = str(page.url or "")
        runtime_block = classify_runtime_block(page_text, page.url)
        if runtime_block:
            result["status"] = runtime_block
            result["detail"] = page_text[:500]
            artifact = yahoo._dump_page_debug_artifacts(page, f"proxy_screen_{runtime_block}")
            if artifact:
                result["artifacts"] = artifact
            return result

        if not yahoo._fill_first(
            page,
            [
                '#login-username',
                '#username',
                'input[name="username"]',
                'input[id*="username" i]',
                'input[autocomplete="username"]',
                'input[type="email"]',
            ],
            str(yahoo.config.get("parent_email") or ""),
            timeout_ms=4000,
        ):
            result["status"] = "login_page"
            result["detail"] = "登录页可访问，但未找到用户名输入框"
            artifact = yahoo._dump_page_debug_artifacts(page, "proxy_screen_login_page")
            if artifact:
                result["artifacts"] = artifact
            return result

        result["status"] = "login_page"
        result["detail"] = "登录页可访问且用户名输入框可见"
        if not yahoo._click_first(
            page,
            ['#login-signin', '#signin', 'button[name="signin"]', 'button[type="submit"]', 'button:has-text("Next")'],
            timeout_ms=4000,
        ):
            artifact = yahoo._dump_page_debug_artifacts(page, "proxy_screen_login_submit_missing")
            if artifact:
                result["artifacts"] = artifact
            return result

        page.wait_for_timeout(2500)
        page_text = yahoo._extract_visible_text(page)
        result["final_url"] = str(page.url or "")
        blocker = yahoo._detect_yahoo_blocker(page_text, page.url)
        if blocker:
            result["status"] = "challenge_selector" if "challenge-selector" in str(page.url or "").lower() else "challenge_fail"
            if "different device" in blocker.lower():
                result["status"] = "challenge_fail_different_device"
            result["detail"] = blocker
            artifact = yahoo._dump_page_debug_artifacts(page, f"proxy_screen_{result['status']}")
            if artifact:
                result["artifacts"] = artifact
            return result

        if not yahoo._fill_first(page, ['#login-passwd', 'input[name="password"]'], str(yahoo.config.get("parent_password") or ""), timeout_ms=4000):
            result["status"] = "password_page"
            result["detail"] = "用户名已通过，但未找到密码输入框"
            artifact = yahoo._dump_page_debug_artifacts(page, "proxy_screen_password_missing")
            if artifact:
                result["artifacts"] = artifact
            return result

        result["status"] = "password_page"
        result["detail"] = "可进入密码页"
        if not yahoo._click_first(page, ['#login-signin', 'button[name="verifyPassword"]', 'button[type="submit"]'], timeout_ms=4000):
            artifact = yahoo._dump_page_debug_artifacts(page, "proxy_screen_password_submit_missing")
            if artifact:
                result["artifacts"] = artifact
            return result

        deadline = time.time() + max(3, int(password_wait_seconds))
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            page_text = yahoo._extract_visible_text(page)
            result["final_url"] = str(page.url or "")
            blocker = yahoo._detect_yahoo_blocker(page_text, page.url)
            if blocker:
                result["status"] = "challenge_selector" if "challenge-selector" in str(page.url or "").lower() else "challenge_fail"
                if "different device" in blocker.lower():
                    result["status"] = "challenge_fail_different_device"
                result["detail"] = blocker
                artifact = yahoo._dump_page_debug_artifacts(page, f"proxy_screen_{result['status']}")
                if artifact:
                    result["artifacts"] = artifact
                break
            current_url = str(page.url or "").lower()
            if "mail.yahoo.com" in current_url or "/d/folders/" in current_url:
                result["status"] = "mailbox_ready"
                result["detail"] = "可进入 Yahoo 邮箱"
                artifact = yahoo._dump_page_debug_artifacts(page, "proxy_screen_mailbox_ready")
                if artifact:
                    result["artifacts"] = artifact
                break

        if result["status"] == "password_page":
            try:
                page.goto(yahoo.MAILBOX_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                page_text = yahoo._extract_visible_text(page)
                result["final_url"] = str(page.url or "")
                blocker = yahoo._detect_yahoo_blocker(page_text, page.url)
                if blocker:
                    result["status"] = "challenge_fail_different_device" if "different device" in blocker.lower() else "challenge_fail"
                    result["detail"] = blocker
                elif "mail.yahoo.com" in str(page.url or "").lower() or "/d/folders/" in str(page.url or "").lower():
                    result["status"] = "mailbox_ready"
                    result["detail"] = "可进入 Yahoo 邮箱"
            except Exception as exc:
                result["detail"] = f"{result['detail']} | mailbox_goto={exc}".strip(" |")
            artifact = yahoo._dump_page_debug_artifacts(page, f"proxy_screen_{result['status']}")
            if artifact:
                result["artifacts"] = artifact
    finally:
        result["score"] = score_for_status(result["status"])
        result["requests_tail"] = requests_log[-10:]
        result["responses_tail"] = responses_log[-10:]
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright_ctx.__exit__(None, None, None)
        except Exception:
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="批量筛选可登录 Yahoo 母号的代理出口")
    parser.add_argument("--service-id", type=int, default=2)
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--headless", action="store_true", help="默认 headful；传此参数改为 headless")
    parser.add_argument("--password-wait-seconds", type=int, default=12)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "release" / "yahoo_proxy_batch_screen.json"),
    )
    args = parser.parse_args()

    initialize_database()
    service = resolve_service(args.service_id)
    proxies = load_proxies(include_disabled=args.include_disabled)
    if not proxies:
        raise SystemExit("代理池为空")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for proxy in proxies:
        proxy_url = proxy_to_url(proxy)
        if not proxy_url:
            continue
        print(f"[PROBE] id={proxy.id} name={proxy.name} proxy={mask_proxy(proxy_url)}")
        item = probe_single_proxy(
            service,
            proxy_url,
            headless=args.headless,
            output_dir=output_path.parent,
            password_wait_seconds=args.password_wait_seconds,
        )
        item["proxy_id"] = proxy.id
        item["proxy_name"] = proxy.name
        item["enabled"] = bool(proxy.enabled)
        item["is_default"] = bool(proxy.is_default)
        results.append(item)
        print(json.dumps({
            "proxy_id": item["proxy_id"],
            "proxy_name": item["proxy_name"],
            "status": item["status"],
            "score": item["score"],
            "detail": item["detail"][:180],
            "final_url": item.get("final_url", ""),
        }, ensure_ascii=False))

    ranked = sorted(results, key=lambda x: (int(x.get("score", 0)), -int(x.get("proxy_id", 0))), reverse=True)
    payload = {
        "service_id": service.id,
        "service_name": service.name,
        "parent_email": str((service.config or {}).get("parent_email") or ""),
        "headless": bool(args.headless),
        "password_wait_seconds": int(args.password_wait_seconds),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ranked": ranked,
        "best": ranked[0] if ranked else None,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] result saved to {output_path}")
    if ranked:
        print(json.dumps({
            "best_proxy_id": ranked[0].get("proxy_id"),
            "best_proxy_name": ranked[0].get("proxy_name"),
            "best_status": ranked[0].get("status"),
            "best_score": ranked[0].get("score"),
            "best_proxy": ranked[0].get("proxy_masked"),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
