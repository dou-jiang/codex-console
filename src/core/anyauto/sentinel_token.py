"""
Sentinel Token 生成器模块
基于对 sentinel.openai.com SDK 的逆向分析
"""

import json
import time
import uuid
import random
import base64
import hashlib


class SentinelTokenGenerator:
    """
    Sentinel Token 纯 Python 生成器
    
    通过逆向 sentinel SDK 的 PoW 算法，纯 Python 构造合法的 openai-sentinel-token。
    """

    MAX_ATTEMPTS = 500000  # 最大 PoW 尝试次数
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"  # SDK 中的错误前缀常量

    def __init__(self, device_id=None, user_agent=None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text):
        """
        FNV-1a 32位哈希算法（从 SDK JS 逆向还原）
        """
        h = 2166136261  # FNV offset basis
        for ch in text:
            code = ord(ch)
            h ^= code
            h = (h * 16777619) & 0xFFFFFFFF

        # xorshift 混合（murmurhash3 finalizer）
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        h = h & 0xFFFFFFFF

        return format(h, "08x")

    def _get_config(self):
        """构造浏览器环境数据数组"""
        from datetime import datetime, timezone
        
        screen_info = "1920x1080"
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        js_heap_limit = 4294705152
        nav_random1 = random.random()
        ua = self.user_agent
        script_src = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"
        script_version = None
        data_build = None
        language = "en-US"
        languages = "en-US,en"
        nav_random2 = random.random()
        
        nav_props = [
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
            "mediaDevices", "permissions", "locks", "ink",
        ]
        nav_prop = random.choice(nav_props)
        nav_val = f"{nav_prop}−undefined"
        
        doc_key = random.choice(["location", "implementation", "URL", "documentURI", "compatMode"])
        win_key = random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"])
        perf_now = random.uniform(1000, 50000)
        hardware_concurrency = random.choice([4, 8, 12, 16])
        time_origin = time.time() * 1000 - perf_now

        config = [
            screen_info, date_str, js_heap_limit, nav_random1, ua,
            script_src, script_version, data_build, language, languages,
            nav_random2, nav_val, doc_key, win_key, perf_now,
            self.sid, "", hardware_concurrency, time_origin,
        ]
        return config

    @staticmethod
    def _base64_encode(data):
        """模拟 SDK 的 E() 函数：JSON.stringify → TextEncoder.encode → btoa"""
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        encoded = json_str.encode("utf-8")
        return base64.b64encode(encoded).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        """单次 PoW 检查"""
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_input = seed + data
        hash_hex = self._fnv1a_32(hash_input)
        diff_len = len(difficulty)
        if hash_hex[:diff_len] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed=None, difficulty=None):
        """生成 sentinel token（完整 PoW 流程）"""
        if seed is None:
            seed = self.requirements_seed
            difficulty = difficulty or "0"

        start_time = time.time()
        config = self._get_config()

        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result

        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self):
        """生成 requirements token（不需要服务端参数）"""
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        data = self._base64_encode(config)
        return "gAAAAAC" + data


def fetch_sentinel_challenge(session, device_id, flow="authorize_continue", user_agent=None, sec_ch_ua=None, impersonate=None):
    """调用 sentinel 后端 API 获取 challenge 数据"""
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    req_body = {
        "p": generator.generate_requirements_token(),
        "id": device_id,
        "flow": flow,
    }
    
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or "Mozilla/5.0",
        "sec-ch-ua": sec_ch_ua or '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }

    kwargs = {
        "data": json.dumps(req_body),
        "headers": headers,
        "timeout": 20,
    }
    if impersonate:
        kwargs["impersonate"] = impersonate

    try:
        resp = session.post("https://sentinel.openai.com/backend-api/sentinel/req", **kwargs)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    
    return None


def build_sentinel_token(session, device_id, flow="authorize_continue", user_agent=None, sec_ch_ua=None, impersonate=None):
    """构建完整的 openai-sentinel-token JSON 字符串"""
    challenge = fetch_sentinel_challenge(session, device_id, flow=flow, user_agent=user_agent, sec_ch_ua=sec_ch_ua, impersonate=impersonate)
    
    if not challenge:
        return None

    c_value = challenge.get("token", "")
    if not c_value:
        return None

    pow_data = challenge.get("proofofwork") or {}
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)

    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = generator.generate_requirements_token()

    return json.dumps({
        "p": p_value,
        "t": "",
        "c": c_value,
        "id": device_id,
        "flow": flow,
    }, separators=(",", ":"))
