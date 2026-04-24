#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
probe_dir = ROOT / 'release' / 'yahoo_probe'
validate_path = ROOT / 'release' / 'yahoo_alias_openai_validate.json'

summary = {
    'challenge_state': 'unknown',
    'email_targets': [],
    'whatsapp_present': False,
    'push_present': False,
    'artifacts': {},
    'tried': [
        'Yahoo 直连 / OpenAI 默认代理分流已验证生效',
        '登录页 selector 已根据 runtime DOM 扩展',
        '运行态截图/HTML/text/json 已抓取',
    ],
    'ruled_out': [
        '不是 Yahoo 直连失败',
        '不是 OpenAI 代理策略错误',
        '不是首屏邮箱输入框 selector 缺失',
    ],
}

validate = {}
if validate_path.exists():
    validate = json.loads(validate_path.read_text(encoding='utf-8'))
    summary['challenge_state'] = 'challenge-selector' if validate.get('markers', {}).get('challenge_detected') else 'no-challenge'
    summary['artifacts']['validate_json'] = str(validate_path)

text_files = sorted(probe_dir.glob('*.txt'))
json_files = sorted(probe_dir.glob('*.json'))
html_files = sorted(probe_dir.glob('*.html'))
png_files = sorted(probe_dir.glob('*.png'))
if text_files:
    latest_text = text_files[-1]
    summary['artifacts']['latest_text'] = str(latest_text)
    text = latest_text.read_text(encoding='utf-8', errors='ignore')
    emails = re.findall(r'([a-z0-9*._%+-]+@[a-z0-9.-]+)', text.lower())
    summary['email_targets'] = list(dict.fromkeys(emails))
    summary['whatsapp_present'] = 'whatsapp' in text.lower()
    summary['push_present'] = ('推送通知' in text) or ('push' in text.lower())
if json_files:
    summary['artifacts']['latest_probe_json'] = str(json_files[-1])
if html_files:
    summary['artifacts']['latest_html'] = str(html_files[-1])
if png_files:
    summary['artifacts']['latest_screenshot'] = str(png_files[-1])

print(json.dumps(summary, ensure_ascii=False, indent=2))
