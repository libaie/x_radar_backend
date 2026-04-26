import os
import redis

# Redis 连接 — 支持环境变量配置（本地/远程服务器）
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', '6379')),
    db=int(os.getenv('REDIS_DB', '0')),
    password=os.getenv('REDIS_PASSWORD', None) or None,
    decode_responses=True,
    socket_connect_timeout=5,
)
