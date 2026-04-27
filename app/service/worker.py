
import sys
import os

# worker 埋得深，需要往上跳三层
current_dir = os.path.dirname(os.path.abspath(__file__)) # app/service
app_dir = os.path.dirname(current_dir) # app
root_dir = os.path.dirname(app_dir) # xianyu_backend
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# 加载 .env 文件
from dotenv import load_dotenv
load_dotenv(os.path.join(root_dir, ".env"))


import json
import asyncio
from dataclasses import dataclass
from typing import Optional
from jinja2 import Template
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from openai import OpenAI
import traceback
import smtplib

# 🌟 内部模块引入
from app import models, database

from app.redis_config import redis_client
from app.crypto import decrypt_value

models.Base.metadata.create_all(bind=database.engine)

# ==========================================
# 🛡️ 0. 定义纯净的数据传输对象 (DTO)
# ==========================================
@dataclass
class EmailConfigDTO:
    sender: str
    receiver: str
    auth_code: str
    service: str
    port: int
    html_template: Optional[str] = None

@dataclass
class ModelConfigDTO:
    api_key: str
    base_url: str
    model_name: str
    prompt_template: Optional[str] = None

# ==========================================
# 🌟 1. 全局默认配置 (实例化为 DTO)
# ==========================================
# B1 修复：所有凭证从环境变量读取，不再硬编码
DEFAULT_EMAIL_CONFIG = EmailConfigDTO(
    sender=os.getenv("DEFAULT_EMAIL_SENDER", ""),
    receiver=os.getenv("DEFAULT_EMAIL_RECEIVER", ""),
    auth_code=os.getenv("DEFAULT_EMAIL_AUTH_CODE", ""),
    service=os.getenv("DEFAULT_EMAIL_SERVICE", "smtp.qq.com"),
    port=int(os.getenv("DEFAULT_EMAIL_PORT", "465")),
    html_template=None
)

DEFAULT_MODEL_CONFIG = ModelConfigDTO(
    api_key=os.getenv("DEFAULT_MODEL_API_KEY", ""),
    base_url=os.getenv("DEFAULT_MODEL_BASE_URL", "https://api.deepseek.com"),
    model_name=os.getenv("DEFAULT_MODEL_NAME", "deepseek-chat"),
    prompt_template=None
)

# ==========================================
# 🧠 2. 系统默认 AI 提示词 (iPhone 专属)
# ==========================================
DEFAULT_SYSTEM_PROMPT = """
# Role
你是一个专精于 iPhone 二手市场的资深采购专家，拥有极强的逻辑推理能力和严格的价格敏感度。

# Workflow
在评估商品时，你必须严格遵循以下思维路径：
1. **合规性自检**：检查是否为国行、无锁、无ID、无拆无修、原装屏。任一不符直接输出“跳过”。
2. **容量与型号匹配**：识别型号。若为标准外的高容量版本（如 14P 1TB），在 512G 基准上 +260-360 元作为新基准。
3. **档位判定**：根据电池和成色，严格匹配对应的“价格标准”。
4. **价格决策**：对比标价与标准。低于”速秒价”且无异常则判定”速秒”；低于”可入价”判定”可入”。
5. **异常低价拦截**：若标价明显低于市场合理范围（低于你给出的”最高心理价位”的85%），必须判定”跳过”，原因标注”价格异常偏低，疑似问题机/翻新机/炸弹机”。这是硬性规则，不可忽略。

# Rules & Standards
## 1. 硬件红线（有一即否）
- 仅限：国行、无锁、无ID、无拆无修、原装屏幕、无进水、无扩容。
- 必须支持：验货宝/官方验机。
- 拒绝：先款、面交、海外版、卡贴机。

## 2. 价格基准表
| 型号 | 配置 | 99新/电池98%+ | 98新/电池95%+ | 95新/电池92%+ |
| :--- | :--- | :--- | :--- | :--- |
| iPhone 13 Pro | 1TB | ≤3200速秒/3400入 | ≤3000速秒/3200入 | ≤2800速秒/3000入 |
| iPhone 14 Pro | 512GB | ≤3800速秒/4000入 | ≤3600速秒/3800入 | ≤3400速秒/3600入 |
| iPhone 15 Pro | 256GB | ≤4800速秒/5000入 | ≤4600速秒/4800入 | ≤4400速秒/4600入 |

## 3. 溢价与减价逻辑
- **高容量溢价**：若型号相同但容量更高，基准价在低一档基础上每级 +260-360元。
- **颜色调整**：冷门色最高心理价下调 150 元。
- **缺失处理**：若未提及电池，按“95新档位”评估，决策设为“需人工复核”，原因注“未明示电池”。

# Output Format (Strict JSON)
不要输出任何 Markdown 格式或解释文字。
{
    "决策": "速秒" | "可入" | "跳过" | "需人工复核",
    "提取电池健康度": "数字" | "未知",
    "提取成色": "描述" | "未知",
    "最高心理价位": 数字,
    "原因": "简短说明"
}
"""

# ==========================================
# 📧 3. 系统默认精美 HTML 邮件模板 (Jinja2)
# ==========================================
DEFAULT_HTML_TEMPLATE = """
<html>
<head>
    <style>
        body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
        .header { text-align: center; border-bottom: 2px solid #ff5000; padding-bottom: 10px; margin-bottom: 20px; }
        .item { border: 1px solid #eee; padding: 15px; margin-bottom: 15px; border-radius: 6px; transition: 0.3s; }
        .item:hover { border-color: #ffda44; }
        .title { font-size: 16px; font-weight: bold; color: #333; }
        .price { color: #ff5000; font-size: 20px; font-weight: bold; margin: 8px 0; }
        .details { font-size: 13px; color: #555; line-height: 1.6; }
        .ai-tag { display: inline-block; padding: 2px 10px; font-size: 13px; border-radius: 4px; margin: 2px 0 12px 0; border: 1px solid transparent; }
        .tag-fast { background-color: #fff1f0; color: #cf1322; border-color: #ffa39e; }
        .tag-ok { background-color: #f6ffed; color: #389e0d; border-color: #b7eb8f; }
        .tag-skip { background-color: #f5f5f5; color: #595959; border-color: #d9d9d9; }
        .tag-review { display: inline-block; background-color: #fff3e0; color: #ff9800; }
        .badge { display: inline-block; padding: 2px 8px; background-color: #fff3e0; color: #ff9800; border-radius: 12px; font-size: 12px; margin-right: 5px; margin-top: 5px; }
        .link-btn { display: block; text-align: center; margin-top: 15px; padding: 10px; background-color: #ffda44; color: #333; text-decoration: none; border-radius: 4px; font-weight: bold; }
        .strategy { margin-top: 25px; padding: 15px; background-color: #f9f9f9; border-left: 4px solid #bbb; font-size: 12px; color: #777; line-height: 1.8; }
        .btn-pc { display: block; background-color: #ffda44; border: 1px solid #ddd;}
        .btn-mobile {display: none; }
        @media only screen and (max-width: 600px) {
            .btn-pc { display: none !important; }
            .btn-mobile { display: block !important; background-color: #ffda44;}
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="color: #ff5000; margin: 0;">🎯 雷达预警触发</h2>
            <p style="color: #666; font-size: 14px; margin-top: 5px;">为您捕获到 <b>{{ items|length }}</b> 个符合严格过滤条件的新商品</p>
        </div>
        
        {% for item in items %}
        <div class="item">
            <div class="title">{{ item.title }}</div>
            <div class="price">￥{{ item.price }}</div>
            <div class="ai-tag {{ item.ai_tag_class }}"><b>AI建议：{{ item.decision }}</b></div>
            <div class="ai-box">
                <div style="font-size: 13px; color: #444; line-height: 1.5;">
                    <b>💰 最高心理价：</b>￥{{ item.max_price }}<br>
                    <b>🔋 电池健康度：</b>{{ item.health }}<br>
                    <b>✨ 提取成色：</b>{{ item.condition }}<br>
                    <b>🧠 分析依据：</b>{{ item.reason }}<br>
                </div>
            </div>

            <div class="details">
                <b>⌚ 发布时间：</b>{{ item.publishTime }}<br>
                <b>📍 发货地区：</b>{{ item.location }}<br>
                <b>👤 卖家昵称：</b>{{ item.nickname }}<br>
                <b>🪪 卖家身份：</b>{{ item.type }}<br>
            </div>
            <div>{{ item.badges | safe }}</div>

            <a href="{{ item.url }}" class="link-btn btn-pc" target="_blank">💻 前往 {{ ref.platform_cn }} 网页端秒杀</a>
            <a href="{{ item.h5_url }}" class="link-btn btn-mobile" style="background-color: #ffe680; margin-top: 10px;">唤醒{{ref.platform_cn}}APP秒杀</a>
        </div>
        {% endfor %}

        <div class="strategy">
            <b style="color: #333;">🔍 触发此通知的策略追踪：</b><br>
            <b>平台：</b> {{ ref.platform_cn }}<br>
            <b>搜索词：</b> {{ ref.keyword }}<br>
            <b>命中关键词：</b> {{ ref.keywords_str }}<br>
            <b>生效的特征过滤：</b> {{ ref.strategyFeatures }}<br>
        </div>
    </div>
</body>
</html>
"""

# ==========================================
# 🤖 4. AI 评估核心逻辑
# ==========================================
def evaluate_with_ai(item_data, model_config: ModelConfigDTO):
    prompt_template = model_config.prompt_template if model_config.prompt_template else DEFAULT_SYSTEM_PROMPT
    client = OpenAI(api_key=model_config.api_key, base_url=model_config.base_url)
    
    item_info = item_data.get('item', {})
    seller_info = item_data.get('seller', {})
    features_info = item_data.get('features', {})

    title = item_info.get('title', '未知标题')
    price = item_info.get('price', 0)
    specs = item_info.get('specs', '无说明')
    
    credit_rate = seller_info.get('creditRate', '未知信用')
    good_rate = seller_info.get('goodRate', '未知评价率')
    is_verified_text = "支持验货宝" if features_info.get('isVerified') else "不支持验货宝"

    prompt = (
        f"商品标题：{title}\n"
        f"卖家信用标签：{credit_rate} | {good_rate}\n"
        f"外观与配置详情：{specs} | {is_verified_text}\n"
        f"当前卖家标价：{price}"
    )

    try:
        response = client.chat.completions.create(
            model=model_config.model_name,
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}, 
            temperature=0.1 
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"❌ AI 评估失败 ({model_config.model_name}): {e}")
        return {"决策": "需人工复核", "原因": f"评估异常: {str(e)}"}

# ==========================================
# 🛡️ 剥离出来的同步发信引擎
# ==========================================
def perform_sync_email_send(email_config, msg_string, item_count):
    if item_count <= 0:
        print("⚠️ 本批次没有符合条件的商品，邮件发送已取消。")
        return
    try:
        with smtplib.SMTP_SSL(email_config.service, email_config.port, timeout=15) as server:
            server.login(email_config.sender, email_config.auth_code)
            server.sendmail(
                email_config.sender, 
                email_config.receiver or email_config.sender, 
                msg_string
            )
        print(f"✅ 邮件发送成功 (共 {item_count} 个商品)")
    except Exception as e:
        print(f"❌ 邮件发送失败，网络或授权码错误: {e}")

# ==========================================
# 📧 5. 邮件渲染与发送核心逻辑
# ==========================================
async def send_batch_email_alert(email_config: EmailConfigDTO, new_items):
    if not new_items: return
    print(f"📊 本批次共有 {len(new_items)} 条新商品待发送邮件通知...")

    try:
        def get_decision_weight(item):
            decision = item.get('AI评估', {}).get('决策', '')
            if "速秒" in decision: return 0
            if "可入" in decision: return 1
            if "需人工复核" in decision: return 2
            return 3
            
        new_items.sort(key=get_decision_weight)
        keyword = new_items[0].get('keyword', '闲鱼商品')
        subject = f"🔥【二手商品搜索雷达】发现 {len(new_items)} 个新极品 [{keyword}]"

        processed_items = []
        keywords = []
        
        for item in new_items:
            item_data = item.get('item', {})
            seller_data = item.get('seller', {})
            features_data = item.get('features', {})
            ai_data = item.get('AI评估', {})
            platform = item.get('platform', 'goofish') 

            raw_id = str(item_data.get('id', ''))
            for prefix in ['goofish_', 'zhuanzhuan_', 'paipai_']:
                raw_id = raw_id.replace(prefix, '')

            pc_url = item_data.get('url', '')
            h5_url = ''

            if platform == 'goofish':
                app_scheme = f"intent://item?id={raw_id}#Intent;scheme=fleamarket;package=com.taobao.idlefish;end"
                h5_url = f'https://market.m.taobao.com/app/idleFish-F2e/widle-taobao-rax/page-detail?id={raw_id}'
            elif platform == 'zhuanzhuan':
                app_scheme = f"zhuanzhuan://jump/core/infoDetail/jump?infoId={raw_id}"
                h5_url = f"https://m.zhuanzhuan.com/detail/{raw_id}"
            elif platform == 'paipai':
                app_scheme = f"openapp.jdmobile://virtual?params={{\"category\":\"jump\",\"des\":\"productDetail\",\"skuId\":\"{raw_id}\"}}"
                h5_url = f"https://paipai.jd.com/auction-detail/{raw_id}"

            badges = ""
            if features_data.get('isVerified'): badges += '<span class="badge">验货宝</span>'
            if features_data.get('isFreeShipping'): badges += '<span class="badge">包邮</span>'
            if "个人" in seller_data.get('type', ''): badges += '<span class="badge">个人闲置</span>'
            if features_data.get('isSevenDaysReturn'): badges += '<span class="badge">7天包退</span>'
            for tag in features_data.get('tags', []): badges += f'<span class="badge">{tag}</span>'

            decision = ai_data.get('决策', '未知判定')
            ai_tag_class = "tag-skip"
            if "速秒" in decision: ai_tag_class = "tag-fast"
            elif "可入" in decision: ai_tag_class = "tag-ok"
            elif "需人工复核" in decision: ai_tag_class = "tag-review"

            if ai_tag_class == "tag-skip":
                continue

            strategyHit = item.get('strategyHit', '')
            if strategyHit and strategyHit not in keywords:
                keywords.append(strategyHit)

            processed_items.append({
                "title": item_data.get('title', '未获取到标题'),
                "price": item_data.get('price', '0'),
                "url": item_data.get('url', ''),
                "h5_url": h5_url,
                "platform_cn": item.get('platformCN', '未知平台'),
                "raw_id": str(item_data.get('id', '')).replace('goofish_', ''),
                "specs": item_data.get('specs', '无说明'),
                "publishTime": item_data.get('publishTime', '未知'),
                "location": item_data.get('location', '未知'),
                "nickname": seller_data.get('nickname', '未知卖家'),
                "creditRate": seller_data.get('creditRate', '未知信用'),
                "type": seller_data.get('type', '未知身份'),
                "badges": badges,
                "decision": decision,
                "ai_tag_class": ai_tag_class,
                "max_price": ai_data.get('最高心理价位', '未知'),
                "health": ai_data.get('提取电池健康度', '未知'),
                "condition": ai_data.get('提取成色', '未知'),
                "reason": ai_data.get('原因', '暂无分析')
            })

        
        if len(processed_items) == 0:
            print("⚠️ 经过 AI 严格过滤，本批次没有极品留存，邮件发送已自动拦截。")
            return
        
        ref = new_items[0]
        ref_data = {
            "platform": ref.get('platform', '未知'),
            "platform_cn": ref.get('platformCN', '未知平台'),   
            "keyword": ref.get('keyword', '未知'),
            "keywords_str": ','.join(keywords) or '无条件',
            "strategyFeatures": ref.get('strategyFeatures', '无要求')
        }

        print("🔍 正在渲染 Jinja2 HTML 模板...")
        template_str = email_config.html_template if email_config.html_template else DEFAULT_HTML_TEMPLATE
        template = Template(template_str)
        html_content = template.render(items=processed_items, ref=ref_data)

        print("✉️ 正在组装邮件协议包...")
        msg = MIMEMultipart("alternative")
        msg['Subject'] = subject
        msg['From'] = email_config.sender
        msg['To'] = email_config.receiver or email_config.sender
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        print(f"🚀 准备发往服务器: {email_config.service}:{email_config.port} (发件人: {email_config.sender}, 收件人：{email_config.receiver})")
        
        
        
        await asyncio.to_thread(
            perform_sync_email_send,
            email_config,
            msg.as_string(),
            len(processed_items)
        )

    except Exception as e:
        print(f"🔥 发信环节发生致命崩溃！")
        traceback.print_exc()

# ==========================================
# 🔄 6. 异步队列消费者 (微批处理分流)
# ==========================================
async def consume_plugin_queue(plugin_id: str):
    queue_name = f"product_queue:{plugin_id}" if plugin_id else "product_queue:default"
    print(f"👀 消费者已就绪，正在静默监听: {queue_name} ...")
    
    try:
        while True:
            first_item_raw = redis_client.lpop(queue_name)
            
            if not first_item_raw: 
                await asyncio.sleep(2)
                continue
            
            print(f"\n📦 [{queue_name}] 收到新数据，开始处理！")
            
            batch_raw = [first_item_raw]
            while True:
                more = redis_client.lpop(queue_name)
                if more: batch_raw.append(more)
                else: break

            evaluated_batch = []
            
            target_model = DEFAULT_MODEL_CONFIG
            target_email = DEFAULT_EMAIL_CONFIG
            
            db = database.SessionLocal()
            try:
                plugin = None  # 🆕 初始化，防止 NameError
                if plugin_id:
                    plugin = db.query(models.Plugin).filter(models.Plugin.id == plugin_id).first()
                    
                    if not plugin or plugin.status != "active":
                        print(f"⚠️ 节点 [{plugin_id}] 未激活或未绑定配置，将降级使用全局默认发信通道！")
                    else:
                        if plugin.model: 
                            target_model = ModelConfigDTO(
                                api_key=decrypt_value(plugin.model.api_key),
                                base_url=plugin.model.base_url,
                                model_name=plugin.model.model_name,
                                prompt_template=plugin.model.prompt_template
                            )
                        if plugin.email: 
                            target_email = EmailConfigDTO(
                                sender=plugin.email.sender,
                                receiver=plugin.email.receiver,
                                auth_code=decrypt_value(plugin.email.auth_code),
                                service=plugin.email.service,
                                port=plugin.email.port,
                                html_template=plugin.email.html_template
                            )

                for raw in batch_raw:
                    try:
                        payload = json.loads(raw)
                        item = payload.get("item", {}) if isinstance(payload, dict) and "item" in payload else payload
                        item_id = item.get("item", {}).get("id") or item.get("商品ID")
                        if not item_id: continue
                        
                        title = item.get('item', {}).get('title', '未知')
                        print(f"🤖 正在评估: {title[:30]}...")
                        
                        ai_result = evaluate_with_ai(item, target_model)
                        item['AI评估'] = ai_result
                        
                        product = db.query(models.Product).filter(
                            models.Product.item_id == item_id
                        )
                        # 🆕 按归属用户过滤，防止更新别人的商品
                        if plugin and plugin.user_id:
                            product = product.filter(models.Product.user_id == plugin.user_id)
                        product = product.first()
                        if product:
                            product.ai_evaluation = json.dumps(ai_result, ensure_ascii=False)
                            product.status = "approved" if "跳过" not in ai_result.get("决策", "") else "rejected"
                            db.commit()
                        
                        if "跳过" not in ai_result.get("决策", ""):
                            evaluated_batch.append(item)
                        else:
                            print(f"⏭️ [AI拦截] 商品不符合要求，已直接抛弃，不进入邮件推送池。")
                    
                    except Exception as e:
                        print(f"⚠️ 处理单条队列数据异常: {e}")

            finally:
                db.close()
            
            if evaluated_batch:
                await send_batch_email_alert(target_email, evaluated_batch)

                # 🆕 Phase 2/3: AI 评估通过的商品，触发私聊对话
                for item in evaluated_batch:
                    try:
                        ai_eval = item.get("AI评估", {})
                        decision = ai_eval.get("决策", "")
                        if decision in ("速秒", "可入", "需人工复核"):
                            item_data = item.get("item", {})
                            seller_data = item.get("seller", {})
                            item_id_raw = str(item_data.get("id", ""))
                            # 去掉平台前缀
                            for prefix in ("goofish_", "zhuanzhuan_"):
                                item_id_raw = item_id_raw.replace(prefix, "")

                            # 查找关联的 product 记录
                            product_uuid = ""
                            try:
                                with database.SessionLocal() as pdb:
                                    prod_query = pdb.query(models.Product).filter(
                                        models.Product.item_id == item_id_raw
                                    )
                                    # 🆕 按归属用户过滤
                                    if plugin and plugin.user_id:
                                        prod_query = prod_query.filter(models.Product.user_id == plugin.user_id)
                                    prod = prod_query.first()
                                    if prod:
                                        product_uuid = prod.id
                            except Exception:
                                pass

                            # 处理心理价位: "需人工复核"时可能无价位，用标价的 80% 作为参考
                            max_price = float(ai_eval.get("最高心理价位", 0))
                            listed_price = float(item_data.get("price", 0))
                            if max_price <= 0:
                                max_price = listed_price * 0.85
                            floor_price = max_price * 0.85

                            # 硬检查：标价低于捡漏底线（floor_price），大概率问题机，直接跳过
                            if listed_price > 0 and listed_price < floor_price:
                                print(f"🚫 [价格拦截] 标价 ¥{listed_price} 低于捡漏底线 ¥{floor_price}，疑似问题机，跳过私聊")
                                continue

                            from app.goofish.chat_engine import trigger_conversation
                            await trigger_conversation(
                                product_id=product_uuid,
                                plugin_id=plugin_id,
                                seller_id=seller_data.get("id", ""),
                                item_id=item_id_raw,
                                item_title=item_data.get("title", ""),
                                item_price=listed_price,
                                ai_decision=decision,
                                max_price=round(max_price, 2),
                                floor_price=round(floor_price, 2),
                            )
                    except Exception as e:
                        print(f"⚠️ [chat] 私聊触发失败: {e}")

    except asyncio.CancelledError:
        print(f"🛑 消费者线程安全退出: {queue_name}")

# ==========================================
# 🚀 7. 主程序入口与动态服务发现 (Watchdog)
# ==========================================
running_consumers = {}

async def plugin_discovery_daemon():
    print("🕵️  队列扫描守护进程已启动，正在实时巡视 Redis 数据缓存...")
    while True:
        try:
            queue_keys = redis_client.keys("product_queue:*")
            
            for key in queue_keys:
                plugin_id = key.split("product_queue:")[1]
                if plugin_id == "default": continue 
                
                if plugin_id not in running_consumers or running_consumers[plugin_id].done():
                    print(f"✨ 发现堆积数据的队列 [{key}]，正在紧急拉起专属消费者...")
                    task = asyncio.create_task(consume_plugin_queue(plugin_id))
                    running_consumers[plugin_id] = task
                    
        except Exception as e:
            print(f"⚠️ 守护进程扫描异常: {e}")
        
        await asyncio.sleep(10)

async def main():
    print("🚀 正在启动 [二手商品搜索雷达] 后台集群引擎 (Worker)...")
    
    default_task = asyncio.create_task(consume_plugin_queue(None))
    running_consumers["default"] = default_task
    
    discovery_task = asyncio.create_task(plugin_discovery_daemon())
    
    await asyncio.gather(default_task, discovery_task)

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())