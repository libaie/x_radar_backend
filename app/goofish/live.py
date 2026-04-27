"""
闲鱼 WebSocket 实时消息
移植自 https://github.com/cv-cat/XianYuApis
适配 FastAPI asyncio 环境
"""
import json
import base64
import asyncio
import time
from typing import Callable, Optional

import websockets
from loguru import logger

from .apis import XianyuApis
from .utils import (
    generate_mid, generate_uuid, parse_cookies, get_user_id,
    generate_device_id, decrypt_message, cookies_to_str
)


class XianyuLive:
    """闲鱼 WebSocket 消息客户端"""

    WS_URL = "wss://wss-goofish.dingtalk.com/"
    WS_HEADERS = {
        "Host": "wss-goofish.dingtalk.com",
        "Connection": "Upgrade",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Origin": "https://www.goofish.com",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def __init__(self, cookie_str: str, on_message: Optional[Callable] = None):
        """
        Args:
            cookie_str: 完整的 cookie 字符串
            on_message: 收到消息时的回调 async def(sender_id, sender_name, content, cid, item_id)
        """
        self.cookie_str = cookie_str
        self.cookies = parse_cookies(cookie_str)
        self.user_id = get_user_id(self.cookies)
        self.device_id = generate_device_id(self.user_id)
        self.xianyu = XianyuApis(self.cookies, self.device_id)
        self.on_message = on_message
        self.ws = None
        self._running = False
        self._heartbeat_task = None
        self._refresh_task = None
        self._pending_create: Optional[asyncio.Future] = None  # 等待 create_chat 响应
        self._last_heartbeat_response: float = 0  # 上次心跳响应时间

    async def connect(self):
        """建立 WebSocket 连接并进入消息循环"""
        headers = {
            **self.WS_HEADERS,
            "Cookie": cookies_to_str(self.xianyu.session.cookies.get_dict()),
        }

        try:
            self.ws = await websockets.connect(self.WS_URL, additional_headers=headers)
            self._running = True
            logger.info(f"[goofish] WebSocket 已连接: user_id={self.user_id}")

            # 初始化: 注册 + 同步
            await self._init(self.ws)

            # 启动心跳和 token 刷新
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._refresh_task = asyncio.create_task(self._refresh_loop())

            # 消息循环
            async for raw_message in self.ws:
                if not self._running:
                    break
                try:
                    message = json.loads(raw_message)
                    await self._ack(message)
                    await self._handle_message(message)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.warning(f"[goofish] 消息处理异常: {e}")

        except websockets.ConnectionClosed as e:
            logger.warning(f"[goofish] WebSocket 连接关闭: code={e.code}")
        except Exception as e:
            logger.error(f"[goofish] WebSocket 连接失败: {e}")
        finally:
            await self.disconnect()

    async def disconnect(self):
        """断开连接并清理"""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._refresh_task:
            self._refresh_task.cancel()
        # 清理 pending create
        if self._pending_create and not self._pending_create.done():
            self._pending_create.set_result(None)
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        logger.info(f"[goofish] WebSocket 已断开: user_id={self.user_id}")

    async def send_text(self, cid: str, to_id: str, text: str):
        """发送文本消息"""
        if not self.ws:
            raise RuntimeError("WebSocket 未连接")
        payload = {"contentType": 1, "text": {"text": text}}
        text_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        msg = self._build_send_msg(cid, to_id, 1, text_b64)
        await self.ws.send(json.dumps(msg))
        logger.info(f"[goofish] 消息已发送 → {to_id}: {text[:50]}")

    async def create_chat(self, to_id: str, item_id: str, _retry: bool = True) -> str:
        """创建与卖家的会话，返回会话 ID (cid)。400 时自动重连重试一次。"""
        if not self.ws:
            raise RuntimeError("WebSocket 未连接")

        mid = generate_mid()
        msg = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {"mid": mid},
            "body": [{
                "pairFirst": f"{to_id}@goofish",
                "pairSecond": f"{self.user_id}@goofish",
                "bizType": "1",
                "extension": {"itemId": item_id},
                "ctx": {"appVersion": "1.0", "platform": "web"}
            }]
        }

        # 设置 Future 等待服务器响应
        loop = asyncio.get_running_loop()
        self._pending_create = loop.create_future()
        self._pending_create_mid = mid

        try:
            await self.ws.send(json.dumps(msg))
            logger.info(f"[goofish] 已发送创建会话请求: to={to_id}, item={item_id}")

            # 等待服务器响应，超时 10 秒
            cid = await asyncio.wait_for(self._pending_create, timeout=10.0)

            # 400 导致 cid 为 None → 尝试重新注册后重试一次
            if not cid and _retry:
                logger.warning("[goofish] create_chat 失败，尝试重新注册 LWP 会话后重试...")
                if await self._re_register():
                    return await self.create_chat(to_id, item_id, _retry=False)

            logger.info(f"[goofish] 获取到会话 ID: cid={cid}")
            return cid or ""
        except asyncio.TimeoutError:
            logger.warning(f"[goofish] 创建会话超时: to={to_id}, item={item_id}")
            return ""
        except Exception as e:
            logger.error(f"[goofish] 创建会话失败: {e}")
            return ""
        finally:
            self._pending_create = None
            self._pending_create_mid = None

    async def _re_register(self):
        """重新注册 LWP 会话（用新 accessToken），用于 400 后恢复"""
        if not self.ws:
            return False
        access_token = self.xianyu.get_access_token()
        if not access_token:
            logger.error("[goofish] _re_register: 获取 accessToken 失败")
            return False
        reg_msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": access_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        try:
            await self.ws.send(json.dumps(reg_msg))
            logger.info("[goofish] LWP 会话已重新注册 (accessToken 已刷新)")
            await asyncio.sleep(1)  # 等待服务器处理
            return True
        except Exception as e:
            logger.error(f"[goofish] _re_register 失败: {e}")
            return False

    async def _init(self, ws):
        """注册 + 同步"""
        access_token = self.xianyu.get_access_token()
        if not access_token:
            logger.error("[goofish] 获取 accessToken 失败")
            return

        reg_msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": access_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(reg_msg))

        current_time = int(time.time() * 1000)
        sync_msg = {
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [{
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": current_time * 1000,
                "seq": 0,
                "timestamp": current_time
            }]
        }
        await ws.send(json.dumps(sync_msg))
        logger.info("[goofish] WebSocket 注册完成")

    async def _ack(self, message: dict):
        """ACK 确认收到的消息"""
        if "headers" not in message:
            return
        ack = {
            "code": 200,
            "headers": {
                "mid": message["headers"].get("mid", generate_mid()),
                "sid": message["headers"].get("sid", ""),
            }
        }
        for key in ("app-key", "ua", "dt"):
            if key in message["headers"]:
                ack["headers"][key] = message["headers"][key]
        await self.ws.send(json.dumps(ack))

    async def _handle_message(self, message: dict):
        """处理收到的消息"""
        # 心跳响应 (lwp: "/!" 的回复)
        if message.get("code") == 200 and "body" not in message:
            self._last_heartbeat_response = time.time()
            return
        try:
            # 检查是否是 create_chat 的响应
            if self._pending_create and not self._pending_create.done():
                headers = message.get("headers", {})
                body = message.get("body", {})
                mid = headers.get("mid", "")
                code = message.get("code")

                # 只匹配与 create_chat 请求 mid 相同的响应
                is_create_response = (mid == getattr(self, '_pending_create_mid', ''))

                # 也匹配没有 mid 但包含 cid 的响应（兜底）
                cid = ""
                if is_create_response or (not mid and body):
                    # 从不同路径提取 cid
                    if isinstance(body, list):
                        for item in body:
                            if isinstance(item, dict):
                                cid = item.get("cid", "") or item.get("conversationId", "")
                                if cid:
                                    break
                    elif isinstance(body, dict):
                        cid = body.get("cid", "") or body.get("conversationId", "")
                    # 从 headers 提取
                    if not cid:
                        cid = headers.get("cid", "")
                    # 从嵌套结构提取
                    if not cid and isinstance(body, dict):
                        data = body.get("data", {})
                        if isinstance(data, dict):
                            cid = data.get("cid", "") or data.get("conversationId", "")

                if cid:
                    # 清理 cid (去掉 @goofish 后缀)
                    if "@" in cid:
                        cid = cid.split("@")[0]
                    self._pending_create.set_result(cid)
                    return

                # 检查是否是错误响应
                if is_create_response and code and code != 200:
                    logger.warning(f"[goofish] create_chat 返回错误: code={code}, body={body}")
                    self._pending_create.set_result(None)
                    return

            body = message.get("body", {})
            sync_data = body.get("syncPushPackage", {}).get("data", [])
            if not sync_data:
                return

            raw_data = sync_data[0].get("data", "")

            # 尝试直接解析 JSON
            try:
                data = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                # 加密消息，需要解密
                data = decrypt_message(raw_data)
                if not data:
                    return

            # 提取消息信息
            sender_name = data.get("1", {}).get("10", {}).get("reminderTitle", "")
            sender_id = data.get("1", {}).get("10", {}).get("senderUserId", "")
            content = data.get("1", {}).get("10", {}).get("reminderContent", "")
            cid = data.get("1", {}).get("2", "")
            if isinstance(cid, str) and "@" in cid:
                cid = cid.split("@")[0]

            # 从 reminderUrl 提取 item_id
            reminder_url = data.get("1", {}).get("10", {}).get("reminderUrl", "")
            item_id = ""
            if "itemId=" in reminder_url:
                item_id = reminder_url.split("itemId=")[1].split("&")[0]

            if sender_id and sender_id != self.user_id:
                logger.info(f"[goofish] 收到消息: {sender_name} → {content[:50]}")
                if self.on_message:
                    try:
                        await self.on_message(
                            sender_id=sender_id,
                            sender_name=sender_name,
                            content=content,
                            cid=cid,
                            item_id=item_id
                        )
                    except Exception as cb_err:
                        logger.warning(f"[goofish] 消息回调异常: {cb_err}")

        except Exception:
            pass  # 非消息类型数据（心跳响应等），静默忽略

    def _build_send_msg(self, cid: str, to_id: str, content_type: int, data_b64: str) -> dict:
        """构造发送消息的 LWP 包"""
        return {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": generate_mid()},
            "body": [{
                "uuid": generate_uuid(),
                "cid": f"{cid}@goofish",
                "conversationType": 1,
                "content": {
                    "contentType": 101,
                    "custom": {"type": content_type, "data": data_b64}
                },
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1
            }, {
                "actualReceivers": [f"{to_id}@goofish", f"{self.user_id}@goofish"]
            }]
        }

    async def _heartbeat_loop(self):
        """心跳保活 (每15秒) + 超时检测"""
        self._last_heartbeat_response = time.time()
        while self._running and self.ws:
            try:
                await self.ws.send(json.dumps({
                    "lwp": "/!",
                    "headers": {"mid": generate_mid()}
                }))
                await asyncio.sleep(15)
                # 检测心跳超时 (30秒无响应 → 连接已死)
                if time.time() - self._last_heartbeat_response > 30:
                    logger.warning("[goofish] 心跳超时，连接可能已断开，触发重连")
                    break
            except Exception:
                break

    async def _refresh_loop(self):
        """定时刷新 token (每600秒)，刷新后主动断开触发重连"""
        while self._running:
            await asyncio.sleep(600)
            try:
                self.xianyu.refresh_token()
                logger.info("[goofish] Token 已刷新，主动断开 WS 以用新 token 重连")
                # 主动关闭连接，触发 connect() 主循环重连
                if self.ws:
                    await self.ws.close()
                break
            except Exception as e:
                logger.warning(f"[goofish] Token 刷新失败: {e}")
