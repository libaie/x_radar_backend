"""
AI 对话引擎 — 闲鱼私聊砍价状态机
根据对话阶段 + 商品信息 + AI 评估结果，自动生成回复
"""
import json
import asyncio
from typing import Optional
from datetime import datetime, timezone

from loguru import logger
from openai import OpenAI

from .. import models, database
from ..crypto import decrypt_value

# ==========================================
# 对话阶段定义
# ==========================================

STAGES = {
    "opening": {
        "name": "开场",
        "instruction": "确认商品是否在售。简短问一句就好，比如'还在吗'或'能拍吗'。",
        "next": "condition",
    },
    "condition": {
        "name": "验货",
        "instruction": "询问商品关键信息。每次只问一个：电池健康度、是否有维修记录、功能是否正常、是否有磕碰。",
        "next": "price_eval",
    },
    "price_eval": {
        "name": "估价",
        "instruction": "根据已了解的信息，结合你的心理价位，决定是否砍价。如果价格合理可直接成交，否则进入砍价。",
        "next": "negotiating",
    },
    "negotiating": {
        "name": "砍价",
        "instruction": "礼貌地报出你的价格，给出理由（预算有限/市场价/成色问题）。每次加价幅度不超过100元。",
        "next": "closing",
    },
    "closing": {
        "name": "收尾",
        "instruction": "确认交易方式（闲鱼下单/包邮/验货宝），或礼貌结束对话。",
        "next": "done",
    },
    "done": {
        "name": "结束",
        "instruction": "对话已结束。",
        "next": None,
    },
    "manual": {
        "name": "手动",
        "instruction": "人工接管中，不自动生成回复。",
        "next": None,
    },
}

# 阶段推进条件
STAGE_TRANSITIONS = {
    "opening": lambda msg: True,  # 卖家回复即可进入验货
    "condition": lambda msg: len(msg) > 2,  # 卖家有实质回复
    "price_eval": lambda msg: True,
    "negotiating": lambda msg: any(kw in msg for kw in ["可以", "行", "好", "成交", "拍", "最低", "少"]),
    "closing": lambda msg: any(kw in msg for kw in ["拍了", "下单", "好的", "可以", "行"]),
}

# ==========================================
# AI 提示词模板
# ==========================================

SYSTEM_PROMPT_TEMPLATE = """# Role
你是一个闲鱼买家，正在和卖家谈一个二手商品。说话要像真人，简短自然，不要像机器人。

# 商品信息
- 标题: {item_title}
- 卖家标价: ¥{item_price}
- AI评估建议: {ai_decision} (最高心理价 ¥{max_price})
- 捡漏底线: ¥{floor_price}
- 商品链接: https://www.goofish.com/item?id={item_id}

# 当前阶段: {stage_name}
# 你的目标
以尽可能低的价格买入。如果卖家报价高于心理价 ¥{max_price}，礼貌还价。

# 对话历史
{chat_history}

# 规则
1. 每次只说1-2句话，像真人聊天
2. 不要一次问太多问题
3. 砍价要有理由（成色、市场价、预算有限）
4. 不要暴露你是AI，不要提及"评估""系统""机器人"
5. 如果价格超过心理价太多，礼貌说"再考虑下"然后结束
6. 闲鱼上的正常对话风格，可以用表情但不要太多

# 你现在应该做: {stage_instruction}

只回复纯文本消息内容，不要 JSON，不要解释。"""


def build_prompt(conversation, chat_history: list, stage: str) -> str:
    """构造 AI 提示词"""
    stage_info = STAGES.get(stage, STAGES["opening"])

    history_text = ""
    for msg in chat_history[-10:]:  # 最近 10 条
        role = "你" if msg["sender"] in ("ai", "manual") else "卖家"
        history_text += f"[{role}] {msg['content']}\n"

    return SYSTEM_PROMPT_TEMPLATE.format(
        item_title=conversation.item_title or "未知商品",
        item_price=conversation.item_price or 0,
        ai_decision=conversation.ai_decision or "未知",
        max_price=conversation.max_price or 0,
        floor_price=conversation.floor_price or 0,
        item_id=conversation.item_id or "",
        stage_name=stage_info["name"],
        stage_instruction=stage_info["instruction"],
        chat_history=history_text or "(对话刚开始)",
    )


# ==========================================
# AI 对话引擎
# ==========================================

def get_ai_client(model_config: dict = None) -> Optional[tuple]:
    """获取 AI 客户端（支持自定义模型配置）"""
    import os
    if model_config and model_config.get("api_key"):
        api_key = model_config["api_key"]
        base_url = model_config.get("base_url", "https://api.deepseek.com")
        model_name = model_config.get("model_name", "deepseek-chat")
    else:
        api_key = os.getenv("DEFAULT_MODEL_API_KEY", "")
        base_url = os.getenv("DEFAULT_MODEL_BASE_URL", "https://api.deepseek.com")
        model_name = os.getenv("DEFAULT_MODEL_NAME", "deepseek-chat")
    if not api_key:
        logger.warning("[chat_engine] 未配置 AI API Key")
        return None
    return OpenAI(api_key=api_key, base_url=base_url), model_name


def generate_ai_reply(conversation_id: str) -> Optional[str]:
    """
    核心：读取对话历史 → 构造 prompt → 调用 AI → 返回回复文本
    同步函数，由 asyncio.to_thread 调用
    """
    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter(
            models.Conversation.id == conversation_id
        ).first()
        if not conv or conv.stage in ("done", "manual"):
            return None

        # 🆕 获取插件绑定的 chat_model
        model_config = None
        if conv.plugin_id:
            plugin = db.query(models.Plugin).filter(models.Plugin.id == conv.plugin_id).first()
            if plugin and plugin.chat_model:
                model_config = {
                    "api_key": decrypt_value(plugin.chat_model.api_key),
                    "base_url": plugin.chat_model.base_url,
                    "model_name": plugin.chat_model.model_name,
                }

        # 获取对话历史
        messages = db.query(models.ChatMessage).filter(
            models.ChatMessage.conversation_id == conversation_id
        ).order_by(models.ChatMessage.created_at.asc()).all()

        history = [{"sender": m.sender, "content": m.content} for m in messages]

        # 构造 prompt
        prompt = build_prompt(conv, history, conv.stage)

        # 调用 AI（优先用插件绑定的模型）
        client_info = get_ai_client(model_config)
        if not client_info:
            return None
        client, model_name = client_info

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "请根据当前阶段和对话历史，生成你的下一条回复。"}
            ],
            temperature=0.7,
            max_tokens=200,
        )

        reply = response.choices[0].message.content.strip()
        logger.info(f"[chat_engine] AI 回复 (stage={conv.stage}): {reply[:50]}")
        return reply

    except Exception as e:
        logger.error(f"[chat_engine] AI 生成失败: {e}")
        return None
    finally:
        db.close()


def advance_stage(conversation_id: str, seller_message: str) -> str:
    """
    根据卖家消息判断是否推进对话阶段
    返回新阶段
    """
    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter(
            models.Conversation.id == conversation_id
        ).first()
        if not conv:
            return "done"

        current = conv.stage
        transition = STAGE_TRANSITIONS.get(current)

        if transition and transition(seller_message):
            next_stage = STAGES.get(current, {}).get("next")
            if next_stage and next_stage != "done":
                conv.stage = next_stage
                db.commit()
                logger.info(f"[chat_engine] 阶段推进: {current} → {next_stage}")
                return next_stage

        return current
    finally:
        db.close()


def store_message(conversation_id: str, sender: str, content: str, stage: str = None) -> models.ChatMessage:
    """存储一条消息到数据库"""
    db = database.SessionLocal()
    try:
        msg = models.ChatMessage(
            conversation_id=conversation_id,
            sender=sender,
            content=content,
            msg_type="text",
            stage=stage
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg
    finally:
        db.close()


# ==========================================
# 对话触发器 — 由 AI 评估结果触发
# ==========================================

async def trigger_conversation(product_id: str, plugin_id: str, seller_id: str,
                                item_id: str, item_title: str, item_price: float,
                                ai_decision: str, max_price: float, floor_price: float):
    """
    AI 评估通过后，创建对话并发送第一条消息
    """
    from .connection import connection_pool

    db = database.SessionLocal()
    try:
        # 检查是否已在聊
        existing = db.query(models.Conversation).filter(
            models.Conversation.seller_id == seller_id,
            models.Conversation.item_id == item_id,
            models.Conversation.plugin_id == plugin_id,  # 🆕 加插件隔离
        ).first()
        if existing:
            logger.info(f"[chat_engine] 已存在对话，跳过: seller={seller_id}, item={item_id}")
            return existing.id

        # 创建对话记录
        conv = models.Conversation(
            plugin_id=plugin_id,
            product_id=product_id,
            seller_id=seller_id,
            item_id=item_id,
            item_title=item_title,
            item_price=item_price,
            ai_decision=ai_decision,
            max_price=max_price,
            floor_price=floor_price,
            stage="opening"
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conversation_id = conv.id

    finally:
        db.close()

    logger.info(f"[chat_engine] 新对话创建: id={conversation_id}, plugin={plugin_id[:8]}, 商品={item_title[:20]}")

    # 先通过 create_chat 获取会话 ID (cid)
    cid = ""
    try:
        logger.info(f"[chat_engine] 正在调用 create_chat: plugin={plugin_id[:8]}, seller={seller_id}, item={item_id}")
        cid = await connection_pool.create_chat(plugin_id, seller_id, item_id)
        logger.info(f"[chat_engine] create_chat 返回: cid='{cid}' (bool={bool(cid)})")
        if cid:
            # 将 cid 更新到对话记录
            db2 = database.SessionLocal()
            try:
                conv2 = db2.query(models.Conversation).filter(
                    models.Conversation.id == conversation_id
                ).first()
                if conv2:
                    conv2.cid = cid
                    db2.commit()
            finally:
                db2.close()
            logger.info(f"[chat_engine] 会话 ID 已保存: cid={cid}")
        else:
            logger.warning(f"[chat_engine] ⚠️ create_chat 返回空 cid! plugin={plugin_id[:8]}, seller={seller_id}")
    except Exception as e:
        logger.error(f"[chat_engine] ❌ create_chat 异常: {type(e).__name__}: {e}")

    # 生成开场白
    logger.info(f"[chat_engine] 正在生成 AI 开场白: conversation={conversation_id}")
    reply = generate_ai_reply(conversation_id)
    logger.info(f"[chat_engine] AI 回复: {'有内容' if reply else 'None/空'} len={len(reply) if reply else 0}")

    if reply:
        try:
            logger.info(f"[chat_engine] 正在发送消息: cid='{cid}', seller={seller_id}")
            await connection_pool.send_text(plugin_id, cid, seller_id, reply)
            store_message(conversation_id, "ai", reply, "opening")
            logger.info(f"[chat_engine] ✅ 开场白发送成功: {reply[:30]}")
        except Exception as e:
            logger.error(f"[chat_engine] ❌ 开场白发送失败: {type(e).__name__}: {e}")
    else:
        logger.warning(f"[chat_engine] ⚠️ AI 回复为空，未发送消息。检查 chat_model 配置和 API Key")

    return conversation_id


# ==========================================
# 卖家消息处理器 — 由 WS 回调触发
# ==========================================

async def handle_seller_message(plugin_id: str, seller_id: str, seller_name: str,
                                 content: str, cid: str, item_id: str):
    """
    收到卖家消息后的处理流程:
    1. 匹配对话 (by seller_id + item_id)
    2. 存储消息
    3. 推送到管理前端
    4. 如果是 AI 模式 → 生成回复 → 发送
    """
    from .connection import connection_pool
    from .router import broadcast_to_admins  # 管理端 WS 推送

    db = database.SessionLocal()
    try:
        # 匹配对话（加 plugin_id 隔离，防止跨用户消息串线）
        conv = db.query(models.Conversation).filter(
            models.Conversation.seller_id == seller_id,
            models.Conversation.item_id == item_id,
            models.Conversation.plugin_id == plugin_id,
        ).first()

        if not conv:
            # 没有对应对话，可能是手动聊天或新对话
            logger.info(f"[chat_engine] 收到未匹配消息: seller={seller_id}, content={content[:30]}")
            return

        # 更新 cid (如果之前没有)
        if cid and not conv.cid:
            conv.cid = cid
            db.commit()

        # 优先使用数据库中已存的 cid（来自 create_chat）
        effective_cid = conv.cid or cid

        # 存储卖家消息
        msg = models.ChatMessage(
            conversation_id=conv.id,
            sender="seller",
            content=content,
            msg_type="text",
            stage=conv.stage
        )
        db.add(msg)
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        msg_id = msg.id
        conv_id = conv.id
        conv_stage = conv.stage
        conv_plugin_id = conv.plugin_id

    finally:
        db.close()

    # 推送到管理前端
    await broadcast_to_admins({
        "type": "new_message",
        "conversation_id": conv_id,
        "message": {
            "id": msg_id,
            "sender": "seller",
            "content": content,
            "stage": conv_stage,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    })

    # 如果是 manual 模式，不自动回复
    if conv_stage == "manual":
        return

    # 如果是 done，不回复
    if conv_stage == "done":
        return

    # 推进阶段
    new_stage = advance_stage(conv_id, content)

    # 随机延迟 (3-8秒，模拟真人)
    import random
    delay = random.uniform(3, 8)
    await asyncio.sleep(delay)

    # 生成 AI 回复
    reply = await asyncio.to_thread(generate_ai_reply, conv_id)
    if reply:
        try:
            await connection_pool.send_text(conv_plugin_id, effective_cid, seller_id, reply)
            stored = store_message(conv_id, "ai", reply, new_stage)

            # 推送 AI 回复到管理前端
            await broadcast_to_admins({
                "type": "new_message",
                "conversation_id": conv_id,
                "message": {
                    "id": stored.id,
                    "sender": "ai",
                    "content": reply,
                    "stage": new_stage,
                    "created_at": stored.created_at.isoformat(),
                }
            })
        except Exception as e:
            logger.error(f"[chat_engine] AI 回复发送失败: {e}")
