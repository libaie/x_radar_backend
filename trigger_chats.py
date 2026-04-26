"""补触发已评估通过商品的私聊对话"""
import json
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app import models, database
from app.goofish.chat_engine import trigger_conversation


def main():
    db = database.SessionLocal()
    products = db.query(models.Product).filter(models.Product.ai_evaluation.isnot(None)).all()

    # 找到 user_id -> plugin_id 的映射
    cookies = db.query(models.CookieStore).filter(models.CookieStore.status == 'active').all()
    user_plugin_map = {}
    for c in cookies:
        user_plugin_map[c.owner_id] = c.plugin_id

    to_trigger = []
    for p in products:
        try:
            eval_data = json.loads(p.ai_evaluation)
            decision = eval_data.get('决策', '')
            if decision in ('速秒', '可入', '需人工复核'):
                max_price = float(eval_data.get('最高心理价位', 0))
                listed_price = float(p.price) if p.price else 0
                if max_price <= 0:
                    max_price = listed_price * 0.85
                floor_price = max_price * 0.85

                raw = json.loads(p.raw_data) if p.raw_data else {}
                seller_id = raw.get('seller', {}).get('id', '')
                plugin_id = user_plugin_map.get(p.user_id, '')

                if not plugin_id:
                    print(f'Skip {p.id}: no plugin_id for user {p.user_id}')
                    continue
                if not seller_id:
                    print(f'Skip {p.id}: no seller_id')
                    continue

                to_trigger.append({
                    'product_id': p.id,
                    'plugin_id': plugin_id,
                    'seller_id': seller_id,
                    'item_id': p.item_id,
                    'item_title': p.title[:50],
                    'item_price': listed_price,
                    'ai_decision': decision,
                    'max_price': round(max_price, 2),
                    'floor_price': round(floor_price, 2),
                })
        except Exception as e:
            print(f'Skip {p.id}: {e}')

    db.close()
    print(f'Found {len(to_trigger)} products to trigger\n')

    async def run():
        for t in to_trigger:
            print(f'Triggering: {t["item_title"][:40]}... decision={t["ai_decision"]}')
            try:
                conv_id = await trigger_conversation(
                    product_id=t['product_id'],
                    plugin_id=t['plugin_id'],
                    seller_id=t['seller_id'],
                    item_id=t['item_id'],
                    item_title=t['item_title'],
                    item_price=t['item_price'],
                    ai_decision=t['ai_decision'],
                    max_price=t['max_price'],
                    floor_price=t['floor_price'],
                )
                print(f'  -> conversation_id={conv_id}')
            except Exception as e:
                print(f'  -> FAILED: {e}')

    asyncio.run(run())


if __name__ == '__main__':
    main()
