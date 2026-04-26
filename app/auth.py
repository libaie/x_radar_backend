import os
from datetime import datetime, timezone, timedelta
from passlib.context import CryptContext
import jwt

# JWT 配置
JWT_SECRET = os.getenv("JWT_SECRET", "xianyu-radar-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

# 密码哈希
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(raw_password: str) -> str:
    return pwd_context.hash(raw_password)

def verify_password(raw_password: str, hashed: str) -> bool:
    return pwd_context.verify(raw_password, hashed)

def create_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> dict:
    """解码 JWT，返回 {sub, role, exp}。过期或无效抛 jwt.PyJWTError。"""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
