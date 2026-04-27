"""
闲鱼 WebSocket 连接池
管理多个账号的 WS 连接，支持并发对话
"""
import asyncio
from typing import Dict, Optional

from loguru import logger

from .live import XianyuLive
from ..crypto import decrypt_value
from .. import models, database


async def _on_message_handler(plugin_id: str, **kwargs):
    """WS 消息回调 → 转发到对话引擎"""
    from .chat_engine import handle_seller_message
    await handle_seller_message(
        plugin_id=plugin_id,
        seller_id=kwargs.get("sender_id", ""),
        seller_name=kwargs.get("sender_name", ""),
        content=kwargs.get("content", ""),
        cid=kwargs.get("cid", ""),
        item_id=kwargs.get("item_id", ""),
    )


class XianYuConnectionPool:
    """
    每个 plugin_id 对应一个 XianyuLive 实例
    维护所有账号的 WebSocket 长连接
    """

    def __init__(self):
        self._connections: Dict[str, XianyuLive] = {}  # plugin_id → XianyuLive
        self._tasks: Dict[str, asyncio.Task] = {}       # plugin_id → WS 任务

    async def ensure_connection(self, plugin_id: str, on_message=None) -> Optional[XianyuLive]:
        """
        确保指定账号的 WS 连接存活
        如果已连接则复用，否则新建并等待连接建立
        """
        # 已连接且活跃
        if plugin_id in self._connections and self._connections[plugin_id].ws:
            return self._connections[plugin_id]

        # 从数据库获取 cookie
        with database.SessionLocal() as db:
            record = db.query(models.CookieStore).filter(
                models.CookieStore.plugin_id == plugin_id,
                models.CookieStore.status == "active"
            ).first()

        if not record:
            logger.warning(f"[pool] ❌ 未找到 plugin_id={plugin_id[:8]} 的 cookie! Chrome 插件需要先同步 Cookie")
            return None

        cookie_str = decrypt_value(record.cookie_enc)

        # 创建连接实例，绑定消息回调
        async def on_msg(**kwargs):
            await _on_message_handler(plugin_id, **kwargs)

        live = XianyuLive(cookie_str, on_message=on_msg)
        self._connections[plugin_id] = live

        # 启动 WS 连接 (后台 task)
        task = asyncio.create_task(self._run_connection(plugin_id, live))
        self._tasks[plugin_id] = task

        # 等待 WS 连接建立 (最多 10 秒)
        for _ in range(20):
            if live.ws:
                break
            await asyncio.sleep(0.5)

        if not live.ws:
            logger.warning(f"[pool] WS 连接建立超时: plugin_id={plugin_id}")
            # 连接仍在后台重试中，不清理

        logger.info(f"[pool] 已启动 WS 连接: plugin_id={plugin_id}, connected={bool(live.ws)}")
        return live

    async def _run_connection(self, plugin_id: str, live: XianyuLive):
        """运行 WS 连接，断线自动重连"""
        retry_count = 0
        while retry_count < 5:
            try:
                await live.connect()
            except Exception as e:
                logger.error(f"[pool] WS 连接异常: plugin_id={plugin_id}, error={e}")

            retry_count += 1
            delay = min(5 * (2 ** retry_count), 60)
            logger.info(f"[pool] {delay}s 后重连: plugin_id={plugin_id} (第{retry_count}次)")
            await asyncio.sleep(delay)

        logger.error(f"[pool] WS 连接放弃: plugin_id={plugin_id} (超过最大重试)")
        self._connections.pop(plugin_id, None)
        self._tasks.pop(plugin_id, None)

    async def send_text(self, plugin_id: str, cid: str, to_id: str, text: str):
        """通过指定账号发送文本消息"""
        live = self._connections.get(plugin_id)
        if not live or not live.ws:
            live = await self.ensure_connection(plugin_id)
        if live and live.ws:
            await live.send_text(cid, to_id, text)
        else:
            raise RuntimeError(f"plugin_id={plugin_id} 的 WS 连接不可用")

    async def create_chat(self, plugin_id: str, to_id: str, item_id: str) -> str:
        """通过指定账号创建会话"""
        live = self._connections.get(plugin_id)
        if not live or not live.ws:
            logger.info(f"[pool] create_chat: plugin={plugin_id[:8]} 无现成连接，尝试 ensure_connection")
            live = await self.ensure_connection(plugin_id)
        if live and live.ws:
            logger.info(f"[pool] create_chat: plugin={plugin_id[:8]} WS 已连接，开始创建会话")
            return await live.create_chat(to_id, item_id)
        logger.error(f"[pool] ❌ create_chat: plugin={plugin_id[:8]} WS 连接不可用 (live={bool(live)}, ws={bool(live.ws) if live else 'N/A'})")
        raise RuntimeError(f"plugin_id={plugin_id} 的 WS 连接不可用")

    async def close(self, plugin_id: str):
        """关闭指定账号的连接"""
        live = self._connections.pop(plugin_id, None)
        task = self._tasks.pop(plugin_id, None)
        if live:
            await live.disconnect()
        if task:
            task.cancel()

    async def close_all(self):
        """关闭所有连接"""
        for plugin_id in list(self._connections.keys()):
            await self.close(plugin_id)
        logger.info("[pool] 所有 WS 连接已关闭")

    @property
    def active_count(self) -> int:
        return len(self._connections)

    @property
    def active_ids(self) -> list:
        return list(self._connections.keys())


# 全局单例
connection_pool = XianYuConnectionPool()
