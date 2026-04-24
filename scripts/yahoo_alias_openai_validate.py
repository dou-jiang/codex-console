#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.dynamic_proxy import get_proxy_url_for_task
from src.core.register import RegistrationEngine
from src.core.http_client import HTTPClient
from src.database import crud
from src.database.init_db import initialize_database
from src.database.models import EmailService as EmailServiceModel
from src.database.session import get_db
from src.services import EmailServiceFactory, EmailServiceType
from src.web.routes.registration import _normalize_email_service_config, _validate_yahoo_mail_config


def safe_print(message: str = "", *, stream = sys.stdout) -> None:
    text = str(message)
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        stream.write(text + ("" if text.endswith("\n") else "\n"))
    except UnicodeEncodeError:
        sanitized = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(sanitized + ("" if sanitized.endswith("\n") else "\n"))
    stream.flush()


def mask_proxy(proxy_url: Optional[str]) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return "-"
    if "@" not in raw:
        return raw
    try:
        scheme, rest = raw.split("://", 1)
        credentials, host = rest.rsplit("@", 1)
        if ":" in credentials:
            username = credentials.split(":", 1)[0]
            return f"{scheme}://{username}:***@{host}"
        return f"{scheme}://***@{host}"
    except ValueError:
        return raw


def _roxy_request(
    api_host: str,
    token: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    base = str(api_host or "http://127.0.0.1:50000").rstrip("/")
    url = f"{base}{path}"
    upper_method = str(method or "GET").upper()
    headers = {
        "Accept": "application/json",
        "token": str(token or "").strip(),
    }
    data: Optional[bytes] = None

    filtered_payload = {
        key: value
        for key, value in (payload or {}).items()
        if value not in (None, "", [], {})
    }
    if upper_method == "GET":
        if filtered_payload:
            url = f"{url}?{urlencode(filtered_payload, doseq=True)}"
    else:
        headers["Content-Type"] = "application/json"
        data = json.dumps(filtered_payload, ensure_ascii=False).encode("utf-8")

    request = Request(url, data=data, headers=headers, method=upper_method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Roxy API {upper_method} {path} HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Roxy API {upper_method} {path} connect failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Roxy API {upper_method} {path} failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Roxy API {upper_method} {path} returned non-JSON: {raw[:300]}") from exc

    code = parsed.get("code")
    if code not in (None, 0, "0"):
        raise RuntimeError(f"Roxy API {upper_method} {path} returned code={code}: {json.dumps(parsed, ensure_ascii=False)}")
    return parsed


def _walk_scalars(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_scalars(value)
        return
    if isinstance(node, list):
        for value in node:
            yield from _walk_scalars(value)
        return
    yield node


def _coerce_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("rows")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, dict)]
    return []


def _match_dir_id(row: Dict[str, Any], dir_id: str) -> bool:
    target = str(dir_id or "").strip()
    if not target:
        return False
    for value in _walk_scalars(row):
        if str(value or "").strip() == target:
            return True
    return False


def resolve_roxy_workspace_id(
    api_host: str,
    token: str,
    dir_id: str,
) -> int:
    workspace_resp = _roxy_request(
        api_host,
        token,
        "GET",
        "/browser/workspace",
        {"page_index": 1, "page_size": 100},
    )
    workspace_rows = _coerce_rows(workspace_resp)
    if not workspace_rows:
        raise RuntimeError("Roxy /browser/workspace 返回为空，无法推断 workspaceId")

    for workspace in workspace_rows:
        workspace_id = workspace.get("id") or workspace.get("workspaceId")
        if workspace_id in (None, ""):
            continue
        browser_resp = _roxy_request(
            api_host,
            token,
            "GET",
            "/browser/list_v3",
            {"workspaceId": workspace_id, "page_index": 1, "page_size": 100},
        )
        for row in _coerce_rows(browser_resp):
            if _match_dir_id(row, dir_id):
                return int(workspace_id)
    raise RuntimeError(f"未在任何 Roxy workspace 中找到窗口 dirId={dir_id}")


def open_roxy_window(
    api_host: str,
    token: str,
    workspace_id: int,
    dir_id: str,
    *,
    headless: bool,
    force_open: bool,
    args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "workspaceId": int(workspace_id),
        "dirId": str(dir_id),
        "forceOpen": bool(force_open),
        "headless": bool(headless),
    }
    if args:
        payload["args"] = [str(item) for item in args if str(item).strip()]
    response = _roxy_request(api_host, token, "POST", "/browser/open", payload)
    data = response.get("data") or {}
    ws = str(data.get("ws") or "").strip()
    if not ws:
        raise RuntimeError(f"Roxy /browser/open 未返回 ws 字段: {json.dumps(response, ensure_ascii=False)}")
    return data


def _select_roxy_mail_page(browser):
    preferred_hosts = ("mail.yahoo.com", "login.yahoo.com")
    contexts = list(getattr(browser, "contexts", []) or [])
    if not contexts:
        raise RuntimeError("Playwright connect_over_cdp 未返回任何 browser context")

    for context in contexts:
        for page in context.pages:
            current_url = str(getattr(page, "url", "") or "")
            if any(host in current_url for host in preferred_hosts):
                return context, page

    for context in contexts:
        if context.pages:
            return context, context.pages[0]

    context = contexts[0]
    return context, context.new_page()


def _normalize_roxy_ws_endpoint(api_host: str, ws_endpoint: str) -> str:
    raw_ws = str(ws_endpoint or "").strip()
    if not raw_ws:
        return raw_ws
    ws_parsed = urlparse(raw_ws)
    api_parsed = urlparse(str(api_host or "").strip() or "http://127.0.0.1:50000")
    if not ws_parsed.scheme or not ws_parsed.netloc:
        return raw_ws
    if ws_parsed.hostname not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return raw_ws
    if not api_parsed.hostname:
        return raw_ws
    if api_parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return raw_ws

    username = ws_parsed.username or ""
    password = ws_parsed.password or ""
    auth = ""
    if username:
        auth = username
        if password:
            auth += f":{password}"
        auth += "@"
    port = f":{ws_parsed.port}" if ws_parsed.port else ""
    rewritten = ws_parsed._replace(netloc=f"{auth}{api_parsed.hostname}{port}")
    return urlunparse(rewritten)


def _ensure_tcp_reachable(host: str, port: int, timeout: float = 5.0) -> None:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return
    except Exception as exc:
        raise RuntimeError(f"TCP connect failed to {host}:{port}: {exc}") from exc


def create_alias_via_cdp_endpoint(
    email_service,
    *,
    ws_endpoint: str,
    workspace_id: Optional[int],
    dir_id: str,
    route_trace: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    route_trace = route_trace if route_trace is not None else []
    parent_email, parent_password, parent_app_password = email_service._get_parent_seed_credentials()
    if not parent_email:
        raise RuntimeError("Yahoo 母号 alias 模式缺少 parent_email")

    normalized_ws = str(ws_endpoint or "").strip()
    parsed_ws = urlparse(normalized_ws)
    if parsed_ws.hostname and parsed_ws.port:
        _ensure_tcp_reachable(parsed_ws.hostname, int(parsed_ws.port), timeout=8.0)
    route_trace.append(
        {
            "stage": "roxy_browser_attach",
            "host": parsed_ws.hostname or "",
            "workspace_id": workspace_id,
            "dir_id": str(dir_id or ""),
            "ws_endpoint": normalized_ws,
        }
    )

    domain = str(email_service.config.get("domain") or "yahoo.com").strip().lower()

    sync_playwright = email_service._ensure_playwright()
    playwright_ctx = sync_playwright()
    pw = playwright_ctx.__enter__()
    try:
        browser = pw.chromium.connect_over_cdp(normalized_ws)
        _context, page = _select_roxy_mail_page(browser)
        page.set_default_timeout(max(30000, int(email_service.config.get("timeout") or 30) * 1000))
        page.goto(email_service.MAILBOX_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        visible_text = email_service._extract_visible_text(page)
        visible_lower = visible_text.lower()
        if "sign in" in visible_lower and "yahoo" in visible_lower and "mail.yahoo.com" not in str(page.url or ""):
            artifact = email_service._dump_page_debug_artifacts(page, "roxy_yahoo_not_logged_in")
            detail = "Roxy 指定窗口未保持 Yahoo 登录态，无法直接复用已登录母号窗口"
            if artifact:
                detail += f" | dump={artifact.get('json')}"
            raise RuntimeError(detail)

        created = email_service._create_and_verify_alias_on_page(
            page,
            domain=domain,
            max_attempts=max(2, int(email_service.config.get("max_retries") or 3)),
        )
        alias_address = str(created.get("alias_email") or "").strip().lower()
        nickname = str(created.get("nickname") or "").strip().lower()
        keyword = str(created.get("keyword") or "").strip().lower()
        profile = created.get("profile") or {}

        account_info = {
            "email": alias_address,
            "service_id": alias_address,
            "id": alias_address,
            "parent_email": parent_email,
            "mailbox_owner_email": parent_email,
            "mailbox_owner_password": parent_password,
            "mailbox_owner_app_password": parent_app_password,
            "alias_nickname": nickname,
            "alias_keyword": keyword,
            "profile": profile,
            "mode": "parent_alias",
            "roxy_ws_endpoint": normalized_ws,
            "prefer_roxy_otp": True,
            "created_at": __import__("time").time(),
        }
        email_service._cache_account(account_info)
        route_trace.append(
            {
                "stage": "roxy_yahoo_alias_created",
                "host": "mail.yahoo.com",
                "workspace_id": workspace_id,
                "dir_id": str(dir_id or ""),
                "alias_email": alias_address,
            }
        )
        return account_info
    finally:
        try:
            playwright_ctx.__exit__(None, None, None)
        except Exception:
            pass


def create_alias_via_roxy_window(
    email_service,
    *,
    api_host: str,
    token: str,
    dir_id: str,
    workspace_id: Optional[int],
    headless: bool,
    force_open: bool,
    args: Optional[List[str]],
    route_trace: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    route_trace = route_trace if route_trace is not None else []
    parent_email, parent_password, parent_app_password = email_service._get_parent_seed_credentials()
    if not parent_email:
        raise RuntimeError("Yahoo 母号 alias 模式缺少 parent_email")

    resolved_workspace_id = int(workspace_id or resolve_roxy_workspace_id(api_host, token, dir_id))
    open_data = open_roxy_window(
        api_host=api_host,
        token=token,
        workspace_id=resolved_workspace_id,
        dir_id=dir_id,
        headless=headless,
        force_open=force_open,
        args=args,
    )
    original_ws = str(open_data.get("ws") or "").strip()
    normalized_ws = _normalize_roxy_ws_endpoint(api_host, original_ws)
    open_data["ws"] = normalized_ws
    parsed_ws = urlparse(normalized_ws)
    if parsed_ws.hostname and parsed_ws.port:
        _ensure_tcp_reachable(parsed_ws.hostname, int(parsed_ws.port), timeout=8.0)
    route_trace.append(
        {
            "stage": "roxy_browser_open",
            "host": str(api_host or "http://127.0.0.1:50000"),
            "workspace_id": resolved_workspace_id,
            "dir_id": str(dir_id),
            "headless": bool(headless),
            "force_open": bool(force_open),
            "ws_present": bool(open_data.get("ws")),
            "ws_endpoint": normalized_ws,
        }
    )
    account_info = create_alias_via_cdp_endpoint(
        email_service,
        ws_endpoint=str(open_data["ws"]),
        workspace_id=resolved_workspace_id,
        dir_id=str(dir_id),
        route_trace=route_trace,
    )
    return resolved_workspace_id, open_data, account_info


def resolve_active_proxy() -> Tuple[Optional[str], str, Optional[int]]:
    with get_db() as db:
        proxy = crud.get_random_proxy(db)
        if proxy:
            return proxy.proxy_url, f"proxy_pool:{proxy.id}", proxy.id
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url, "dynamic_or_static", None
    return None, "none", None


def resolve_yahoo_service(service_id: Optional[int] = None) -> EmailServiceModel:
    with get_db() as db:
        query = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yahoo_mail",
            EmailServiceModel.enabled == True,
        )
        if service_id:
            query = query.filter(EmailServiceModel.id == int(service_id))
            service = query.first()
        else:
            service = query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).first()
    if not service:
        raise RuntimeError("未找到启用中的 Yahoo 邮箱服务，请先在邮箱服务页面配置并启用 Yahoo 服务")
    return service


def detect_markers(result, engine_logs: list[str]) -> Dict[str, bool]:
    text = "\n".join(engine_logs + [str(getattr(result, "error_message", "") or "")]).lower()
    return {
        "add_phone_detected": ("auth.openai.com/add-phone" in text) or ("add-phone" in text) or ("add_phone" in text),
        "about_you_detected": "auth.openai.com/about-you" in text or "about-you" in text,
        "challenge_detected": "challenge" in text,
        "create_account_password_detected": "create_account_password" in text,
        "otp_timeout_detected": ("等待验证码超时" in text) or ("verification code" in text and "timeout" in text),
    }


def build_output(result, service: EmailServiceModel, proxy_url: Optional[str], proxy_source: str, selected_config: Dict[str, Any], engine_logs: list[str], save_db: bool, route_trace: List[Dict[str, Any]], split_policy: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": bool(result.success),
        "email": result.email,
        "error_message": result.error_message,
        "account_id": result.account_id,
        "workspace_id": result.workspace_id,
        "session_token_present": bool(result.session_token),
        "access_token_present": bool(result.access_token),
        "save_db_requested": bool(save_db),
        "proxy_source": proxy_source,
        "proxy_masked": mask_proxy(proxy_url),
        "email_service_id": service.id,
        "email_service_name": service.name,
        "email_service_type": service.service_type,
        "headless": bool(selected_config.get("headless")),
        "parent_email": str(selected_config.get("parent_email") or ""),
        "has_parent_password": bool(selected_config.get("parent_password")),
        "has_parent_app_password": bool(selected_config.get("parent_app_password")),
        "markers": detect_markers(result, engine_logs),
        "metadata": result.metadata or {},
        "split_policy": split_policy,
        "route_trace": route_trace,
        "log_count": len(engine_logs),
        "logs": engine_logs,
        "generated_at": datetime.now().isoformat(),
    }


def build_local_dry_run_output(
    service: EmailServiceModel,
    proxy_url: Optional[str],
    proxy_source: str,
    selected_config: Dict[str, Any],
    split_policy: Dict[str, Any],
) -> Dict[str, Any]:
    yahoo_proxy_url = str(selected_config.get("proxy_url") or "").strip() or None
    route_trace = [
        {
            "stage": "http_request",
            "method": "GET",
            "url": "https://auth.openai.com/oauth/authorize?local_dry_run=1",
            "host": "auth.openai.com",
            "via_proxy": bool(proxy_url),
            "proxy_masked": mask_proxy(proxy_url),
            "simulated": True,
        },
        {
            "stage": "yahoo_browser_launch",
            "host": "login.yahoo.com",
            "via_proxy": bool(yahoo_proxy_url),
            "proxy_masked": mask_proxy(yahoo_proxy_url),
            "strategy": "default_proxy" if yahoo_proxy_url else "direct",
            "simulated": True,
        },
    ]
    return {
        "success": True,
        "local_dry_run": True,
        "check_only": False,
        "email": "",
        "error_message": "",
        "account_id": "",
        "workspace_id": "",
        "session_token_present": False,
        "access_token_present": False,
        "save_db_requested": False,
        "proxy_source": proxy_source,
        "proxy_masked": mask_proxy(proxy_url),
        "email_service_id": service.id,
        "email_service_name": service.name,
        "email_service_type": service.service_type,
        "headless": bool(selected_config.get("headless")),
        "parent_email": str(selected_config.get("parent_email") or ""),
        "has_parent_password": bool(selected_config.get("parent_password")),
        "has_parent_app_password": bool(selected_config.get("parent_app_password")),
        "markers": {
            "add_phone_detected": False,
            "about_you_detected": False,
            "challenge_detected": False,
            "create_account_password_detected": False,
            "otp_timeout_detected": False,
        },
        "metadata": {"mode": "local_dry_run"},
        "split_policy": split_policy,
        "route_trace": route_trace,
        "log_count": 1,
        "logs": ["[LOCAL-DRY-RUN] 未执行外部网络请求，仅验证配置解析与路由分流策略"],
        "generated_at": datetime.now().isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Yahoo 母号 alias -> OpenAI 注册最小验证脚本（绕过前端）")
    parser.add_argument("--service-id", type=int, default=0, help="指定 Yahoo 邮箱服务 ID；默认取第一个启用服务")
    parser.add_argument("--proxy", default="", help="手工指定 OpenAI 代理 URL；默认读取当前生效代理")
    parser.add_argument("--yahoo-direct", dest="yahoo_direct", action="store_true", help="Yahoo 相关站点强制直连（默认开启）")
    parser.add_argument("--yahoo-via-proxy", dest="yahoo_direct", action="store_false", help="Yahoo 相关站点也走代理（调试用）")
    parser.add_argument("--headless", action="store_true", help="强制无头模式（默认验证建议有头）")
    parser.add_argument("--roxy-api-host", default="http://127.0.0.1:50000", help="RoxyBrowser API host")
    parser.add_argument("--roxy-token", default="", help="RoxyBrowser API token")
    parser.add_argument("--roxy-open-dir-id", default="", help="复用/打开已登录 Yahoo 母号窗口的 dirId")
    parser.add_argument("--roxy-ws-endpoint", default="", help="直接复用已转发的 CDP ws://.../devtools/browser/... 端点")
    parser.add_argument("--roxy-workspace-id", type=int, default=0, help="可选：已知的 Roxy workspaceId")
    parser.add_argument("--roxy-force-open", action="store_true", help="Roxy /browser/open 时启用 forceOpen=true")
    parser.add_argument(
        "--roxy-arg",
        action="append",
        default=[],
        help="附加传给 Roxy /browser/open 的浏览器启动参数，可重复传入",
    )
    parser.add_argument("--skip-openai-preflight", action="store_true", help="跳过 OpenAI 代理预检请求")
    parser.add_argument("--check-only", action="store_true", help="仅校验服务/代理配置，不真正发起注册")
    parser.add_argument("--local-dry-run", action="store_true", help="仅本地模拟 Yahoo/OpenAI 分流，不执行任何外部网络请求")
    parser.add_argument("--save-db", action="store_true", help="注册成功后写入 accounts 表")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "release" / "yahoo_alias_openai_validate.json"),
        help="验证结果 JSON 输出路径",
    )
    parser.set_defaults(yahoo_direct=True)
    args = parser.parse_args()

    initialize_database()

    proxy_url = str(args.proxy or "").strip() or None
    proxy_source = "manual" if proxy_url else "auto"
    proxy_id: Optional[int] = None
    if not proxy_url:
        proxy_url, proxy_source, proxy_id = resolve_active_proxy()
    if not proxy_url:
        raise SystemExit("[ERR] 未找到生效代理（代理池 / 动态代理 / 静态代理均为空），当前脚本按要求不会直连执行")

    service = resolve_yahoo_service(args.service_id or None)
    service_type = EmailServiceType.YAHOO_MAIL
    yahoo_proxy_url = None if args.yahoo_direct else proxy_url
    config = _normalize_email_service_config(service_type, service.config or {}, yahoo_proxy_url)
    config["proxy_url"] = yahoo_proxy_url
    # 最小验证优先给出可观察流程；不显式传 --headless 时默认改为有头。
    config["headless"] = bool(args.headless)
    _validate_yahoo_mail_config(config)

    route_trace: List[Dict[str, Any]] = []
    split_policy = {
        "yahoo_strategy": "direct" if args.yahoo_direct else "default_proxy",
        "yahoo_proxy_masked": mask_proxy(yahoo_proxy_url),
        "openai_strategy": "default_proxy",
        "openai_proxy_masked": mask_proxy(proxy_url),
    }

    safe_print("[INFO] Selected Yahoo service: " + json.dumps({
        "id": service.id,
        "name": service.name,
        "service_type": service.service_type,
        "parent_email": str(config.get("parent_email") or ""),
        "headless": bool(config.get("headless")),
    }, ensure_ascii=False))
    safe_print(f"[INFO] OpenAI proxy: {mask_proxy(proxy_url)} source={proxy_source}")
    safe_print(f"[INFO] Yahoo network strategy: {'direct' if args.yahoo_direct else 'default_proxy'} proxy={mask_proxy(yahoo_proxy_url)}")
    if args.roxy_open_dir_id or args.roxy_ws_endpoint:
        safe_print(
            "[INFO] Roxy integration enabled: "
            + json.dumps(
                {
                    "api_host": args.roxy_api_host,
                    "dir_id": args.roxy_open_dir_id,
                    "ws_endpoint": str(args.roxy_ws_endpoint or ""),
                    "workspace_id": args.roxy_workspace_id or None,
                    "headless": bool(args.headless),
                    "force_open": bool(args.roxy_force_open),
                    "extra_args": list(args.roxy_arg or []),
                },
                ensure_ascii=False,
            )
        )

    if args.local_dry_run:
        payload = build_local_dry_run_output(
            service=service,
            proxy_url=proxy_url,
            proxy_source=proxy_source,
            selected_config=config,
            split_policy=split_policy,
        )
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"[OK] local-dry-run passed, output saved to {output_path}")
        return 0

    if args.check_only:
        payload = {
            "success": True,
            "check_only": True,
            "email_service_id": service.id,
            "email_service_name": service.name,
            "proxy_source": proxy_source,
            "proxy_masked": mask_proxy(proxy_url),
            "split_policy": split_policy,
            "headless": bool(config.get("headless")),
            "generated_at": datetime.now().isoformat(),
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"[OK] check-only passed, output saved to {output_path}")
        return 0

    email_service = EmailServiceFactory.create(service_type, config)
    if hasattr(email_service, "config") and isinstance(getattr(email_service, "config"), dict):
        if args.roxy_ws_endpoint:
            email_service.config["roxy_ws_endpoint"] = _normalize_roxy_ws_endpoint(
                args.roxy_api_host,
                str(args.roxy_ws_endpoint),
            )
            email_service.config["prefer_roxy_otp"] = True
    task_uuid = str(uuid.uuid4())
    precreated_yahoo_account: Optional[Dict[str, Any]] = None
    roxy_open_result: Optional[Dict[str, Any]] = None
    roxy_workspace_id: Optional[int] = None
    original_create_email = getattr(email_service, "create_email", None)

    if args.roxy_ws_endpoint:
        precreated_yahoo_account = create_alias_via_cdp_endpoint(
            email_service=email_service,
            ws_endpoint=_normalize_roxy_ws_endpoint(args.roxy_api_host, str(args.roxy_ws_endpoint)),
            workspace_id=(args.roxy_workspace_id or None),
            dir_id="",
            route_trace=route_trace,
        )
        roxy_open_result = {"ws": str(args.roxy_ws_endpoint)}
        roxy_workspace_id = args.roxy_workspace_id or None
    elif args.roxy_open_dir_id:
        roxy_token = str(args.roxy_token or "").strip()
        if not roxy_token:
            raise SystemExit("[ERR] 使用 --roxy-open-dir-id 时必须同时提供 --roxy-token")
        if service_type != EmailServiceType.YAHOO_MAIL:
            raise SystemExit("[ERR] Roxy 复用已登录窗口当前仅用于 Yahoo alias 模式")
        roxy_workspace_id, roxy_open_result, precreated_yahoo_account = create_alias_via_roxy_window(
            email_service=email_service,
            api_host=args.roxy_api_host,
            token=roxy_token,
            dir_id=args.roxy_open_dir_id,
            workspace_id=(args.roxy_workspace_id or None),
            headless=bool(args.headless),
            force_open=bool(args.roxy_force_open),
            args=list(args.roxy_arg or []),
            route_trace=route_trace,
        )

    if precreated_yahoo_account and callable(original_create_email):
        def traced_create_email(*create_args, **create_kwargs):
            route_trace.append(
                {
                    "stage": "roxy_alias_reused",
                    "email": str(precreated_yahoo_account.get("email") or ""),
                    "workspace_id": roxy_workspace_id,
                    "dir_id": str(args.roxy_open_dir_id or ""),
                    "ws_endpoint": str(args.roxy_ws_endpoint or ""),
                }
            )
            return precreated_yahoo_account
        email_service.create_email = traced_create_email  # type: ignore[assignment]

    original_launch_browser = getattr(email_service, "_launch_browser", None)
    if callable(original_launch_browser):
        def traced_launch_browser(*launch_args, **launch_kwargs):
            playwright_ctx, browser_launch_kwargs = original_launch_browser(*launch_args, **launch_kwargs)
            route_trace.append({
                "stage": "yahoo_browser_launch",
                "host": "login.yahoo.com",
                "via_proxy": bool(browser_launch_kwargs.get("proxy")),
                "proxy_masked": mask_proxy((browser_launch_kwargs.get("proxy") or {}).get("server") if isinstance(browser_launch_kwargs.get("proxy"), dict) else yahoo_proxy_url),
                "strategy": "direct" if not browser_launch_kwargs.get("proxy") else "default_proxy",
            })
            return playwright_ctx, browser_launch_kwargs
        email_service._launch_browser = traced_launch_browser  # type: ignore[attr-defined]

    original_http_request = HTTPClient.request
    def traced_request(self, method, url, **kwargs):
        host = str(urlparse(str(url or "")).netloc or "")
        proxies = kwargs.get("proxies") or getattr(self, "proxies", None)
        route_trace.append({
            "stage": "http_request",
            "method": str(method).upper(),
            "url": str(url),
            "host": host,
            "via_proxy": bool(proxies),
            "proxy_masked": mask_proxy((proxies or {}).get("https") if isinstance(proxies, dict) else proxy_url),
        })
        return original_http_request(self, method, url, **kwargs)
    HTTPClient.request = traced_request  # type: ignore[assignment]

    def log_callback(message: str) -> None:
        safe_print(message)

    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url,
        callback_logger=log_callback,
        task_uuid=task_uuid,
    )

    if not args.skip_openai_preflight:
        try:
            oauth_start = engine.oauth_manager.start_oauth()
            preflight_url = getattr(oauth_start, "auth_url", "")
            safe_print(f"[INFO] OpenAI preflight URL: {preflight_url[:120]}...")
            preflight_resp = engine.http_client.get(preflight_url, allow_redirects=False, timeout=20)
            safe_print(f"[INFO] OpenAI preflight status: {preflight_resp.status_code}")
        except Exception as exc:
            safe_print(f"[WARN] OpenAI preflight failed: {exc}")

    try:
        result = engine.run()
    finally:
        HTTPClient.request = original_http_request  # type: ignore[assignment]
        if callable(original_launch_browser):
            email_service._launch_browser = original_launch_browser  # type: ignore[attr-defined]

    if proxy_id is not None:
        with get_db() as db:
            crud.update_proxy_last_used(db, proxy_id)

    if args.save_db and result.success:
        engine.save_to_database(result)

    output = build_output(
        result=result,
        service=service,
        proxy_url=proxy_url,
        proxy_source=proxy_source,
        selected_config=config,
        engine_logs=engine.logs,
        save_db=args.save_db,
        route_trace=route_trace,
        split_policy=split_policy,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    safe_print("\n[SUMMARY]")
    safe_print(json.dumps({
        "success": output["success"],
        "email": output["email"],
        "error_message": output["error_message"],
        "proxy_source": output["proxy_source"],
        "proxy_masked": output["proxy_masked"],
        "split_policy": output["split_policy"],
        "route_trace": output["route_trace"],
        "markers": output["markers"],
        "output": str(output_path),
    }, ensure_ascii=False, indent=2))

    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
