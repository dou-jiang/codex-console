#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_command(cmd: list[str]) -> int:
    print("[CMD]", " ".join(cmd))
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="远端执行版：Roxy Yahoo alias -> OpenAI 注册")
    parser.add_argument("--roxy-api-host", default="http://127.0.0.1:50000")
    parser.add_argument("--roxy-token", default="")
    parser.add_argument("--workspace-id", type=int, default=0)
    parser.add_argument("--dir-id", default="")
    parser.add_argument("--roxy-ws-endpoint", default="")
    parser.add_argument("--service-id", type=int, default=2)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--force-open", action="store_true")
    parser.add_argument("--save-db", action="store_true")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "release" / "roxy_yahoo_alias_openai_remote.json"))
    parser.add_argument("--extra-roxy-arg", action="append", default=[])
    args = parser.parse_args()

    if not str(args.roxy_ws_endpoint or "").strip():
        missing = []
        if not str(args.roxy_token or "").strip():
            missing.append("--roxy-token")
        if int(args.workspace_id or 0) <= 0:
            missing.append("--workspace-id")
        if not str(args.dir_id or "").strip():
            missing.append("--dir-id")
        if missing:
            parser.error("未传 --roxy-ws-endpoint 时必须提供: " + ", ".join(missing))

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "yahoo_alias_openai_validate.py"),
        "--service-id", str(args.service_id),
        "--proxy", str(args.proxy),
        "--output", str(args.output),
    ]

    if str(args.roxy_ws_endpoint or "").strip():
        cmd.extend(["--roxy-ws-endpoint", str(args.roxy_ws_endpoint)])
        if int(args.workspace_id or 0) > 0:
            cmd.extend(["--roxy-workspace-id", str(args.workspace_id)])
    else:
        cmd.extend([
            "--roxy-api-host", str(args.roxy_api_host),
            "--roxy-token", str(args.roxy_token),
            "--roxy-open-dir-id", str(args.dir_id),
            "--roxy-workspace-id", str(args.workspace_id),
        ])

    if args.headless:
        cmd.append("--headless")
    if args.force_open:
        cmd.append("--roxy-force-open")
    if args.save_db:
        cmd.append("--save-db")
    for item in args.extra_roxy_arg:
        cmd.extend(["--roxy-arg", str(item)])

    rc = run_command(cmd)
    if rc != 0:
        print(json.dumps({
            "success": False,
            "returncode": rc,
            "output": args.output,
        }, ensure_ascii=False, indent=2))
        return rc

    print(json.dumps({
        "success": True,
        "output": args.output,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
