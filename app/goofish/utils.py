"""
闲鱼 API 工具函数
- sign 生成: 纯 Python MD5 (无需 Node.js)
- cookie 解析
- 消息 ID / UUID / 设备 ID 生成
- 消息解密 (base64 + protobuf)
"""
import hashlib
import json
import time
import random
import string
import base64

import blackboxprotobuf


# ==========================================
# Cookie 工具
# ==========================================

def parse_cookies(cookies_str: str) -> dict:
    """将 cookie 字符串解析为字典"""
    cookies = {}
    for item in cookies_str.split("; "):
        try:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
        except ValueError:
            continue
    return cookies


def cookies_to_str(cookies_dict: dict) -> str:
    """将 cookie 字典转为字符串"""
    return "; ".join(f"{k}={v}" for k, v in cookies_dict.items())


def get_token_from_cookies(cookies: dict) -> str:
    """从 cookie 中提取 _m_h5_tk 的 token 部分 (前32位)"""
    tk = cookies.get("_m_h5_tk", "")
    return tk.split("_")[0] if tk else ""


def get_user_id(cookies: dict) -> str:
    """从 cookie 中提取用户 ID (unb)"""
    return cookies.get("unb", "")


# ==========================================
# ID 生成 (纯 Python, 替代 execjs)
# ==========================================

def generate_mid() -> str:
    """生成消息 ID"""
    return f"{int(random.random() * 1000)}{int(time.time() * 1000)} 0"


def generate_uuid() -> str:
    """生成 UUID"""
    return f"-{int(time.time() * 1000)}1"


def generate_device_id(user_id: str) -> str:
    """从用户 ID 生成设备 ID"""
    chars = string.ascii_letters + string.digits
    uuid_chars = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            uuid_chars.append("-")
        elif i == 14:
            uuid_chars.append("4")
        else:
            r = random.randint(0, 15)
            if i == 19:
                uuid_chars.append(chars[(3 & r) | 8])
            else:
                uuid_chars.append(chars[r])
    return "".join(uuid_chars) + "-" + user_id


# ==========================================
# 签名 (纯 Python MD5, 替代 execjs)
# ==========================================

APP_KEY = "34839810"

def generate_sign(t: str, token: str, data: str) -> str:
    """
    闲鱼 MTOP API 签名
    sign = MD5(token + "&" + timestamp + "&" + appKey + "&" + data)
    """
    msg = f"{token}&{t}&{APP_KEY}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


# ==========================================
# 消息解密 (保留 execjs, 因 MessagePack 解码器复杂)
# ==========================================

_js_runtime = None

def _get_js_runtime():
    """懒加载 JS 运行时 (execjs + Node.js)"""
    global _js_runtime
    if _js_runtime is None:
        try:
            import subprocess
            from functools import partial
            subprocess.Popen = partial(subprocess.Popen, encoding="utf-8")
            import execjs
            import os
            js_path = os.path.join(os.path.dirname(__file__), "static", "goofish_js_version_2.js")
            with open(js_path, "r", encoding="utf-8") as f:
                _js_runtime = execjs.compile(f.read())
        except Exception as e:
            print(f"⚠️ [goofish] JS 运行时加载失败 (需要 Node.js): {e}")
    return _js_runtime


def decrypt_message(data: str) -> dict:
    """
    解密闲鱼 WebSocket 消息
    base64 -> MessagePack (JS) -> JSON
    """
    runtime = _get_js_runtime()
    if runtime is None:
        return {}
    try:
        result = runtime.call("decrypt", data)
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        print(f"⚠️ [goofish] 消息解密失败: {e}")
        return {}
