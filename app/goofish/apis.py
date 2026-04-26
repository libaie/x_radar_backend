"""
闲鱼 HTTP API 封装
移植自 https://github.com/cv-cat/XianYuApis
适配 FastAPI 后端（requests Session → 可复用）
"""
import json
import time
from typing import Optional

import requests
from loguru import logger

from .utils import (
    parse_cookies, cookies_to_str, get_token_from_cookies, get_user_id,
    generate_device_id, generate_sign
)


class XianyuApis:
    """闲鱼 HTTP API 客户端 (MTOP 网关)"""

    BASE_HEADERS = {
        "Host": "h5api.m.goofish.com",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "accept": "application/json",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "content-type": "application/x-www-form-urlencoded",
        "sec-ch-ua-mobile": "?0",
        "origin": "https://www.goofish.com",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://www.goofish.com/",
        "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
        "priority": "u=1, i"
    }

    def __init__(self, cookies: dict, device_id: str):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.device_id = device_id
        self.user_id = get_user_id(cookies)

    @classmethod
    def from_cookie_str(cls, cookie_str: str) -> "XianyuApis":
        """从 cookie 字符串创建实例"""
        cookies = parse_cookies(cookie_str)
        user_id = get_user_id(cookies)
        device_id = generate_device_id(user_id)
        return cls(cookies, device_id)

    def _post_mtop(self, url: str, api_name: str, data_val: str, spm_pre: str = "") -> dict:
        """通用 MTOP POST 请求"""
        t = str(int(time.time() * 1000))
        token = get_token_from_cookies(self.session.cookies.get_dict())

        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": t,
            "sign": generate_sign(t, token, data_val),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
            "spm_pre": spm_pre or "a21ybx.item.want.1.12523da6waCtUp",
            "log_id": "12523da6waCtUp"
        }
        data = {"data": data_val}

        response = self.session.post(url, params=params, headers=self.BASE_HEADERS, data=data)
        self._sync_cookies(response)
        return response.json()

    def _sync_cookies(self, response):
        """同步响应 cookie，去重"""
        resp_cookies = response.cookies.get_dict()
        session_cookies = self.session.cookies.get_dict()
        for key in resp_cookies:
            if key in session_cookies:
                for c in self.session.cookies:
                    if c.name == key and c.domain == "" and c.path == "/":
                        self.session.cookies.clear(domain=c.domain, path=c.path, name=c.name)
                        break

    def get_token(self) -> dict:
        """获取 WebSocket accessToken"""
        url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
        data_val = json.dumps({
            "appKey": "444e9908a51d1cb236a27862abc769c9",
            "deviceId": self.device_id
        }, separators=(",", ":"))
        result = self._post_mtop(url, "mtop.taobao.idlemessage.pc.login.token", data_val)

        # 令牌过期时重试一次
        if "ret" in result and any("令牌过期" in r for r in result.get("ret", [])):
            return self.get_token()
        return result

    def refresh_token(self) -> dict:
        """刷新登录 session"""
        url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/"
        return self._post_mtop(url, "mtop.taobao.idlemessage.pc.loginuser.get", "{}")

    def get_item_info(self, item_id: str) -> dict:
        """获取商品详情"""
        url = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/"
        data_val = json.dumps({"itemId": item_id}, separators=(",", ":"))
        return self._post_mtop(url, "mtop.taobao.idle.pc.detail", data_val)

    def get_access_token(self) -> str:
        """获取 WS accessToken，失败返回空字符串"""
        try:
            result = self.get_token()
            return result.get("data", {}).get("accessToken", "")
        except Exception as e:
            logger.error(f"[goofish] 获取 token 失败: {e}")
            return ""
