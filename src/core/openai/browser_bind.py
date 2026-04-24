"""
本地浏览器自动绑卡（参考 ABCard 的 checkout 自动化流程）。

说明：
- 仅做自动填写和提交，不做验证码绕过。
- 若检测到 challenge（如 hCaptcha/3DS），返回 need_user_action，由前端提示用户手动完成。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STRIPE_IFRAME_SELECTOR = 'iframe[name*="__privateStripeFrame"]'
_SUCCESS_TEXT_TOKENS = (
    "payment successful",
    "successfully subscribed",
    "welcome to",
    "订阅成功",
    "支付成功",
)
_FAIL_TEXT_PATTERNS = (
    ("card was declined", "银行卡被拒"),
    ("your card was declined", "银行卡被拒"),
    ("card_declined", "银行卡被拒"),
    ("insufficient funds", "余额不足"),
    ("expired card", "卡已过期"),
    ("authentication_required", "需要验证"),
    ("payment failed", "支付失败"),
    ("unable to process", "无法处理支付"),
    ("invalid card", "卡信息无效"),
)

_COOKIE_ATTR_NAMES = {
    "path",
    "domain",
    "expires",
    "max-age",
    "samesite",
    "secure",
    "httponly",
    "priority",
    "partitioned",
}
_COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SAFE_COOKIE_NAME_ALLOWLIST = {
    "__Secure-next-auth.session-token",
    "oai-did",
    "oai-client-auth-session",
    "__cf_bm",
    "cf_clearance",
}


def _parse_cookie_str(cookies_str: str, domain: str) -> List[dict]:
    cookies: List[dict] = []
    text = str(cookies_str or "").strip()
    if not text:
        return cookies
    for item in text.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        cookies.append(
            {
                "name": key,
                "value": value,
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def _sanitize_cookie_value(value: str) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    text = text.replace("\r", "").replace("\n", "")
    if ";" in text:
        text = text.split(";", 1)[0].strip()
    return text


def _parse_cookie_pairs(cookies_str: str) -> Dict[str, str]:
    text = str(cookies_str or "").strip()
    if not text:
        return {}

    result: Dict[str, str] = {}
    for item in text.split(";"):
        raw = str(item or "").strip()
        if not raw or "=" not in raw:
            continue
        name_raw, value_raw = raw.split("=", 1)
        name = str(name_raw or "").strip()
        value = _sanitize_cookie_value(value_raw)
        if not name or not value:
            continue
        if name.lower() in _COOKIE_ATTR_NAMES:
            continue
        if not _COOKIE_NAME_PATTERN.match(name):
            continue
        prev = str(result.get(name) or "")
        if (not prev) or (len(value) > len(prev)):
            result[name] = value
    return result


def _build_playwright_cookie_items(
    cookies_str: str,
    resolved_session: str,
    resolved_did: str,
) -> List[dict]:
    """
    构造可被 Playwright 接受的 cookie 列表：
    - 过滤属性项/非法名称
    - 仅注入安全白名单 cookie
    - __Host- 前缀不携带 domain（避免 Invalid cookie fields）
    """
    cookie_map = _parse_cookie_pairs(cookies_str)

    session = _sanitize_cookie_value(resolved_session)
    did = _sanitize_cookie_value(resolved_did)
    if session:
        cookie_map["__Secure-next-auth.session-token"] = session
    if did:
        cookie_map["oai-did"] = did

    items: List[dict] = []
    for name, value in cookie_map.items():
        if name not in _SAFE_COOKIE_NAME_ALLOWLIST and not name.startswith("__Host-"):
            continue
        if not value:
            continue

        if name.startswith("__Host-"):
            items.append(
                {
                    "name": name,
                    "value": value,
                    "url": "https://chatgpt.com/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
            continue

        items.append(
            {
                "name": name,
                "value": value,
                "domain": ".chatgpt.com",
                "path": "/",
                "httpOnly": bool(name in ("__Secure-next-auth.session-token", "oai-client-auth-session")),
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return items


def _add_cookies_resilient(context, cookies: List[dict], stage: str) -> None:
    if not cookies:
        return
    try:
        context.add_cookies(cookies)
        return
    except Exception as exc:
        names = [str(c.get("name") or "") for c in cookies]
        logger.warning("%s 注入完整 cookie 失败，将降级重试: %s | names=%s", stage, exc, ",".join(names[:12]))

    # 降级：仅注入最关键的 session + did
    minimal: List[dict] = []
    for item in cookies:
        name = str(item.get("name") or "")
        if name in ("__Secure-next-auth.session-token", "oai-did"):
            minimal.append(item)
    if not minimal:
        raise RuntimeError("cookie injection failed and no minimal cookies available")
    context.add_cookies(minimal)


def _extract_cookie_value(cookies_str: str, name: str) -> str:
    text = str(cookies_str or "")
    if not text:
        return ""
    prefix = f"{name}="
    for item in text.split(";"):
        item = item.strip()
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def _extract_session_token_from_cookie_text(cookies_str: str) -> str:
    text = str(cookies_str or "")
    if not text:
        return ""

    direct = _extract_cookie_value(text, "__Secure-next-auth.session-token")
    if direct:
        return direct

    chunks: Dict[int, str] = {}
    for raw in text.split(";"):
        item = str(raw or "").strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        name = str(key or "").strip()
        if not name.startswith("__Secure-next-auth.session-token."):
            continue
        try:
            idx = int(name.rsplit(".", 1)[-1])
        except Exception:
            continue
        chunks[idx] = str(value or "").strip()
    if chunks:
        return "".join(chunks[idx] for idx in sorted(chunks.keys()))
    return ""


def _extract_cookie_value_from_items(items: List[dict], name: str) -> str:
    for item in items or []:
        try:
            key = str(item.get("name") or "").strip()
            if key != name:
                continue
            return str(item.get("value") or "").strip()
        except Exception:
            continue
    return ""


def _extract_session_token_from_items(items: List[dict]) -> str:
    direct = _extract_cookie_value_from_items(items, "__Secure-next-auth.session-token")
    if direct:
        return direct
    chunks: Dict[int, str] = {}
    for item in items or []:
        try:
            name = str(item.get("name") or "").strip()
            if not name.startswith("__Secure-next-auth.session-token."):
                continue
            idx = int(name.rsplit(".", 1)[-1])
            chunks[idx] = str(item.get("value") or "").strip()
        except Exception:
            continue
    if chunks:
        return "".join(chunks[idx] for idx in sorted(chunks.keys()))
    return ""


def _normalize_exp_year(exp_year: str) -> str:
    digits = re.sub(r"\D", "", str(exp_year or ""))
    if not digits:
        return ""
    if len(digits) >= 2:
        return digits[-2:]
    return digits.zfill(2)


def _find_chrome_binary() -> str:
    env_path = str(os.getenv("CHROME_PATH") or "").strip()
    candidates = [
        env_path,
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for binary in candidates:
        value = str(binary or "").strip()
        if "*" in value:
            try:
                import glob
                matched = sorted(glob.glob(value))
            except Exception:
                matched = []
            for mv in matched:
                if mv and os.path.exists(mv):
                    return mv
            continue
        if value and os.path.exists(value):
            return value

    # fallback to PATH lookup
    import shutil as _which_lib

    for name in ("chrome", "google-chrome", "chromium-browser", "chromium", "msedge"):
        value = _which_lib.which(name)
        if value:
            return value
    return ""


def _simulate_human_behavior(page) -> None:
    try:
        for _ in range(random.randint(3, 6)):
            x = random.randint(100, 1100)
            y = random.randint(120, 760)
            page.mouse.move(x, y, steps=random.randint(8, 18))
            time.sleep(random.uniform(0.08, 0.25))
        page.mouse.wheel(0, random.randint(120, 260))
        time.sleep(random.uniform(0.2, 0.5))
        page.mouse.wheel(0, -random.randint(80, 180))
    except Exception:
        pass


def _try_click_hcaptcha_checkbox(page) -> bool:
    # 先从 frame URL 检测
    try:
        for frame in page.frames:
            url = str(getattr(frame, "url", "") or "").lower()
            if "hcaptcha" not in url:
                continue
            try:
                frame_el = frame.frame_element()
                box = frame_el.bounding_box() if frame_el else None
                if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 再从 DOM iframe 兜底
    try:
        candidates = page.query_selector_all('iframe[src*="hcaptcha"], iframe[title*="hCaptcha"], iframe[title*="security challenge"]')
        for iframe_el in candidates:
            box = iframe_el.bounding_box()
            if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
    except Exception:
        pass
    return False


def _try_click_challenge_continue(page) -> bool:
    """
    挑战页上的常见继续按钮兜底点击（3DS/hCaptcha）。
    """
    selectors = (
        'button:has-text("Verify")',
        'button:has-text("Continue")',
        'button:has-text("Complete")',
        'button:has-text("Authorize")',
        'button:has-text("确认")',
        'button:has-text("继续")',
        'button:has-text("完成")',
        '[data-testid*="continue"]',
    )
    for selector in selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False


def _detect_challenge_in_context(context, primary_page):
    """
    扫描当前 context 的所有页面，返回是否存在 challenge，以及命中的页面。
    """
    pages = []
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    if primary_page and primary_page not in pages:
        pages.append(primary_page)
    if not pages:
        return False, primary_page

    for pg in list(reversed(pages)):
        try:
            body = _extract_page_text(pg, 2000).lower()
            if _detect_challenge(pg, body):
                return True, pg
        except Exception:
            continue
    return False, primary_page


def _auto_bind_with_cdp_checkout(
    *,
    checkout_url: str,
    cookies_str: str,
    session_token: str,
    access_token: str,
    device_id: str,
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvc: str,
    billing_name: str,
    billing_country: str,
    billing_line1: str,
    billing_city: str,
    billing_state: str,
    billing_postal: str,
    proxy: Optional[str] = None,
    timeout_seconds: int = 180,
    post_submit_wait_seconds: int = 90,
    headless: bool = False,
) -> Dict[str, Any]:
    """
    ABCard 风格: 外部 Chrome + CDP 连接，降低自动化特征后执行 checkout 填卡。
    """
    checkout_url = str(checkout_url or "").strip()
    if not checkout_url:
        return {
            "success": False,
            "need_user_action": False,
            "error": "checkout_url empty",
            "stage": "cdp_input",
            "driver": "cdp",
            "fallback_recommended": True,
        }

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {
            "success": False,
            "need_user_action": True,
            "error": "playwright not installed",
            "stage": "cdp_bootstrap",
            "driver": "cdp",
            "fallback_recommended": True,
        }

    resolved_session = str(session_token or "").strip() or _extract_session_token_from_cookie_text(cookies_str)
    resolved_did = str(device_id or "").strip() or _extract_cookie_value(cookies_str, "oai-did")
    session_missing = not bool(resolved_session)
    if session_missing:
        logger.warning("CDP 自动绑卡缺少 session token，将继续尝试无会话模式")

    chrome_binary = _find_chrome_binary()
    if not chrome_binary:
        return {
            "success": False,
            "need_user_action": False,
            "error": "chrome binary not found",
            "stage": "cdp_chrome_not_found",
            "driver": "cdp",
            "fallback_recommended": True,
        }

    timeout_seconds = max(int(timeout_seconds), 60)
    post_submit_wait_seconds = max(int(post_submit_wait_seconds), 30)
    cdp_port = random.randint(9320, 9480)
    user_data_dir = tempfile.mkdtemp(prefix=f"codex-cdp-{cdp_port}-")
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    chrome_args = [
        chrome_binary,
        f"--remote-debugging-port={cdp_port}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--window-size=1366,900",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    if headless:
        chrome_args.extend(["--headless=new", "--disable-gpu"])
    else:
        # 无头设备上维持 WebGL 指纹可用
        chrome_args.extend([
            "--use-gl=angle",
            "--use-angle=swiftshader-webgl",
            "--enable-unsafe-swiftshader",
        ])
    if proxy:
        chrome_args.append(f"--proxy-server={proxy}")

    chrome_proc = None
    try:
        chrome_proc = subprocess.Popen(
            chrome_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        cdp_ready = False
        for _ in range(22):
            try:
                with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as resp:
                    data = json.loads(resp.read() or b"{}")
                    if data.get("Browser"):
                        cdp_ready = True
                        break
            except Exception:
                time.sleep(0.5)
        if not cdp_ready:
            return {
                "success": False,
                "need_user_action": False,
                "error": "chrome cdp port not responding",
                "stage": "cdp_unavailable",
                "driver": "cdp",
                "fallback_recommended": True,
            }

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = None
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )

                cookies = _build_playwright_cookie_items(
                    cookies_str=cookies_str,
                    resolved_session=resolved_session,
                    resolved_did=resolved_did,
                )
                if cookies:
                    _add_cookies_resilient(context, cookies, stage="CDP")

                page = context.new_page()
                page.set_default_timeout(timeout_seconds * 1000)

                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                cf_ok, cf_note = _wait_for_cloudflare(page, max_wait_seconds=min(timeout_seconds, 90))
                if not cf_ok:
                    return {
                        "success": False,
                        "need_user_action": True,
                        "pending": True,
                        "error": cf_note,
                        "stage": "cdp_cloudflare",
                        "driver": "cdp",
                        "current_url": page.url,
                        "fallback_recommended": False,
                    }

                page.goto(checkout_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(1.5)
                redirect_url = str(page.url or "")
                if "/checkout/" not in redirect_url:
                    return {
                        "success": False,
                        "need_user_action": False,
                        "pending": False,
                        "error": "checkout redirected, verify session" if not session_missing else "checkout redirected, missing session token",
                        "stage": "cdp_session_missing" if session_missing else "cdp_navigate_checkout",
                        "driver": "cdp",
                        "current_url": redirect_url,
                        "fallback_recommended": True,
                    }

                if not _wait_for_stripe_iframe(page, max_wait_seconds=min(timeout_seconds, 90)):
                    return {
                        "success": False,
                        "need_user_action": True,
                        "pending": True,
                        "error": "stripe iframe not loaded",
                        "stage": "cdp_wait_stripe",
                        "driver": "cdp",
                        "current_url": page.url,
                        "fallback_recommended": False,
                    }

                _simulate_human_behavior(page)

                ok, reason = _fill_payment_iframe(
                    page,
                    card_number=card_number,
                    exp_month=exp_month,
                    exp_year=exp_year,
                    cvc=cvc,
                )
                if not ok:
                    return {
                        "success": False,
                        "need_user_action": False,
                        "error": reason,
                        "stage": "cdp_fill_card",
                        "driver": "cdp",
                        "current_url": page.url,
                        "fallback_recommended": True,
                    }

                _fill_address_iframe(
                    page,
                    billing_name=billing_name,
                    billing_line1=billing_line1,
                    billing_zip=billing_postal,
                    billing_country=billing_country,
                    billing_city=billing_city,
                    billing_state=billing_state,
                )

                submit_btn = _find_submit_button(page)
                if not submit_btn:
                    return {
                        "success": False,
                        "need_user_action": True,
                        "pending": True,
                        "error": "submit button not found",
                        "stage": "cdp_find_submit",
                        "driver": "cdp",
                        "current_url": page.url,
                        "fallback_recommended": False,
                    }

                submit_btn.click()
                challenge_click_count = 0
                challenge_seen = False
                challenge_last_seen_at = 0.0
                resubmit_count = 0
                # challenge 场景常比普通提交更慢，给更长观察窗口，避免过早返回 need_user_action。
                deadline = time.monotonic() + max(post_submit_wait_seconds, 150)
                while time.monotonic() < deadline:
                    current_url = str(page.url or "")
                    body = _extract_page_text(page, 2500)
                    body_lower = body.lower()
                    url_lower = current_url.lower()

                    if any(token in url_lower for token in ("subscribed=true", "success", "/settings/subscription")):
                        return {
                            "success": True,
                            "need_user_action": False,
                            "stage": "cdp_confirmed",
                            "driver": "cdp",
                            "current_url": current_url,
                            "fallback_recommended": False,
                        }

                    if any(token in body_lower for token in _SUCCESS_TEXT_TOKENS):
                        return {
                            "success": True,
                            "need_user_action": False,
                            "stage": "cdp_confirmed_text",
                            "driver": "cdp",
                            "current_url": current_url,
                            "fallback_recommended": False,
                        }

                    for pattern, msg in _FAIL_TEXT_PATTERNS:
                        if pattern in body_lower:
                            return {
                                "success": False,
                                "need_user_action": False,
                                "error": msg,
                                "stage": "cdp_declined",
                                "driver": "cdp",
                                "current_url": current_url,
                                "fallback_recommended": False,
                            }

                    detected, challenge_page = _detect_challenge_in_context(context, page)
                    if detected:
                        challenge_seen = True
                        challenge_last_seen_at = time.monotonic()
                        clicked = False
                        if challenge_click_count < 6:
                            clicked = _try_click_hcaptcha_checkbox(challenge_page)
                            if clicked:
                                challenge_click_count += 1
                                logger.info(
                                    "CDP challenge 自动点击尝试: attempt=%s current_url=%s",
                                    challenge_click_count,
                                    str(getattr(challenge_page, "url", "") or current_url)[:120],
                                )
                        if not clicked:
                            clicked = _try_click_challenge_continue(challenge_page)
                        # challenge 出现后继续等待，不要立刻判失败。
                        time.sleep(3.0 if clicked else 2.0)
                        continue

                    if challenge_seen:
                        # challenge 消失后触发一次重提交流程，避免停在“已验证但未确认支付”。
                        elapsed = time.monotonic() - challenge_last_seen_at
                        if elapsed >= 4 and resubmit_count < 2:
                            try:
                                retry_btn = _find_submit_button(page)
                                if retry_btn and retry_btn.is_visible():
                                    retry_btn.click()
                                    resubmit_count += 1
                                    logger.info(
                                        "CDP challenge 后自动重提交流程: attempt=%s current_url=%s",
                                        resubmit_count,
                                        current_url[:120],
                                    )
                                    time.sleep(2.5)
                                    continue
                            except Exception:
                                pass

                    time.sleep(3)

                if challenge_seen:
                    return {
                        "success": False,
                        "need_user_action": True,
                        "pending": True,
                        "error": "challenge_detected",
                        "stage": "cdp_challenge",
                        "driver": "cdp",
                        "current_url": str(page.url or ""),
                        "fallback_recommended": False,
                    }

                return {
                    "success": False,
                    "need_user_action": True,
                    "pending": True,
                    "error": "payment_result_timeout",
                    "stage": "cdp_timeout",
                    "driver": "cdp",
                    "current_url": str(page.url or ""),
                    "fallback_recommended": False,
                }
            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        return {
            "success": False,
            "need_user_action": False,
            "error": f"cdp_exception: {exc}",
            "stage": "cdp_exception",
            "driver": "cdp",
            "fallback_recommended": True,
        }
    finally:
        if chrome_proc is not None:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=4)
            except Exception:
                try:
                    chrome_proc.kill()
                except Exception:
                    pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _wait_for_cloudflare(page, max_wait_seconds: int) -> Tuple[bool, str]:
    max_wait_seconds = max(int(max_wait_seconds), 15)
    rounds = max(max_wait_seconds // 3, 1)
    for _ in range(rounds):
        try:
            title = str(page.title() or "").lower()
            url = str(page.url or "").lower()
            body = str(page.evaluate("document.body ? document.body.innerText.slice(0, 500) : ''") or "").lower()
        except Exception:
            time.sleep(3)
            continue
        challenge = (
            "just a moment" in title
            or "请稍候" in title
            or "/cdn-cgi/challenge-platform" in url
            or "verify you are human" in body
        )
        if not challenge:
            return True, ""
        time.sleep(3)
    return False, "Cloudflare challenge not passed in time"


def _wait_for_stripe_iframe(page, max_wait_seconds: int) -> bool:
    max_wait_seconds = max(int(max_wait_seconds), 15)
    rounds = max(max_wait_seconds // 3, 1)
    for _ in range(rounds):
        try:
            iframe_elements = page.query_selector_all(_STRIPE_IFRAME_SELECTOR)
            visible = []
            for el in iframe_elements:
                box = el.bounding_box() or {}
                if box.get("height", 0) > 30 and box.get("width", 0) > 120:
                    visible.append(el)
            if visible:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _fill_payment_iframe(page, card_number: str, exp_month: str, exp_year: str, cvc: str) -> Tuple[bool, str]:
    card_number = re.sub(r"\D", "", str(card_number or ""))
    exp_month = re.sub(r"\D", "", str(exp_month or "")).zfill(2)[:2]
    exp_year = _normalize_exp_year(exp_year)
    cvc = re.sub(r"\D", "", str(cvc or ""))[:4]
    if not card_number or not exp_month or not exp_year or not cvc:
        return False, "card fields incomplete"

    exp_text = f"{exp_month}{exp_year}"
    iframe_elements = page.query_selector_all(_STRIPE_IFRAME_SELECTOR)
    payment_el = None
    backup_el = None

    for iframe_el in iframe_elements:
        try:
            box = iframe_el.bounding_box()
            if not box or box.get("height", 0) < 30:
                continue
            frame_obj = iframe_el.content_frame()
            if frame_obj and "elements-inner-payment" in str(frame_obj.url or ""):
                payment_el = iframe_el
                break
            if backup_el is None and box.get("height", 0) < 220:
                backup_el = iframe_el
        except Exception:
            continue
    if payment_el is None:
        payment_el = backup_el
    if payment_el is None:
        return False, "payment iframe not found"

    box = payment_el.bounding_box()
    if not box:
        return False, "payment iframe box unavailable"

    payment_el.scroll_into_view_if_needed()
    time.sleep(0.3)
    page.mouse.click(box["x"] + 80, box["y"] + 24)
    time.sleep(0.5)

    # Stripe Payment Element 常见流程：卡号 -> 到期日 -> CVC
    page.keyboard.type(card_number, delay=45)
    time.sleep(0.4)
    page.keyboard.type(exp_text, delay=45)
    time.sleep(0.3)
    page.keyboard.type(cvc, delay=45)
    time.sleep(0.4)
    return True, ""


def _fill_address_iframe(
    page,
    billing_name: str,
    billing_line1: str,
    billing_zip: str,
    billing_country: str,
    billing_city: str = "",
    billing_state: str = "",
) -> Tuple[bool, str]:
    iframe_elements = page.query_selector_all(_STRIPE_IFRAME_SELECTOR)
    address_el = None
    address_frame = None
    for iframe_el in iframe_elements:
        try:
            box = iframe_el.bounding_box()
            if not box or box.get("height", 0) < 30:
                continue
            frame_obj = iframe_el.content_frame()
            if frame_obj and "elements-inner-address" in str(frame_obj.url or ""):
                address_el = iframe_el
                address_frame = frame_obj
                break
        except Exception:
            continue

    if not address_el or not address_frame:
        return False, "address iframe not found"

    def _click_and_type(selector: str, value: str) -> bool:
        value = str(value or "").strip()
        if not value:
            return True
        try:
            address_el.scroll_into_view_if_needed()
            time.sleep(0.2)
            rect = address_frame.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""",
                selector,
            )
            box = address_el.bounding_box()
            if not rect or not box:
                return False
            page.mouse.click(box["x"] + rect["x"], box["y"] + rect["y"])
            time.sleep(0.2)
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(value, delay=35)
            time.sleep(0.2)
            return True
        except Exception:
            return False

    try:
        address_frame.evaluate(
            """(country) => {
                const sel = document.querySelector('select[name="country"]');
                if (!sel || !country) return;
                const nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value'
                ).set;
                nativeSet.call(sel, country);
                sel.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            str(billing_country or "US").upper(),
        )
        time.sleep(0.5)
    except Exception:
        pass

    _click_and_type('input[name="name"]', billing_name)
    _click_and_type('input[name="addressLine1"]', billing_line1)

    # 城市/州/邮编在部分布局下会在 addressLine1 之后出现，优先 JS 赋值兜底。
    try:
        address_frame.evaluate(
            """(data) => {
                function setInput(name, value) {
                    const el = document.querySelector('input[name="' + name + '"]');
                    if (!el || !value) return;
                    const nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSet.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
                function setSelect(name, value) {
                    const el = document.querySelector('select[name="' + name + '"]');
                    if (!el || !value) return;
                    const nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLSelectElement.prototype, 'value'
                    ).set;
                    nativeSet.call(el, value);
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
                if (data.city) setInput('locality', data.city);
                if (data.state) setSelect('administrativeArea', data.state);
                if (data.postal) setInput('postalCode', data.postal);
            }""",
            {"city": billing_city, "state": billing_state, "postal": billing_zip},
        )
    except Exception:
        # 忽略地址补充失败，避免硬中断
        pass
    return True, ""


def _find_submit_button(page):
    selectors = (
        '[data-testid="checkout-submit"]',
        'button[type="submit"]',
        'button:has-text("Subscribe")',
        'button:has-text("Pay")',
        'button:has-text("Confirm")',
        'button:has-text("订阅")',
        'button:has-text("支付")',
        'button:has-text("确认")',
    )
    for selector in selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _extract_page_text(page, max_len: int = 2000) -> str:
    try:
        text = str(page.evaluate("document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""
    return text[:max_len]


def _detect_challenge(page, page_text_lower: str) -> bool:
    challenge_tokens = (
        "hcaptcha",
        "3d secure",
        "requires_action",
        "authentication required",
        "verify you are human",
        "complete verification",
        "challenge",
    )
    if any(token in page_text_lower for token in challenge_tokens):
        return True
    try:
        for frame in page.frames:
            url = str(getattr(frame, "url", "") or "").lower()
            if any(token in url for token in ("hcaptcha", "3ds", "challenge")):
                return True
    except Exception:
        pass
    return False


def auto_bind_checkout_with_playwright(
    *,
    checkout_url: str,
    cookies_str: str,
    session_token: str,
    access_token: str,
    device_id: str,
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvc: str,
    billing_name: str,
    billing_country: str,
    billing_line1: str,
    billing_city: str,
    billing_state: str,
    billing_postal: str,
    proxy: Optional[str] = None,
    timeout_seconds: int = 180,
    post_submit_wait_seconds: int = 90,
    headless: bool = False,
) -> Dict[str, Any]:
    """
    在 chatgpt checkout 页面执行自动填卡。
    返回结构：
    - success=True: 浏览器提交流程出现成功信号
    - success=False + need_user_action=True: 需要人工继续（challenge/超时）
    - success=False + need_user_action=False: 明确失败
    """
    checkout_url = str(checkout_url or "").strip()
    if not checkout_url:
        return {"success": False, "need_user_action": False, "error": "checkout_url empty"}

    # 优先使用 ABCard 风格 CDP 模式，降低自动化痕迹；
    # 若判定为“环境性失败”再回退到标准 Playwright 模式。
    cdp_result = _auto_bind_with_cdp_checkout(
        checkout_url=checkout_url,
        cookies_str=cookies_str,
        session_token=session_token,
        access_token=access_token,
        device_id=device_id,
        card_number=card_number,
        exp_month=exp_month,
        exp_year=exp_year,
        cvc=cvc,
        billing_name=billing_name,
        billing_country=billing_country,
        billing_line1=billing_line1,
        billing_city=billing_city,
        billing_state=billing_state,
        billing_postal=billing_postal,
        proxy=proxy,
        timeout_seconds=timeout_seconds,
        post_submit_wait_seconds=post_submit_wait_seconds,
        headless=headless,
    )
    if cdp_result.get("success"):
        return cdp_result
    if not bool(cdp_result.get("fallback_recommended", False)):
        return cdp_result
    logger.warning(
        "CDP 自动绑卡回退到标准 Playwright: stage=%s error=%s",
        cdp_result.get("stage"),
        cdp_result.get("error"),
    )

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {
            "success": False,
            "need_user_action": True,
            "error": "playwright not installed (pip install playwright && playwright install chromium)",
            "stage": "bootstrap",
        }

    launch_kwargs: Dict[str, Any] = {"headless": bool(headless)}
    proxy_server = str(proxy or "").strip()
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}

    timeout_seconds = max(int(timeout_seconds), 60)
    post_submit_wait_seconds = max(int(post_submit_wait_seconds), 30)

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )

            # 注入账号 cookie（优先使用账号管理中已保存 cookies）
            session_token = str(session_token or "").strip() or _extract_session_token_from_cookie_text(cookies_str)
            access_token = str(access_token or "").strip()
            device_id = str(device_id or "").strip() or _extract_cookie_value(cookies_str, "oai-did")
            cookies = _build_playwright_cookie_items(
                cookies_str=cookies_str,
                resolved_session=session_token,
                resolved_did=device_id,
            )
            if cookies:
                _add_cookies_resilient(context, cookies, stage="Playwright")

            page = context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)

            # 先热身 chatgpt 首页，减少 checkout 重定向概率
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            cf_ok, cf_note = _wait_for_cloudflare(page, max_wait_seconds=min(timeout_seconds, 90))
            if not cf_ok:
                return {
                    "success": False,
                    "need_user_action": True,
                    "error": cf_note,
                    "stage": "cloudflare",
                    "current_url": page.url,
                }

            page.goto(checkout_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(1.5)
            redirect_url = str(page.url or "")
            if "/checkout/" not in redirect_url:
                body = _extract_page_text(page, 600)
                return {
                    "success": False,
                    "need_user_action": True,
                    "error": "checkout page redirected, please verify login/session",
                    "stage": "navigate_checkout",
                    "current_url": redirect_url,
                    "body_preview": body,
                }

            if not _wait_for_stripe_iframe(page, max_wait_seconds=min(timeout_seconds, 90)):
                return {
                    "success": False,
                    "need_user_action": True,
                    "error": "stripe payment iframe not loaded",
                    "stage": "wait_stripe",
                    "current_url": page.url,
                }

            ok, reason = _fill_payment_iframe(
                page,
                card_number=card_number,
                exp_month=exp_month,
                exp_year=exp_year,
                cvc=cvc,
            )
            if not ok:
                return {"success": False, "need_user_action": False, "error": reason, "stage": "fill_card"}

            _fill_address_iframe(
                page,
                billing_name=billing_name,
                billing_line1=billing_line1,
                billing_zip=billing_postal,
                billing_country=billing_country,
                billing_city=billing_city,
                billing_state=billing_state,
            )

            submit_btn = _find_submit_button(page)
            if not submit_btn:
                return {
                    "success": False,
                    "need_user_action": True,
                    "error": "submit button not found",
                    "stage": "find_submit",
                    "current_url": page.url,
                }
            submit_btn.click()

            challenge_seen = False
            challenge_click_count = 0
            challenge_last_seen_at = 0.0
            resubmit_count = 0
            deadline = time.monotonic() + max(post_submit_wait_seconds, 150)
            while time.monotonic() < deadline:
                current_url = str(page.url or "")
                body = _extract_page_text(page, 2500)
                body_lower = body.lower()
                url_lower = current_url.lower()

                if any(token in url_lower for token in ("subscribed=true", "success", "/settings/subscription")):
                    return {"success": True, "need_user_action": False, "stage": "confirmed", "current_url": current_url}

                if any(token in body_lower for token in _SUCCESS_TEXT_TOKENS):
                    return {"success": True, "need_user_action": False, "stage": "confirmed_text", "current_url": current_url}

                for pattern, msg in _FAIL_TEXT_PATTERNS:
                    if pattern in body_lower:
                        return {
                            "success": False,
                            "need_user_action": False,
                            "error": msg,
                            "stage": "declined",
                            "current_url": current_url,
                        }

                detected, challenge_page = _detect_challenge_in_context(context, page)
                if detected:
                    challenge_seen = True
                    challenge_last_seen_at = time.monotonic()
                    clicked = False
                    if challenge_click_count < 6:
                        clicked = _try_click_hcaptcha_checkbox(challenge_page)
                        if clicked:
                            challenge_click_count += 1
                            logger.info(
                                "Playwright challenge 自动点击尝试: attempt=%s current_url=%s",
                                challenge_click_count,
                                str(getattr(challenge_page, "url", "") or current_url)[:120],
                            )
                    if not clicked:
                        clicked = _try_click_challenge_continue(challenge_page)
                    time.sleep(3.0 if clicked else 2.0)
                    continue

                if challenge_seen:
                    elapsed = time.monotonic() - challenge_last_seen_at
                    if elapsed >= 4 and resubmit_count < 2:
                        try:
                            retry_btn = _find_submit_button(page)
                            if retry_btn and retry_btn.is_visible():
                                retry_btn.click()
                                resubmit_count += 1
                                logger.info(
                                    "Playwright challenge 后自动重提交流程: attempt=%s current_url=%s",
                                    resubmit_count,
                                    current_url[:120],
                                )
                                time.sleep(2.5)
                                continue
                        except Exception:
                            pass

                time.sleep(3)

            if challenge_seen:
                return {
                    "success": False,
                    "need_user_action": True,
                    "pending": True,
                    "error": "challenge_detected",
                    "stage": "challenge",
                    "current_url": str(page.url or ""),
                }

            return {
                "success": False,
                "need_user_action": True,
                "pending": True,
                "error": "payment_result_timeout",
                "stage": "timeout",
                "current_url": str(page.url or ""),
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass
