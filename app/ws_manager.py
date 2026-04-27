import asyncio
import json
import random
from typing import Dict, Set
from fastapi import WebSocket
from app.redis_config import redis_client
import time

class ConnectionManager:
    def __init__(self):
        # 📺 1. 广播池：存放所有连接（包含插件自身 + 正在看这台机器的管理端网页）
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        
        # 🤖 2. 任务池：专门存放真实在跑的 Chrome 插件环境
        self.worker_connections: Dict[str, WebSocket] = {}
        self.node_status: Dict[str, str] = {}
        self.node_owner: Dict[str, str] = {}  # 🆕 plugin_id -> user_id 映射

        # Redis 队列键名
        self.QUEUE_KEY = "radar:keyword_queue"

        # 接力控制：记录每个节点正在拿着什么任务，以及最后一次干完活的时间
        self.working_tasks: Dict[str, dict] = {}
        self.last_work_time: Dict[str, float] = {}
        self.task_start_time: Dict[str, float] = {}

        # 🌟 全局事件唤醒器（这就是大屏总按钮和节点完工的通信器）
        self.wakeup_event = asyncio.Event()

        self.admin_connections: Set[WebSocket] = set()

        self.last_ping_time: Dict[str, float] = {}
    
    def trigger_dispatch(self):
        """🌟 触发唤醒，让调度引擎立刻开始查空闲节点"""
        self.wakeup_event.set()

    # 🌟向大屏推送状态的方法
    async def broadcast_to_admins(self, message: dict):
        for connection in list(self.admin_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.admin_connections.discard(connection)

    # 🌟 新增了 role 参数，默认是干活的 plugin
    async def connect(self, client_id: str, websocket: WebSocket, role: str = "plugin", user_id: str = None):
        await websocket.accept()

        if role == "admin":
            self.admin_connections.add(websocket)

        # 无论谁来，都丢进全局广播池，保证能收到日志
        if client_id not in self.active_connections:
            self.active_connections[client_id] = set()
        self.active_connections[client_id].add(websocket)

        # 如果是真正干活的插件，才注册进任务调度池
        if role == "plugin":
            self.last_ping_time[client_id] = time.time()
            self.worker_connections[client_id] = websocket
            self.node_status[client_id] = "idle"
            if user_id:
                self.node_owner[client_id] = user_id  # 🆕 记录归属
            # 刚上线，赋予时间戳 0，保证它能优先接单
            if client_id not in self.last_work_time:
                self.last_work_time[client_id] = 0.0
            
            # 🌟 秒传大屏：节点上线啦！
            asyncio.create_task(self.broadcast_to_admins({
                "type": "sys_status", "plugin_id": client_id, "ws_status": "idle"
            }))
        else:
            print(f"👁️ [管理端前端] 接入监控: {client_id}")

    def disconnect(self, client_id: str, websocket: WebSocket, role: str = "plugin"):
        # 🚨 绝杀修复 1：不要在这里清理 task_start_time，也不要没收任务！
        # 给节点一个网络抖动重连的机会，真正的生死交由看门狗裁决！
        
        if role == "admin":
            self.admin_connections.discard(websocket)

        # 从广播池移除
        if client_id in self.active_connections:
            self.active_connections[client_id].discard(websocket)
            if not self.active_connections[client_id]:
                del self.active_connections[client_id]
        
        # 如果是插件掉线，仅仅摘除通信 Socket，保留它的任务记忆！
        if role == "plugin":
            self.worker_connections.pop(client_id, None)
            # 🆕 不再立即标记 offline！Chrome MV3 Service Worker 每30秒会被杀一次，
            # 但会立刻重连。给看门狗（120秒超时）来判定真正的死亡。
            # 保留当前状态（idle/working/standby），避免大屏状态闪烁。

        else:
            print(f"🙈 [管理端前端] 离开监控: {client_id}")

    # ==========================================
    # 📡 兼容层：全频道广播 (发日志用)
    # ==========================================
    async def broadcast_to_plugin(self, client_id: str, message: dict):
        if client_id in self.active_connections:
            for connection in self.active_connections[client_id].copy():
                try:
                    await connection.send_json(message)
                except Exception:
                    pass 

    # ==========================================
    # 🎯 调度层：精准派发任务 (防污染)
    # ==========================================
    async def send_task_to_worker(self, client_id: str, message: dict):
        # 绝对不广播，只精准发给该 ID 对应的插件 Socket
        if client_id in self.worker_connections:
            try:
                await self.worker_connections[client_id].send_json(message)
            except Exception:
                pass

    def update_status(self, client_id: str, status: str):
        if client_id in self.worker_connections:
            self.node_status[client_id] = status
            
            if status == "idle":
                self.last_work_time[client_id] = time.time()
                self.task_start_time.pop(client_id, None)
                if client_id in self.working_tasks:
                    task_data = self.working_tasks.pop(client_id)
                    redis_client.rpush(self.QUEUE_KEY, json.dumps(task_data))
                    print(f"✅ [完工] 节点 {client_id} 完成了 '{task_data['keyword']}'")
                self.trigger_dispatch()
                
            # 🌟 修复：将其与 if status == "idle" 平级，确保退回 standby 时也能安全交还任务
            elif status in ["standby", "offline"]:
                self.task_start_time.pop(client_id, None)
                if client_id in self.working_tasks:
                    task_data = self.working_tasks.pop(client_id)
                    redis_client.rpush(self.QUEUE_KEY, json.dumps(task_data))
                    print(f"🛑 [节点截停] 节点 {client_id} 已{status}，交还任务 '{task_data['keyword']}'")
                self.trigger_dispatch()
        
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast_to_admins({
                "type": "sys_status", "plugin_id": client_id, "ws_status": status
            }))
        except RuntimeError: pass

    # ================= =========================
    # ⚙️ Redis 分布式调度引擎
    # ==========================================
    async def start_dispatcher(self):
        print("🚀 [引擎] 多模式接力调度已启动...")
        while True:

            try:
                await asyncio.wait_for(self.wakeup_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass # 超时正常，说明这 5 秒内风平浪静
            self.wakeup_event.clear() # 清理唤醒标志，准备处理

            # 1. 每轮重新计算空闲节点（避免使用过期状态）
            idle_nodes = [
                pid for pid, status in self.node_status.items()
                if status == "idle" and pid in self.worker_connections
            ]

            if not idle_nodes: continue
            idle_nodes.sort(key=lambda pid: self.last_work_time.get(pid, 0.0))

            # 2. 扫描 Redis 队列
            q_len = redis_client.llen(self.QUEUE_KEY)
            if q_len == 0: continue

            # 获取当前所有正在工作的任务名称，用于排队模式去重
            active_keywords = [t.get('keyword') for t in self.working_tasks.values()]
            no_progress_count = 0  # 连续无进展计数，防止死循环

            # 🌟 核心：逐个任务进行分发判断
            for _ in range(q_len):
                # 连续无进展次数超过队列长度，说明所有任务都无法派发，退出
                if no_progress_count >= q_len:
                    break

                # 从左侧偷看一眼任务，但不弹出 (LINDEX)
                task_json = redis_client.lindex(self.QUEUE_KEY, 0)
                if not task_json: break
                task_data = json.loads(task_json)

                task_type = task_data.get('task_type', 'sequential')
                keyword = task_data.get('keyword')
                task_user_id = task_data.get('user_id', '')
                target_plugin_id = task_data.get('target_plugin_id', '')  # 🆕 指定节点

                # 🆕 按 user_id 过滤可用节点
                if not task_user_id:
                    # 无归属的任务跳过（旧数据或异常数据）
                    redis_client.lpop(self.QUEUE_KEY)
                    redis_client.rpush(self.QUEUE_KEY, task_json)
                    no_progress_count += 1
                    continue

                # 🆕 指定节点模式：只发给目标节点
                if target_plugin_id:
                    task_idle_nodes = [
                        pid for pid in idle_nodes
                        if pid == target_plugin_id and self.node_owner.get(pid) == task_user_id
                    ]
                else:
                    task_idle_nodes = [
                        pid for pid in idle_nodes
                        if self.node_owner.get(pid) == task_user_id
                    ]

                if not task_idle_nodes:
                    # 🆕 没有匹配的空闲节点，将任务移到队尾避免阻塞
                    redis_client.lpop(self.QUEUE_KEY)
                    redis_client.rpush(self.QUEUE_KEY, task_json)
                    no_progress_count += 1
                    continue

                node = task_idle_nodes[0]
                dispatch_delays = task_data.get('dispatchDelays', [10, 25])
                if not isinstance(dispatch_delays, list) or len(dispatch_delays) != 2:
                    dispatch_delays = [5, 10]

                d_min = min(int(dispatch_delays[0]), int(dispatch_delays[1]))
                d_max = max(int(dispatch_delays[0]), int(dispatch_delays[1]))
                dispatch_delay = random.randint(d_min, d_max)

                # -------------------------------------------------------
                # 🚦 逻辑判定：是否允许派发？
                if task_type == 'concurrent' or keyword not in active_keywords:
                    redis_client.lpop(self.QUEUE_KEY)
                    self.node_status[node] = "working"
                    self.working_tasks[node] = task_data

                    now = time.time()
                    last_time = self.last_work_time.get(node, 0.0) # 节点上次完工的时间
                    elapsed = now - last_time
                    actual_delay = max(0, dispatch_delay - elapsed)
                    self.task_start_time[node] = now + actual_delay

                    print(f"📤 [派发] 任务 '{keyword}' → 节点 {node[:8]}... (延迟 {dispatch_delay}s)")
                    asyncio.create_task(self._delayed_send(node, dispatch_delay, task_data))
                    # 从空闲列表移除已分配的节点，避免重复分配
                    if node in idle_nodes:
                        idle_nodes.remove(node)
                    active_keywords.append(keyword)
                    no_progress_count = 0  # 有进展，重置计数
                else:
                    # 排队阻塞中，移到队尾给其他词让路
                    redis_client.lpop(self.QUEUE_KEY)
                    redis_client.rpush(self.QUEUE_KEY, task_json)
                    no_progress_count += 1


    async def _delayed_send(self, node: str, delay: int, task_data: dict):
        """🌟 后台倒计时发车器，绝不阻塞主调度大脑"""
        if delay > 0:
            await asyncio.sleep(delay)
            
        # 🌟 醒来后安全校验：防止在等红灯期间，用户按了“紧急清空列队”
        current_task = self.working_tasks.get(node)
        if current_task and current_task.get("keyword") == task_data.get("keyword"):
            await self.send_task_to_worker(node, {"type": "command", "action": "DO_SINGLE_SEARCH", "payload": task_data})


    async def cancel_all_working_tasks(self, user_id: str = None):
        """🌟 紧急召回！让正在干活的节点立刻刹车（可按 user_id 过滤）"""
        targets = []
        for client_id, task in self.working_tasks.items():
            if user_id:
                # 只取消属于该用户的任务
                if self.node_owner.get(client_id) == user_id:
                    targets.append(client_id)
            else:
                # admin 全局清空
                targets.append(client_id)

        for client_id in targets:
            await self.send_task_to_worker(client_id, {
                "type": "command", "action": "REMOTE_STOP"
            })
            self.working_tasks.pop(client_id, None)

    # ==========================================
    # 🐕 全局超时看门狗
    # ==========================================
    async def watchdog_sweeper(self):
        print("🐕 [看门狗] 任务超时巡逻队已出发...")
        # 🌟 绝杀修复 2：放宽耐心！拟人化爬虫很慢，允许一个任务最多跑 10 分钟！
        TIMEOUT_SECONDS = 600  
        # 🌟 绝杀修复 3：容忍断网！2分钟连不上才判定死亡
        ZOMBIE_TIMEOUT = 120   

        while True:
            await asyncio.sleep(15)
            now = time.time()
            
            # 🧟‍♂️ 1. 扫描僵尸节点 (断网不回来的)
            for client_id, last_seen in list(self.last_ping_time.items()):
                if now - last_seen > ZOMBIE_TIMEOUT:
                    print(f"👻 [猎杀僵尸] 发现无响应节点: {client_id}")
                    
                    ws = self.worker_connections.get(client_id)
                    if ws:
                        try:
                            await ws.send_json({"type": "command", "action": "PING_CHECK"})
                            await ws.close()
                        except Exception: pass
                    
                    # 🌟 正式收尸：断网太久了，剥夺任务并放回队列让别人做
                    if client_id in self.working_tasks:
                        task_data = self.working_tasks.pop(client_id)
                        redis_client.rpush(self.QUEUE_KEY, json.dumps(task_data))
                        print(f"♻️ [僵尸回收] 已剥夺失联节点 [{client_id}] 的任务 '{task_data['keyword']}'")
                    
                    # 彻底清理遗物
                    self.last_ping_time.pop(client_id, None)
                    self.task_start_time.pop(client_id, None)
                    self.node_status.pop(client_id, None)
                    self.worker_connections.pop(client_id, None)
                    
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.broadcast_to_admins({
                            "type": "sys_status", "plugin_id": client_id, "ws_status": "offline"
                        }))
                    except RuntimeError: pass

            # 🐢 2. 扫描执行超时任务 (活着的但是卡死了 10 分钟还没滚到底的)
            for client_id, start_time in list(self.task_start_time.items()):
                if now - start_time > TIMEOUT_SECONDS:
                    print(f"🚨🚨 [警报] 节点 [{client_id}] 超过 {TIMEOUT_SECONDS} 秒未交差，判定为网页卡死！")
                    
                    if client_id in self.working_tasks:
                        task_data = self.working_tasks.pop(client_id)
                        redis_client.rpush(self.QUEUE_KEY, json.dumps(task_data))
                        print(f"♻️ 已强行剥夺 [{client_id}] 的卡死任务并塞回队列。")
                    
                    self.task_start_time.pop(client_id, None)
                    
                    if client_id in self.worker_connections:
                        self.node_status[client_id] = "idle"
                        try:
                            ws = self.worker_connections[client_id]
                            # 发送急刹车，让它刷新页面重置状态
                            await ws.send_json({"type": "command", "action": "FORCE_RESTART"})
                            await ws.close()
                        except Exception: pass


manager = ConnectionManager()