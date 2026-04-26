"""
闲鱼私聊整合 — FastAPI 路由
Cookie 管理 + 对话列表 + 消息发送 + 管理端 WS 推送
(已加入 user_id 多租户隔离)
"""
import uuid
import json
import asyncio
from typing import Optional, Dict

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request
from sqlalchemy.orm import Session
from loguru import logger

from .. import models, schemas, database
from ..crypto import encrypt_value, decrypt_value
from ..auth import decode_token
from .utils import parse_cookies, get_user_id

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ==========================================
# 管理端 WebSocket 连接池 (user_id → {ws})
# ==========================================

# 每个 WS 连接记录其对应的 user_id，用于推送隔离
_admin_ws_map: Dict[WebSocket, str] = {}   # ws -> user_id


def _get_owner_id_for_conversation(db: Session, conversation_id: str) -> Optional[str]:
    """通过 conversation → CookieStore.owner_id 查找对话归属用户"""
    row = (
        db.query(models.CookieStore.owner_id)
        .join(
            models.Conversation,
            models.Conversation.plugin_id == models.CookieStore.plugin_id,
        )
        .filter(models.Conversation.id == conversation_id)
        .first()
    )
    return row[0] if row else None


async def broadcast_to_admins(message: dict):
    """
    向归属用户匹配的管理端 WebSocket 推送消息。
    如果消息包含 conversation_id，则只推送给该对话归属用户的连接；
    无法确定归属时跳过推送（fail-closed）。
    """
    target_user_id: Optional[str] = None
    conversation_id = message.get("conversation_id")

    if conversation_id:
        try:
            db = database.SessionLocal()
            try:
                target_user_id = _get_owner_id_for_conversation(db, conversation_id)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[chat-ws] broadcast 查找 owner 失败: {e}")
            return  # 🆕 无法确定归属，跳过推送

    # 🆕 有 conversation_id 但无法确定 owner，跳过
    if conversation_id and not target_user_id:
        logger.warning(f"[chat-ws] 无法确定 conversation {conversation_id} 的归属用户，跳过推送")
        return

    dead = set()
    for ws, uid in _admin_ws_map.items():
        # 如果能确定 owner，只推送给该用户的连接
        if target_user_id and uid != target_user_id:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _admin_ws_map.pop(ws, None)


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# 用户身份提取 (由 auth 中间件写入 request.state.user_id)
# ==========================================

def get_current_user_id(request: Request) -> str:
    """从 request.state.user_id 获取当前登录用户 ID"""
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="未认证：缺少 user_id")
    return uid


# ==========================================
# Cookie 管理
# ==========================================

@router.post("/cookie/sync")
def sync_cookie(
    payload: schemas.CookieSync,
    request: Request,
    db: Session = Depends(get_db),
):
    """插件节点同步 cookie (CDP 自动提取)"""
    plugin_id = payload.plugin_id
    cookie_str = payload.cookies
    if not plugin_id or not cookie_str:
        raise HTTPException(status_code=400, detail="plugin_id 和 cookies 必填")

    cookies = parse_cookies(cookie_str)
    xianyu_uid = get_user_id(cookies)  # 闲鱼用户ID (unb)

    # 确定归属用户：优先从 Plugin.user_id 取，其次从请求上下文取
    owner_id: Optional[str] = None
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if plugin:
        # 🆕 验证插件归属
        request_user_id = getattr(request.state, "user_id", None)
        if plugin.user_id != request_user_id and getattr(request.state, "role", "") != "admin":
            raise HTTPException(status_code=403, detail="无权操作此插件的 Cookie")
        owner_id = plugin.user_id
    else:
        owner_id = getattr(request.state, "user_id", None)

    record = db.query(models.CookieStore).filter(
        models.CookieStore.plugin_id == plugin_id
    ).first()

    if record:
        record.cookie_enc = encrypt_value(cookie_str)
        record.user_id = xianyu_uid
        record.status = "active"
        if owner_id:
            record.owner_id = owner_id
    else:
        record = models.CookieStore(
            plugin_id=plugin_id,
            owner_id=owner_id,
            user_id=xianyu_uid,
            cookie_enc=encrypt_value(cookie_str),
            status="active",
        )
        db.add(record)

    db.commit()
    return {"status": "ok", "user_id": xianyu_uid}


@router.post("/cookie/manual")
def manual_cookie(
    payload: schemas.CookieSync,
    request: Request,
    db: Session = Depends(get_db),
):
    """手动粘贴 cookie"""
    plugin_id = payload.plugin_id
    cookie_str = payload.cookies
    if not plugin_id or not cookie_str:
        raise HTTPException(status_code=400, detail="plugin_id 和 cookies 必填")

    cookies = parse_cookies(cookie_str)
    xianyu_uid = get_user_id(cookies)

    # 手动粘贴场景：owner_id 取自当前登录用户
    owner_id = getattr(request.state, "user_id", None)

    # 🆕 验证插件归属
    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
    if plugin and plugin.user_id != owner_id and getattr(request.state, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="无权操作此插件的 Cookie")

    record = db.query(models.CookieStore).filter(
        models.CookieStore.plugin_id == plugin_id
    ).first()

    if record:
        record.cookie_enc = encrypt_value(cookie_str)
        record.user_id = xianyu_uid
        record.status = "active"
        if owner_id:
            record.owner_id = owner_id
    else:
        record = models.CookieStore(
            plugin_id=plugin_id,
            owner_id=owner_id,
            user_id=xianyu_uid,
            cookie_enc=encrypt_value(cookie_str),
            status="active",
        )
        db.add(record)

    db.commit()
    return {"status": "ok", "user_id": xianyu_uid}


@router.get("/cookies")
def list_cookies(
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """列出 cookie 状态（admin 看全部，普通用户看自己的）"""
    is_admin = request.state.role == 'admin'
    query = db.query(models.CookieStore)
    if not is_admin:
        query = query.filter(models.CookieStore.owner_id == current_uid)
    records = query.all()
    return [{
        "id": r.id,
        "plugin_id": r.plugin_id,
        "user_id": r.user_id,
        "status": r.status,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "has_token": bool(r.ws_token),
    } for r in records]


@router.delete("/cookie/{plugin_id}")
def delete_cookie(
    plugin_id: str,
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """删除当前用户的账号 cookie"""
    record = (
        db.query(models.CookieStore)
        .filter(
            models.CookieStore.plugin_id == plugin_id,
            models.CookieStore.owner_id == current_uid,
        )
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Cookie not found")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}


# ==========================================
# 对话管理
# ==========================================

def _user_conversations_query(db: Session, user_id: str):
    """
    构造仅包含当前用户拥有的对话的 base query。
    通过 Conversation.plugin_id JOIN CookieStore.plugin_id
    并过滤 CookieStore.owner_id == user_id。
    """
    return (
        db.query(models.Conversation)
        .join(
            models.CookieStore,
            models.Conversation.plugin_id == models.CookieStore.plugin_id,
        )
        .filter(models.CookieStore.owner_id == user_id)
    )


@router.get("/conversations")
def list_conversations(
    stage: Optional[str] = None,
    result: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    request: Request = None,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """对话列表 (分页+筛选) — admin 看所有，普通用户看自己的"""
    is_admin = request.state.role == 'admin'

    if is_admin:
        query = db.query(models.Conversation)
    else:
        query = _user_conversations_query(db, current_uid)

    if stage:
        query = query.filter(models.Conversation.stage == stage)
    if result:
        query = query.filter(models.Conversation.result == result)

    total = query.count()
    items = (
        query.order_by(models.Conversation.updated_at.desc())
        .offset((page - 1) * size)
        .limit(min(size, 100))
        .all()
    )

    # 🆕 admin 加载 username（通过 plugin_id 关联 owner）
    user_map = {}
    plugin_owner_map = {}
    if is_admin:
        users = db.query(models.User.id, models.User.username).all()
        user_map = {u.id: u.username for u in users}
        plugins = db.query(models.Plugin.id, models.Plugin.user_id).all()
        plugin_owner_map = {p.id: p.user_id for p in plugins}

    return {
        "total": total,
        "items": [{
            "id": c.id,
            "plugin_id": c.plugin_id,
            "seller_id": c.seller_id,
            "seller_name": c.seller_name,
            "item_id": c.item_id,
            "item_title": c.item_title,
            "item_price": c.item_price,
            "ai_decision": c.ai_decision,
            "max_price": c.max_price,
            "stage": c.stage,
            "result": c.result,
            "final_price": c.final_price,
            "username": user_map.get(plugin_owner_map.get(c.plugin_id, ""), "-") if is_admin else None,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        } for c in items]
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """对话详情 + 消息历史 — admin 看任意，普通用户看自己的"""
    is_admin = request.state.role == 'admin'
    if is_admin:
        conv = db.query(models.Conversation).filter(
            models.Conversation.id == conversation_id
        ).first()
    else:
        conv = (
            _user_conversations_query(db, current_uid)
            .filter(models.Conversation.id == conversation_id)
            .first()
        )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.conversation_id == conversation_id)
        .order_by(models.ChatMessage.created_at.asc())
        .all()
    )

    return {
        "conversation": {
            "id": conv.id,
            "plugin_id": conv.plugin_id,
            "seller_id": conv.seller_id,
            "seller_name": conv.seller_name,
            "item_id": conv.item_id,
            "item_title": conv.item_title,
            "item_price": conv.item_price,
            "ai_decision": conv.ai_decision,
            "max_price": conv.max_price,
            "floor_price": conv.floor_price,
            "stage": conv.stage,
            "result": conv.result,
            "cid": conv.cid,
        },
        "messages": [{
            "id": m.id,
            "sender": m.sender,
            "content": m.content,
            "msg_type": m.msg_type,
            "stage": m.stage,
            "created_at": m.created_at.isoformat(),
        } for m in messages]
    }


@router.post("/conversations/{conversation_id}/send")
def send_manual_message(
    conversation_id: str,
    payload: schemas.ManualMessage,
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """手动发送消息 — admin 可操作任意，普通用户操作自己的"""
    is_admin = request.state.role == 'admin'
    if is_admin:
        conv = db.query(models.Conversation).filter(
            models.Conversation.id == conversation_id
        ).first()
    else:
        conv = (
            _user_conversations_query(db, current_uid)
            .filter(models.Conversation.id == conversation_id)
            .first()
        )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not payload.content:
        raise HTTPException(status_code=400, detail="content 不能为空")

    # 存储消息
    msg = models.ChatMessage(
        conversation_id=conversation_id,
        sender="manual",
        content=payload.content,
        msg_type="text",
        stage=conv.stage
    )
    db.add(msg)

    # 切换为手动模式
    conv.stage = "manual"
    db.commit()

    # 通过 connection_pool 发送到闲鱼 WS
    try:
        from .connection import connection_pool
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                connection_pool.send_text(conv.plugin_id, conv.cid or "", conv.seller_id, payload.content)
            )
        except RuntimeError:
            logger.warning("[chat] 无运行中的事件循环，WS 发送跳过")
    except Exception as e:
        logger.warning(f"[chat] 手动消息 WS 发送失败 (已存储): {e}")

    return {"status": "sent", "msg_id": msg.id}


@router.post("/conversations/{conversation_id}/takeover")
def toggle_takeover(
    conversation_id: str,
    payload: schemas.TakeoverToggle,
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """AI 接管 / 手动接管切换 — admin 可操作任意"""
    is_admin = request.state.role == 'admin'
    if is_admin:
        conv = db.query(models.Conversation).filter(
            models.Conversation.id == conversation_id
        ).first()
    else:
        conv = (
            _user_conversations_query(db, current_uid)
            .filter(models.Conversation.id == conversation_id)
            .first()
        )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if payload.mode == "ai":
        conv.stage = "opening"  # 恢复 AI 控制
    else:
        conv.stage = "manual"

    db.commit()
    return {"status": "ok", "stage": conv.stage}


@router.get("/stats")
def chat_stats(
    request: Request,
    current_uid: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """私聊统计 — 仅当前用户"""
    base = _user_conversations_query(db, current_uid)

    total = base.count()
    deals = base.filter(models.Conversation.result == "deal").count()
    active = base.filter(
        models.Conversation.stage.notin_(["done", "manual"])
    ).count()

    return {
        "total_conversations": total,
        "active": active,
        "deals": deals,
        "failed": base.filter(models.Conversation.result == "failed").count(),
    }


# ==========================================
# 管理端 WebSocket — 实时聊天推送 (按 user_id 隔离)
# ==========================================

@router.websocket("/ws/admin")
async def admin_chat_ws(websocket: WebSocket):
    """
    管理前端连接此端点接收实时聊天消息
    推送类型: new_message, conversation_update, sys_status

    隔离方式: JWT token 验证 + user_id 从 token 提取
    例如: ws://host/api/chat/ws/admin?token=xxx
    """
    # 🆕 JWT 验证
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="缺少 token 参数")
        return
    try:
        payload = decode_token(token)
        user_id = payload["sub"]
    except Exception:
        await websocket.close(code=4003, reason="token 无效或已过期")
        return

    await websocket.accept()
    _admin_ws_map[websocket] = user_id
    logger.info(
        f"[chat-ws] 管理端接入 user={user_id}，当前连接数: {len(_admin_ws_map)}"
    )
    try:
        while True:
            # 保持连接，接收前端心跳
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        _admin_ws_map.pop(websocket, None)
        logger.info(
            f"[chat-ws] 管理端断开 user={user_id}，当前连接数: {len(_admin_ws_map)}"
        )
    except Exception:
        _admin_ws_map.pop(websocket, None)
