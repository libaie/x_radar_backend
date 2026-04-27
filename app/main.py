import sys
import os
import time
import uuid
import json

# 精准定位到 xianyu_backend
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 加载 .env 文件
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session
import asyncio
from datetime import datetime, timezone
from app.service.email_service import send_emergency_email
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

# 内部模块导入
from app import models, schemas, database
from app.database import SessionLocal, engine, Base

# 🌟 导入解耦后的核心组件
from app.redis_config import redis_client
from app.ws_manager import manager
from app.crypto import encrypt_value

# IM-6: 导入 worker 的消费者逻辑，集成到同一进程
from app.service.worker import consume_plugin_queue, plugin_discovery_daemon

# 🆕 JWT 多租户认证
from app.auth import hash_password, verify_password, create_token, decode_token

# 创建数据库表
Base.metadata.create_all(bind=engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- 启动逻辑 ---
    print("🚀 [系统启动] 正在拉起分布式调度引擎与看门狗...")
    
    # 1. 开启派单引擎 (负责 A->B->C 轮询)
    dispatcher_task = asyncio.create_task(manager.start_dispatcher())
    
    # 2. 🌟 开启看门狗 (负责监控假死并回收任务)
    # 这行绝对不能注释掉，它是防止任务丢失的核心
    watchdog_task = asyncio.create_task(manager.watchdog_sweeper())
    
    # IM-6: 启动 AI 评估消费者 + 队列发现守护进程（集成到同一进程）
    default_consumer_task = asyncio.create_task(consume_plugin_queue(None))
    discovery_task = asyncio.create_task(plugin_discovery_daemon())
    
    print("✅ 后台服务已全部就绪（含 AI 评估消费者）。")
    
    yield  # 服务器运行中...
    
    # --- 关闭逻辑 ---
    print("🛑 [系统关闭] 正在安全停止后台任务...")
    dispatcher_task.cancel()
    watchdog_task.cancel()
    default_consumer_task.cancel()
    discovery_task.cancel()

    # 关闭闲鱼 WS 连接池
    from app.goofish.connection import connection_pool
    await connection_pool.close_all()
    
    try:
        await asyncio.gather(
            dispatcher_task, watchdog_task, 
            default_consumer_task, discovery_task,
            return_exceptions=True
        )
    except Exception as e:
        print(f"👋 任务清理完成: {e}")
        print("👋 调度引擎已安全退出。")

app = FastAPI(title="二手商品搜索雷达后台管理服务", lifespan=lifespan)

# B5 修复：CORS 不再用 * + credentials 矛盾配置
# 生产环境请设置 CORS_ORIGINS 环境变量，逗号分隔
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else [
    "http://localhost:15001", "http://127.0.0.1:15001",
    "http://localhost:3000", "http://127.0.0.1:3000",
    "null",  # 支持 file:// 协议打开的前端
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 托管前端 index.html
@app.get("/")
@app.get("/index.html")
async def serve_frontend():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

# 🆕 注册闲鱼私聊整合路由
from app.goofish.router import router as chat_router
app.include_router(chat_router)

# ==========================================
# 🔐 JWT 多租户认证中间件
# ==========================================

# 免认证路径
AUTH_FREE_PATHS = ['/api/login', '/api.login']

@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # 非 API / WS 路径直接放行
    if not path.startswith('/api/') and not path.startswith('/ws/'):
        return await call_next(request)

    # 免认证路径放行
    if any(path.startswith(p) for p in AUTH_FREE_PATHS):
        return await call_next(request)

    # 提取 token
    auth = request.headers.get('Authorization', '')
    token = ''
    if auth.startswith('Bearer '):
        token = auth[7:]
    elif path.startswith('/ws/'):
        token = request.query_params.get('token', '')

    if not token:
        return JSONResponse(status_code=401, content={'detail': 'Missing token'})

    try:
        payload = decode_token(token)
        request.state.user_id = payload['sub']
        request.state.role = payload['role']
    except Exception:
        return JSONResponse(status_code=401, content={'detail': 'Invalid or expired token'})

    return await call_next(request)


# ==========================================
# 🛠️ 认证辅助函数
# ==========================================

def get_current_user_id(request: Request) -> str:
    return request.state.user_id

def require_admin(request: Request):
    if request.state.role != 'admin':
        raise HTTPException(status_code=403, detail='Admin only')


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def save_ws_log_to_db(plugin_id: str, level: str, message: str):
    with SessionLocal() as db_session:
        log_record = models.PluginLog(plugin_id=plugin_id, level=level, message=message)
        db_session.add(log_record)
        db_session.commit()


# ==========================================
# 🔑 登录端点
# ==========================================

@app.post('/api/login')
def login(body: dict, db: Session = Depends(get_db)):
    username = body.get('username', '')
    password = body.get('password', '')
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail='用户名或密码错误')
    if user.status != 'active':
        raise HTTPException(status_code=403, detail='账号已禁用')
    if user.expires_at and user.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail='账号已过期')
    token = create_token(user.id, user.role)
    return {'token': token, 'user': {'id': user.id, 'username': user.username, 'role': user.role}}


# ==========================================
# 👤 管理员用户管理端点
# ==========================================

@app.get('/api/admin/users')
def list_users(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    return [{
        'id': u.id, 'username': u.username, 'role': u.role, 'status': u.status,
        'max_plugins': u.max_plugins,
        'created_at': u.created_at.isoformat() if u.created_at else None,
        'expires_at': u.expires_at.isoformat() if u.expires_at else None
    } for u in users]


@app.post('/api/admin/users')
def create_user(body: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    user = models.User(
        username=body['username'],
        password_hash=hash_password(body['password']),
        role=body.get('role', 'user'),
        status=body.get('status', 'active'),
        max_plugins=body.get('max_plugins', 3),
        expires_at=datetime.fromisoformat(body['expires_at']) if body.get('expires_at') else None
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {'id': user.id, 'username': user.username, 'role': user.role}


@app.put('/api/admin/users/{user_id}')
def update_user(user_id: str, body: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    if 'status' in body:
        user.status = body['status']
    if 'expires_at' in body:
        user.expires_at = datetime.fromisoformat(body['expires_at']) if body['expires_at'] else None
    if 'password' in body and body['password']:
        user.password_hash = hash_password(body['password'])
    if 'role' in body:
        user.role = body['role']
    if 'max_plugins' in body:
        user.max_plugins = body['max_plugins']
    db.commit()
    return {'status': 'updated'}


@app.delete('/api/admin/users/{user_id}')
def delete_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    db.delete(user)
    db.commit()
    return {'status': 'deleted'}


# ==========================================
# 🔌 插件注册与状态管控
# ==========================================

# CR-4: 注册接口防刷 — 简单内存限速器（每 IP 每分钟最多 5 次）
_register_rate_limit: dict = {}

def _check_register_rate_limit(ip: str) -> bool:
    """返回 True 表示允许，False 表示限速"""
    now = time.time()
    if ip not in _register_rate_limit:
        _register_rate_limit[ip] = []
    # 清理 60 秒前的记录
    _register_rate_limit[ip] = [t for t in _register_rate_limit[ip] if now - t < 60]
    if len(_register_rate_limit[ip]) >= 5:
        return False
    _register_rate_limit[ip].append(now)
    return True

@app.post("/api/plugin/register")
def register_plugin(plugin_info: schemas.PluginRegister, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    client_ip = request.client.host if request.client else "unknown"

    # CR-4: 防刷检查 — 已有插件重连不算注册，跳过限速
    # 先查是否是已有插件重连
    db_plugin = None
    if plugin_info.plugin_id:
        db_plugin = db.query(models.Plugin).filter(
            models.Plugin.id == plugin_info.plugin_id,
            models.Plugin.user_id == user_id
        ).first()

    # 只对真正的新建插件限速
    if not db_plugin:
        if not _check_register_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="注册请求过于频繁，请稍后再试")

    # 🆕 检查用户绑定节点上限
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        existing_count = db.query(models.Plugin).filter(models.Plugin.user_id == user_id).count()
        if existing_count >= user.max_plugins:
            # 已有插件重连（允许），新建则拒绝
            if not db_plugin:
                raise HTTPException(status_code=403, detail=f"已达节点绑定上限（{user.max_plugins}个），请升级套餐或联系管理员")

    # 2. 如果没捞到（说明是新安装的插件，或者后端数据库被清空过）
    if not db_plugin:
        db_plugin = models.Plugin(
            # 如果插件自己带了 ID 就用它的，否则后端生成一个全新的 UUID
            id=plugin_info.plugin_id or str(uuid.uuid4()), 
            name=plugin_info.name, 
            status="inactive",
            user_id=user_id  # 🆕 归属当前登录用户
        )
        db.add(db_plugin)
        db.commit()
        db.refresh(db_plugin)
    else:
        # 3. 如果是老员工，顺便检查一下名字有没有变，变了就同步更新
        if db_plugin.name != plugin_info.name:
            db_plugin.name = plugin_info.name
            db.commit()
            
    return {"status": "registered", "plugin_id": db_plugin.id}

@app.get("/api/plugin/{plugin_id}/sync")
def sync_plugin_status(plugin_id: str, request: Request, db: Session = Depends(get_db)):
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    plugin.last_heartbeat = datetime.now(timezone.utc)
    db.commit()
    
    return {
        "status": plugin.status,
        "name": plugin.name,
        "is_ready": bool(plugin.model_id and plugin.email_id)
    }

@app.post("/api/plugin/status/{plugin_id}")
async def toggle_plugin(plugin_id: str, status_update: schemas.PluginStatusUpdate, request: Request, db: Session = Depends(get_db)):
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # 1. 更新数据库状态
    plugin.status = "active" if status_update.action == "start" else "inactive"
    db.commit()
    
    # 🌟 2. 核心进化：通过 WebSocket 瞬间下发控制指令！
    command_action = "REMOTE_START" if status_update.action == "start" else "REMOTE_STOP"
    
    # 使用 manager 精准打击对应的插件节点
    await manager.send_task_to_worker(plugin_id, {
        "type": "command",
        "action": command_action
    })
    
    return {"status": plugin.status}

@app.delete("/api/plugin/{plugin_id}")
def delete_plugin(plugin_id: str, request: Request, db: Session = Depends(get_db)):
    """删除节点（仅限自己的或 admin）"""
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Plugin).filter(models.Plugin.id == plugin_id)
    if not is_admin:
        query = query.filter(models.Plugin.user_id == user_id)
    plugin = query.first()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 删除关联的 CookieStore 记录
    db.query(models.CookieStore).filter(models.CookieStore.plugin_id == plugin_id).delete()
    # 🆕 清理任务组中引用此节点的 ID
    groups = db.query(models.TaskGroup).filter(models.TaskGroup.user_id == plugin.user_id).all()
    for g in groups:
        try:
            ids = json.loads(g.plugin_ids) if g.plugin_ids else []
            if plugin_id in ids:
                ids.remove(plugin_id)
                g.plugin_ids = json.dumps(ids) if ids else "[]"
        except: pass
    db.delete(plugin)
    db.commit()
    return {"status": "deleted"}

@app.post("/api/plugin/{plugin_id}/config")
def bind_plugin_config(plugin_id: str, config_data: dict, request: Request, db: Session = Depends(get_db)):
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    if "model_id" in config_data:
        # 🆕 验证模型归属
        if config_data["model_id"]:
            model = db.query(models.Model).filter(models.Model.id == config_data["model_id"]).first()
            if model and model.user_id and model.user_id != plugin.user_id and request.state.role != 'admin':
                raise HTTPException(status_code=403, detail="无权绑定他人的模型")
        plugin.model_id = config_data["model_id"]
    if "chat_model_id" in config_data:
        # 🆕 验证模型归属
        if config_data["chat_model_id"]:
            model = db.query(models.Model).filter(models.Model.id == config_data["chat_model_id"]).first()
            if model and model.user_id and model.user_id != plugin.user_id and request.state.role != 'admin':
                raise HTTPException(status_code=403, detail="无权绑定他人的模型")
        plugin.chat_model_id = config_data["chat_model_id"]
    if "email_id" in config_data:
        if config_data["email_id"]:
            email = db.query(models.Email).filter(models.Email.id == config_data["email_id"]).first()
            if email and email.user_id and email.user_id != plugin.user_id and request.state.role != 'admin':
                raise HTTPException(status_code=403, detail="无权绑定他人的邮箱")
        plugin.email_id = config_data["email_id"]
    if "alert_email_id" in config_data:
        if config_data["alert_email_id"]:
            email = db.query(models.Email).filter(models.Email.id == config_data["alert_email_id"]).first()
            if email and email.user_id and email.user_id != plugin.user_id and request.state.role != 'admin':
                raise HTTPException(status_code=403, detail="无权绑定他人的邮箱")
        plugin.alert_email_id = config_data["alert_email_id"]
    db.commit()
    return {"status": "bound"}

# ==========================================
# 📥 采集数据接收 (严格类型校验版)
# ==========================================
@app.post("/api/collect")
async def collect_products(payload: schemas.CollectPayload, request: Request, db: Session = Depends(get_db)):
    """B12 修复：改为 async，避免同步阻塞事件循环"""
    from sqlalchemy.exc import IntegrityError
    
    plugin_id = payload.plugin_id
    queued_count = 0
    queue_name = f"product_queue:{plugin_id}" if plugin_id else "product_queue:default"

    # 🆕 确定商品归属用户（验证插件归属）
    user_id = get_current_user_id(request)
    if plugin_id:
        plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
        if plugin:
            if plugin.user_id != user_id:
                raise HTTPException(status_code=403, detail="Plugin not owned by current user")
            user_id = plugin.user_id

    # 用 Redis Pipeline 批量去重，减少网络往返
    pipe = redis_client.pipeline()
    new_items = []
    
    for record in payload.data:
        item_id = record.item.id
        platform = record.platform
        if not item_id: continue
            
        dedup_key = f"radar:alerted:{user_id}:{platform}:{item_id}"
        pipe.set(dedup_key, "1", ex=604800, nx=True)
        new_items.append((record, item_id, platform))
    
    # OPT-7: 批量执行 Redis 去重查询（用 run_in_threadpool 避免阻塞事件循环）
    dedup_results = await run_in_threadpool(pipe.execute)
    
    for (record, item_id, platform), is_new in zip(new_items, dedup_results):
        if is_new:
            existing_product = db.query(models.Product).filter(
                models.Product.item_id == item_id,
                models.Product.platform == platform,
                models.Product.user_id == user_id
            ).first()

            if not existing_product:
                try:
                    new_product = models.Product(
                        item_id=item_id,
                        platform=platform, 
                        title=record.item.title,
                        price=str(record.item.price),
                        raw_data=json.dumps(record.model_dump(), ensure_ascii=False),
                        user_id=user_id  # 🆕 归属用户
                    )
                    db.add(new_product)
                    db.flush()  # IM-4: 提前 flush 检测唯一约束冲突
                except IntegrityError:
                    db.rollback()  # IM-4: 竞态冲突时回滚单条，继续处理其他
            
            redis_client.rpush(queue_name, json.dumps({
                "plugin_id": plugin_id, 
                "item": record.model_dump() 
            }, ensure_ascii=False))
            queued_count += 1
    
    await run_in_threadpool(db.commit)
    return {"status": "success", "queued": queued_count}

# ==========================================
# 📊 仪表盘与大盘数据统计
# ==========================================
@app.get("/api/dashboard/stats")
def get_stats(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    if is_admin:
        total_products = db.query(models.Product).count()
        pending = db.query(models.Product).filter(models.Product.status == "pending").count()
        approved = db.query(models.Product).filter(models.Product.status == "approved").count()
        plugins = db.query(models.Plugin).filter(models.Plugin.status == "active").count()
    else:
        total_products = db.query(models.Product).filter(models.Product.user_id == user_id).count()
        pending = db.query(models.Product).filter(models.Product.user_id == user_id, models.Product.status == "pending").count()
        approved = db.query(models.Product).filter(models.Product.user_id == user_id, models.Product.status == "approved").count()
        plugins = db.query(models.Plugin).filter(models.Plugin.user_id == user_id, models.Plugin.status == "active").count()

    return {
        "total_products": total_products, 
        "pending_evaluation": pending, 
        "approved": approved, 
        "active_plugins": plugins
    }

# ==========================================
# 🛍️ 商品管理列表
# ==========================================
@app.get("/api/products")
def list_products(page: int = 1, size: int = 20, status: str = None, search: str = None,
                  ai_decision: str = None, sort: str = "time",
                  date_from: str = None, date_to: str = None,
                  request: Request = None, db: Session = Depends(get_db)):
    size = min(size, 100)  # OPT-2: 防止超大分页拖垮数据库
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    query = db.query(models.Product)
    # 🆕 非管理员只看自己的
    if not is_admin:
        query = query.filter(models.Product.user_id == user_id)
    if status:
        query = query.filter(models.Product.status == status)
    if search:
        query = query.filter(models.Product.title.contains(search))
    # 🆕 日期筛选：默认只显示今天
    from datetime import date, timedelta
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            query = query.filter(models.Product.created_at >= dt_from)
        except: pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to) + timedelta(days=1)
            query = query.filter(models.Product.created_at < dt_to)
        except: pass
    if not date_from and not date_to:
        # 默认只显示今天
        today_start = datetime.combine(date.today(), datetime.min.time())
        query = query.filter(models.Product.created_at >= today_start)

    total = query.count()
    products = query.order_by(models.Product.created_at.desc()).all()

    # 从 raw_data 提取卡片展示字段
    # 🆕 admin 查看时加载 username 映射
    user_map = {}
    if is_admin:
        users = db.query(models.User.id, models.User.username).all()
        user_map = {u.id: u.username for u in users}

    enriched = []
    for p in products:
        raw = {}
        try:
            raw = json.loads(p.raw_data) if p.raw_data else {}
        except Exception:
            pass

        item_info = raw.get("item", {})
        seller_info = raw.get("seller", {})
        features_info = raw.get("features", {})
        ai_eval = json.loads(p.ai_evaluation) if p.ai_evaluation else None

        # AI 决策筛选
        decision = ai_eval.get("决策", "") if ai_eval else ""
        if ai_decision and decision != ai_decision:
            continue

        enriched.append({
            "id": p.id,
            "item_id": p.item_id,
            "title": p.title,
            "price": p.price,
            "status": p.status,
            "ai_evaluation": ai_eval,
            "created_at": p.created_at.isoformat(),
            # 卡片展示字段
            "image_url": item_info.get("picUrl", ""),
            "url": item_info.get("url", ""),
            "specs": item_info.get("specs", ""),
            "location": item_info.get("location", ""),
            "publish_time": item_info.get("publishTime", ""),
            "seller_nickname": seller_info.get("nickname", ""),
            "seller_type": seller_info.get("type", ""),
            "seller_credit": seller_info.get("creditRate", ""),
            "seller_good_rate": seller_info.get("goodRate", ""),
            "seller_avatar": seller_info.get("avatarUrl", ""),
            "is_verified": features_info.get("isVerified", False),
            "is_free_shipping": features_info.get("isFreeShipping", False),
            "has_video": features_info.get("hasVideo", False),
            "is_seven_days_return": features_info.get("isSevenDaysReturn", False),
            "tags": features_info.get("tags", []),
            "platform": raw.get("platform", "goofish"),
            "platform_cn": raw.get("platformCN", "闲鱼"),
            "keyword": raw.get("keyword", ""),
            "user_id": p.user_id,
            "username": user_map.get(p.user_id, "-") if is_admin else None,
        })

    # 排序
    decision_order = {"速秒": 0, "可入": 1, "需人工复核": 2, "跳过": 3}
    if sort == "price_asc":
        enriched.sort(key=lambda x: float(x["price"]) if x["price"] else 999999)
    elif sort == "price_desc":
        enriched.sort(key=lambda x: float(x["price"]) if x["price"] else 0, reverse=True)
    elif sort == "ai_priority":
        enriched.sort(key=lambda x: decision_order.get(x["ai_evaluation"].get("决策", "") if x["ai_evaluation"] else "", 4))
    # 默认 time 已经是 created_at desc

    # 分页
    total_filtered = len(enriched)
    start = (page - 1) * size
    items_page = enriched[start:start + size]

    return {"total": total_filtered, "items": items_page}

@app.put("/api/products/{product_id}/status")
def update_product_status(product_id: str, status_data: dict, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    query = db.query(models.Product).filter(models.Product.id == product_id)
    if not is_admin:
        query = query.filter(models.Product.user_id == user_id)
    product = query.first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.status = status_data.get("status", product.status)
    db.commit()
    return {"status": "updated"}

# ==========================================
# 🔌 插件列表管理 (升级：融合内存实时 WS 状态)
# ==========================================
@app.get("/api/plugins")
def list_plugins(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    # B11 修复：用 joinedload 一次性加载关联数据，消除 N+1 查询
    from sqlalchemy.orm import joinedload
    query = db.query(models.Plugin).options(
        joinedload(models.Plugin.model),
        joinedload(models.Plugin.chat_model),
        joinedload(models.Plugin.email),
        joinedload(models.Plugin.alert_email)
    )
    # 🆕 非管理员只看自己的
    if not is_admin:
        query = query.filter(models.Plugin.user_id == user_id)
    plugins = query.all()

    result = []
    # 🆕 admin 查看时加载 username 映射
    user_map = {}
    if is_admin:
        users = db.query(models.User.id, models.User.username).all()
        user_map = {u.id: u.username for u in users}

    for p in plugins:
        # 🌟 核心：直接从 manager 获取该节点的真实物理连接状态！
        real_ws_status = manager.node_status.get(p.id, "offline")

        result.append({
            "id": p.id,
            "name": p.name,
            "status": p.status,
            "ws_status": real_ws_status,
            "user_id": p.user_id,
            "username": user_map.get(p.user_id, "-") if is_admin else None,
            "registered_at": p.registered_at.isoformat(),
            "last_heartbeat": p.last_heartbeat.isoformat() if p.last_heartbeat else None,
            "model": {"id": p.model.id, "name": p.model.name} if p.model else None,
            "chat_model": {"id": p.chat_model.id, "name": p.chat_model.name} if p.chat_model else None,
            "email": {"id": p.email.id, "name": p.email.name, "sender": p.email.sender} if p.email else None,
            "alert_email": {"id": p.alert_email.id, "name": p.alert_email.name, "sender": p.alert_email.sender} if p.alert_email else None
        })
    return result

# ==========================================
# 🧠 模型管理 (全局共享，仅管理员可写)
# ==========================================
@app.get("/api/models")
def list_models(request: Request, db: Session = Depends(get_db)):
    # 🆕 非管理员只看自己的模型
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Model)
    if not is_admin:
        query = query.filter(models.Model.user_id == user_id)
    # 🆕 admin 查看时加载 username 映射
    user_map = {}
    if is_admin:
        users = db.query(models.User.id, models.User.username).all()
        user_map = {u.id: u.username for u in users}

    result = []
    for m in query.all():
        result.append({
            "id": m.id, "name": m.name, "model_name": m.model_name,
            "base_url": m.base_url, "api_key": "***",
            "prompt_template": m.prompt_template, "user_id": m.user_id,
            "username": user_map.get(m.user_id, "-") if is_admin else None,
        })
    return result

@app.post("/api/models")
def create_model(model_data: schemas.ModelCreate, request: Request, db: Session = Depends(get_db)):
    # 🆕 任何用户都可以创建自己的模型
    user_id = get_current_user_id(request)
    db_model = models.Model(
        user_id=user_id,
        name=model_data.name,
        model_name=model_data.model_name,
        base_url=model_data.base_url,
        api_key=encrypt_value(model_data.api_key),
        prompt_template=model_data.prompt_template
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return {"id": db_model.id, "name": db_model.name, "model_name": db_model.model_name,
            "base_url": db_model.base_url, "api_key": "****", "prompt_template": db_model.prompt_template}

@app.put("/api/models/{model_id}")
def update_model(model_id: str, model_data: schemas.ModelCreate, request: Request, db: Session = Depends(get_db)):
    # 🆕 只能修改自己的模型，admin 可修改任意
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Model).filter(models.Model.id == model_id)
    if not is_admin:
        query = query.filter(models.Model.user_id == user_id)
    db_model = query.first()
    if not db_model:
        raise HTTPException(status_code=404, detail="Model not found")
    db_model.name = model_data.name
    db_model.model_name = model_data.model_name
    db_model.base_url = model_data.base_url
    # 只有传了真实 api_key（不是 ****）才更新
    if model_data.api_key and model_data.api_key != "****":
        db_model.api_key = encrypt_value(model_data.api_key)
    db_model.prompt_template = model_data.prompt_template
    db.commit()
    return {"id": db_model.id, "name": db_model.name, "model_name": db_model.model_name,
            "base_url": db_model.base_url, "api_key": "****", "prompt_template": db_model.prompt_template}

@app.delete("/api/models/{model_id}")
def delete_model(model_id: str, request: Request, db: Session = Depends(get_db)):
    # 🆕 只能删除自己的模型，admin 可删除任意
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Model).filter(models.Model.id == model_id)
    if not is_admin:
        query = query.filter(models.Model.user_id == user_id)
    db_model = query.first()
    if not db_model:
        raise HTTPException(status_code=404, detail="Model not found")
    db.delete(db_model)
    db.commit()
    return {"status": "deleted"}

# ==========================================
# 📧 邮箱管理 (多租户隔离)
# ==========================================
@app.get("/api/emails")
def list_emails(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    # 🆕 非管理员只看自己的
    if is_admin:
        emails = db.query(models.Email).all()
    else:
        emails = db.query(models.Email).filter(models.Email.user_id == user_id).all()
    # 隐藏 auth_code 明文
    # 🆕 admin 查看时加载 username 映射
    user_map = {}
    if is_admin:
        users = db.query(models.User.id, models.User.username).all()
        user_map = {u.id: u.username for u in users}

    result = []
    for e in emails:
        result.append({
            "id": e.id, "name": e.name, "sender": e.sender,
            "receiver": e.receiver, "auth_code": "****",
            "service": e.service, "port": e.port,
            "html_template": e.html_template, "user_id": e.user_id,
            "username": user_map.get(e.user_id, "-") if is_admin else None,
        })
    return result

@app.post("/api/emails")
def create_email(email_data: schemas.EmailCreate, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    db_email = models.Email(
        name=email_data.name,
        sender=email_data.sender,
        receiver=email_data.receiver,
        auth_code=encrypt_value(email_data.auth_code),
        service=email_data.service,
        port=email_data.port,
        html_template=email_data.html_template,
        user_id=user_id  # 🆕 归属当前用户
    )
    db.add(db_email)
    db.commit()
    db.refresh(db_email)
    return {"id": db_email.id, "name": db_email.name, "sender": db_email.sender,
            "receiver": db_email.receiver, "auth_code": "****",
            "service": db_email.service, "port": db_email.port,
            "html_template": db_email.html_template}

@app.put("/api/emails/{email_id}")
def update_email(email_id: str, email_data: schemas.EmailCreate, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Email).filter(models.Email.id == email_id)
    if not is_admin:
        query = query.filter(models.Email.user_id == user_id)
    db_email = query.first()
    if not db_email:
        raise HTTPException(status_code=404, detail="Email not found")
    db_email.name = email_data.name
    db_email.sender = email_data.sender
    db_email.receiver = email_data.receiver
    # 只有传了真实 auth_code（不是 ****）才更新
    if email_data.auth_code and email_data.auth_code != "****":
        db_email.auth_code = encrypt_value(email_data.auth_code)
    db_email.service = email_data.service
    db_email.port = email_data.port
    db_email.html_template = email_data.html_template
    db.commit()
    return {"id": db_email.id, "name": db_email.name, "sender": db_email.sender,
            "receiver": db_email.receiver, "auth_code": "****",
            "service": db_email.service, "port": db_email.port,
            "html_template": db_email.html_template}

@app.delete("/api/emails/{email_id}")
def delete_email(email_id: str, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.Email).filter(models.Email.id == email_id)
    if not is_admin:
        query = query.filter(models.Email.user_id == user_id)
    db_email = query.first()
    if not db_email:
        raise HTTPException(status_code=404, detail="Email not found")
    db.delete(db_email)
    db.commit()
    return {"status": "deleted"}

# ==========================================
# 📝 插件日志获取 (HTTP 兜底通道)
# ==========================================
@app.post("/api/plugin/{plugin_id}/log")
def receive_plugin_log(plugin_id: str, log_entry: dict, background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    db_plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not db_plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if db_plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    log = models.PluginLog(plugin_id=plugin_id, level=log_entry.get("level", "INFO"), message=log_entry.get("message", ""))
    db.add(log)
    db.commit()

    # 🌟 修复点：改用 background_tasks
    background_tasks.add_task(manager.broadcast_to_plugin, plugin_id, {
        "type": "log",
        "timestamp": log.timestamp.isoformat(),
        "level": log.level,
        "message": log.message
    })
    return {"status": "logged"}

@app.get("/api/plugin/{plugin_id}/logs")
def get_plugin_logs(plugin_id: str, limit: int = 100, request: Request = None, db: Session = Depends(get_db)):
    db_plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not db_plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if db_plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")

    logs = db.query(models.PluginLog).filter(models.PluginLog.plugin_id == plugin_id).order_by(models.PluginLog.timestamp.desc()).limit(limit).all()
    return [{"timestamp": l.timestamp.isoformat(), "level": l.level, "message": l.message} for l in logs]

# ==========================================
# 🌐 WebSocket 实时双向通道
# ==========================================
@app.websocket("/ws/{plugin_id}")
async def websocket_endpoint(websocket: WebSocket, plugin_id: str):
    # 🌟 JWT 鉴权 — 从 query param 提取 token
    requested_role = websocket.query_params.get("role", "plugin")
    user_id = getattr(websocket.scope.get("state", None), "user_id", None)
    actual_role = getattr(websocket.scope.get("state", None), "role", None)

    # 如果中间件没设置，手动解码 token
    if not user_id:
        ws_token = websocket.query_params.get("token", "")
        if not ws_token:
            await websocket.close(code=4003, reason="Unauthorized: missing token")
            return
        try:
            payload = decode_token(ws_token)
            user_id = payload['sub']
            actual_role = payload.get('role', 'user')
        except Exception:
            await websocket.close(code=4003, reason="Unauthorized: invalid token")
            return

    # WS role 由 URL 参数决定（plugin/admin），JWT 只做身份验证
    # dashboard_global_admin 强制 admin，其他用 URL 参数的 role
    if plugin_id == "dashboard_global_admin":
        role = "admin"
    elif requested_role in ("plugin", "admin"):
        role = requested_role
    else:
        role = actual_role or "plugin"

    print(f"🔌 新的 WebSocket 连接: Plugin ID={plugin_id}, Role={role}, User={user_id}")

    # 🆕 验证插件归属（admin 可连接任意插件）
    if role != "admin":
        with SessionLocal() as db_session:
            plugin = db_session.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
            if not plugin:
                await websocket.close(code=4004, reason="Plugin not found")
                return
            if plugin.user_id != user_id:
                await websocket.close(code=4003, reason="Forbidden: plugin not owned by user")
                return

    await manager.connect(plugin_id, websocket, role, user_id if role == "plugin" else None)
    try:
        while True:
            data = await websocket.receive_text()
            # B3 修复：JSON 解析保护，防止非法消息导致连接崩溃
            try:
                log_data = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            if log_data.get("type") == "ping":
                manager.last_ping_time[plugin_id] = time.time()
                await websocket.send_json({"type": "pong", "server_time": time.time()})
                continue
            
            if log_data.get("type") == "status_update":
                if role == "plugin":
                    manager.update_status(plugin_id, log_data.get("status"))
                continue
            
            if log_data.get("type") == "log":
                await manager.broadcast_to_plugin(plugin_id, log_data)
                await run_in_threadpool(
                    save_ws_log_to_db, 
                    plugin_id, 
                    log_data.get("level", "INFO"), 
                    log_data.get("message", "")
                )
    except WebSocketDisconnect as e:
        print(f"👋 WebSocket 连接断开: Plugin ID={plugin_id}, Role={role}, Reason={e.code}")
        manager.disconnect(plugin_id, websocket, role)
        # B10 修复：插件断线时不再自动改为 inactive（用户手动启用的状态应保留）
        # 只更新 last_heartbeat，不改 status

    except Exception as e:
        print(f"❌ WebSocket 连接异常: Plugin ID={plugin_id}, Role={role}, Error={e}") 
        manager.disconnect(plugin_id, websocket, role)


@app.post("/api/plugin/{plugin_id}/alert")
def trigger_alert(plugin_id: str, alert: schemas.PluginAlertPayload, background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    # 🆕 验证归属
    if plugin.user_id != get_current_user_id(request) and request.state.role != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")

    log_record = models.PluginLog(plugin_id=plugin_id, level="ERROR", message=alert.message)
    db.add(log_record)
    plugin.status = "inactive"
    db.commit()

    background_tasks.add_task(manager.broadcast_to_plugin, plugin_id, {
        "type": "log", "timestamp": log_record.timestamp.isoformat(), "level": "ERROR", "message": log_record.message
    })

    # 🌟 核心拦截逻辑：如果有专门的报警通道就用专门的，没有就兜底用商品推送通道！
    target_email_id = plugin.alert_email_id or plugin.email_id 
    if target_email_id:
        email_config = db.query(models.Email).filter(models.Email.id == target_email_id).first()
        if email_config:
            background_tasks.add_task(send_emergency_email, email_config, plugin.name, alert.message)

    return {"status": "alert_processed"}


# ==========================================
# 📦 任务组管理 (用户自定义节点组合)
# ==========================================
@app.get("/api/task-groups")
def list_task_groups(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.TaskGroup)
    if not is_admin:
        query = query.filter(models.TaskGroup.user_id == user_id)
    groups = query.order_by(models.TaskGroup.created_at.desc()).all()
    result = []
    for g in groups:
        plugin_ids = []
        try:
            plugin_ids = json.loads(g.plugin_ids) if g.plugin_ids else []
        except: pass
        result.append({
            "id": g.id, "name": g.name, "plugin_ids": plugin_ids,
            "user_id": g.user_id, "created_at": g.created_at.isoformat() if g.created_at else None
        })
    return result

@app.post("/api/task-groups")
def create_task_group(body: dict, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    name = body.get("name", "").strip()
    plugin_ids = body.get("plugin_ids", [])
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if not plugin_ids:
        raise HTTPException(status_code=400, detail="至少选择一个节点")
    # 🆕 验证 plugin_ids 归属
    query = db.query(models.Plugin.id).filter(models.Plugin.id.in_(plugin_ids))
    if not is_admin:
        query = query.filter(models.Plugin.user_id == user_id)
    valid_ids = {r[0] for r in query.all()}
    invalid = [pid for pid in plugin_ids if pid not in valid_ids]
    if invalid:
        raise HTTPException(status_code=400, detail=f"无权操作以下节点: {', '.join(invalid[:3])}")
    group = models.TaskGroup(
        user_id=user_id,
        name=name,
        plugin_ids=json.dumps(plugin_ids, ensure_ascii=False)
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"id": group.id, "name": group.name, "plugin_ids": plugin_ids}

@app.delete("/api/task-groups/{group_id}")
def delete_task_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'
    query = db.query(models.TaskGroup).filter(models.TaskGroup.id == group_id)
    if not is_admin:
        query = query.filter(models.TaskGroup.user_id == user_id)
    group = query.first()
    if not group:
        raise HTTPException(status_code=404, detail="任务组不存在")
    db.delete(group)
    db.commit()
    return {"status": "deleted"}

# ==========================================
# 🔍 节点诊断 (排查任务派发问题)
# ==========================================
@app.get("/api/tasks/diagnose")
def diagnose_nodes(request: Request, db: Session = Depends(get_db)):
    """诊断当前用户节点状态，排查为什么任务派发不出去"""
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    # 1. 数据库中的节点
    query = db.query(models.Plugin)
    if not is_admin:
        query = query.filter(models.Plugin.user_id == user_id)
    db_plugins = query.all()

    # 2. WebSocket 连接状态
    ws_connected = set(manager.worker_connections.keys())
    all_status = dict(manager.node_status)
    all_owner = dict(manager.node_owner)

    results = []
    for p in db_plugins:
        is_ws_connected = p.id in ws_connected
        ws_status = all_status.get(p.id, "未连接")
        owner_match = all_owner.get(p.id) == user_id
        has_model = bool(p.model_id)
        has_email = bool(p.email_id)

        issue = None
        if not is_ws_connected:
            issue = "WebSocket 未连接 — Chrome 插件是否打开？后端地址是否正确？"
        elif not owner_match:
            issue = "节点归属不匹配"
        elif ws_status == "standby":
            issue = "节点待机中 — 需要点击启动或下发任务时会自动唤醒"
        elif ws_status == "working":
            issue = "节点正在执行任务"
        elif not has_model:
            issue = "未绑定 AI 模型"
        elif not has_email:
            issue = "未绑定邮箱"
        elif ws_status == "idle":
            issue = None  # 正常

        results.append({
            "id": p.id[:8] + "...",
            "full_id": p.id,
            "name": p.name,
            "db_status": p.status,
            "ws_connected": is_ws_connected,
            "ws_status": ws_status,
            "has_model": has_model,
            "has_email": has_email,
            "ready": is_ws_connected and ws_status == "idle" and has_model and has_email,
            "issue": issue,
        })

    ready_count = sum(1 for r in results if r["ready"])
    return {
        "user_id": user_id,
        "total_nodes": len(results),
        "ready_nodes": ready_count,
        "ws_total_connected": len(ws_connected),
        "nodes": results,
    }


# ==========================================
# 🚀 任务派发中心 (带 MySQL 持久化)
# ==========================================
@app.post("/api/tasks/publish")
async def publish_cloud_task(task_req: schemas.CloudTaskRequest, request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)

    # 🌟 1. 永久保存到数据库
    db_task = models.Task(
        platformEN=task_req.platformEN,
        keywords=",".join(task_req.keywords),
        params_json=json.dumps(task_req.dict()),
        task_type=task_req.task_type,
        status="active",
        user_id=user_id
    )
    db.add(db_task)
    db.commit()

    # 🆕 解析任务组 → 插件列表
    target_plugin_ids = []
    if task_req.task_group_id:
        group = db.query(models.TaskGroup).filter(
            models.TaskGroup.id == task_req.task_group_id,
            models.TaskGroup.user_id == user_id
        ).first()
        if not group:
            raise HTTPException(status_code=404, detail="任务组不存在或无权访问")
        try:
            target_plugin_ids = json.loads(group.plugin_ids) or []
        except:
            raise HTTPException(status_code=400, detail="任务组数据异常")
        if not target_plugin_ids:
            raise HTTPException(status_code=400, detail="任务组内无节点")
    elif task_req.target_plugin_id:
        target_plugin_ids = [task_req.target_plugin_id]

    # 🌟 自动激活待机中的节点（下发任务时无需手动点"启动"）
    activated = 0
    nodes_to_activate = []
    for pid, status in manager.node_status.items():
        if status == "standby" and manager.node_owner.get(pid) == user_id:
            # 如果指定了节点组，只激活组内的节点
            if target_plugin_ids and pid not in target_plugin_ids:
                continue
            # 检查节点是否已配置 AI 模型和邮箱
            plugin = db.query(models.Plugin).filter(models.Plugin.id == pid).first()
            if plugin and plugin.model_id and plugin.email_id:
                nodes_to_activate.append(pid)

    for pid in nodes_to_activate:
        manager.node_status[pid] = "idle"
        plugin = db.query(models.Plugin).filter(models.Plugin.id == pid).first()
        if plugin:
            plugin.status = "active"
        await manager.send_task_to_worker(pid, {"type": "command", "action": "REMOTE_START"})
        activated += 1
    if activated:
        db.commit()
        print(f"⚡ [自动激活] 下发任务时自动唤醒了 {activated} 个待机节点")

    # 🌟 2. 塞入 Redis 接力池
    count = 0
    for idx, kw in enumerate(task_req.keywords):
        task_dict = task_req.dict()
        del task_dict["keywords"]
        task_dict["keyword"] = kw
        task_dict["user_id"] = user_id

        # 🆕 如果指定了节点组，按轮询分配
        if target_plugin_ids:
            task_dict["target_plugin_id"] = target_plugin_ids[idx % len(target_plugin_ids)]

        redis_client.rpush(manager.QUEUE_KEY, json.dumps(task_dict))
        count += 1

    # 统计当前可用节点数 + 诊断信息
    idle_count = 0
    diagnostic = []
    user_plugins = db.query(models.Plugin).filter(models.Plugin.user_id == user_id).all()
    for p in user_plugins:
        is_ws = p.id in manager.worker_connections
        ws_st = manager.node_status.get(p.id, "未连接")
        if is_ws and ws_st == "idle":
            idle_count += 1
        elif not is_ws:
            diagnostic.append(f"节点 [{p.name}] WebSocket 未连接")
        elif ws_st == "standby":
            diagnostic.append(f"节点 [{p.name}] 待机中(已尝试自动唤醒)")
        elif ws_st == "working":
            diagnostic.append(f"节点 [{p.name}] 正在执行任务")
        else:
            diagnostic.append(f"节点 [{p.name}] 状态={ws_st}")

    if not user_plugins:
        diagnostic.append("没有绑定任何节点，请先在 Chrome 插件中注册节点")

    manager.trigger_dispatch()
    msg = f"✅ 已成功下发 {count} 个任务"
    if activated:
        msg += f"，自动唤醒了 {activated} 个节点"
    if idle_count == 0:
        msg += f"。⚠️ 当前没有空闲节点"
        if diagnostic:
            msg += f"：{'；'.join(diagnostic[:3])}"
    else:
        msg += f"。当前有 {idle_count} 个空闲节点待命"
    return {"status": "success", "msg": msg, "idle_count": idle_count, "diagnostic": diagnostic}

@app.delete("/api/tasks/clear")
async def clear_cloud_tasks(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    # 1. 处理 Redis 队列
    if is_admin:
        # 管理员：清空整个队列
        count = redis_client.llen(manager.QUEUE_KEY)
        redis_client.delete(manager.QUEUE_KEY)
    else:
        # 普通用户：只移除属于自己的任务，保留其他人的
        total = redis_client.llen(manager.QUEUE_KEY)
        kept = []
        removed = 0
        for _ in range(total):
            item = redis_client.lpop(manager.QUEUE_KEY)
            if not item:
                break
            try:
                data = json.loads(item)
                if data.get("user_id") != user_id:
                    kept.append(item)
                else:
                    removed += 1
            except:
                kept.append(item)
        if kept:
            redis_client.rpush(manager.QUEUE_KEY, *kept)
        count = removed

    # 🌟 召回正在工作的插件（紧急刹车，非 admin 只召回自己的）
    await manager.cancel_all_working_tasks(user_id if not is_admin else None)

    # 🌟同步改变数据库中任务的状态
    if is_admin:
        db.query(models.Task).filter(models.Task.status == "active").update({"status": "cleared"})
    else:
        db.query(models.Task).filter(models.Task.status == "active", models.Task.user_id == user_id).update({"status": "cleared"})
    db.commit()

    return {"status": "success", "msg": f"🗑️ 队列已清空，并已拦截所有正在执行的 {count} 个子任务！"}

@app.get("/api/tasks/history")
def get_task_history(request: Request, db: Session = Depends(get_db)):
    """🌟 新增：获取历史任务列表"""
    user_id = get_current_user_id(request)
    is_admin = request.state.role == 'admin'

    query = db.query(models.Task)
    if not is_admin:
        query = query.filter(models.Task.user_id == user_id)
    tasks = query.order_by(models.Task.created_at.desc()).limit(20).all()

    return [{
        "id": t.id,
        "platformEN": t.platformEN,
        "keywords": t.keywords,
        "task_type": t.task_type,
        "status": t.status,
        "created_at": t.created_at.isoformat(),
        "params": json.loads(t.params_json)
    } for t in tasks]


# ==========================================
# 🔘 批量操作 (仅管理员)
# ==========================================
@app.post("/api/plugins/toggle_all")
async def toggle_all_nodes(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)  # 🆕 仅管理员
    action = payload.get("action")
    online_ids = list(manager.active_connections.keys())
    nodes = db.query(models.Plugin).filter(models.Plugin.id.in_(online_ids)).all()
    
    target_ids = []
    skipped = 0  # 🌟 专门统计因为没配大脑而被跳过的机器
    
    if action == "start":
        for n in nodes:
            if manager.node_status.get(n.id) == "standby":
                # 必须配了大脑和邮箱才能上岗
                if n.model_id and n.email_id:
                    n.status = "active"
                    target_ids.append(n.id)
                    await manager.send_task_to_worker(n.id, {"type": "command", "action": "REMOTE_START"})
                else:
                    skipped += 1
    else:
        for n in nodes:
            if manager.node_status.get(n.id) in ["idle", "working"]:
                n.status = "inactive"
                target_ids.append(n.id)
                # 剥夺任务交还队列
                if n.id in manager.working_tasks:
                    task_data = manager.working_tasks.pop(n.id)
                    redis_client.rpush(manager.QUEUE_KEY, json.dumps(task_data))
                await manager.send_task_to_worker(n.id, {"type": "command", "action": "REMOTE_STOP"})
                manager.node_status[n.id] = "standby"

    db.commit()
    
    # 🌟 组合详细的回报信息反馈给前端
    msg = f"✅ 成功{'唤醒' if action == 'start' else '停用'} {len(target_ids)} 个节点"
    if skipped > 0:
        msg += f" (跳过 {skipped} 个未配 AI/邮箱 的节点)"
        
    return {"status": "success", "count": len(target_ids), "skipped": skipped, "msg": msg}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=15001)
