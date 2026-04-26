# app/crypto.py - Fernet 对称加密工具
import os
from cryptography.fernet import Fernet

_ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is None:
        if not _ENCRYPTION_KEY:
            # 自动生成并警告（开发模式）
            _ENCRYPTION_KEY_LOCAL = Fernet.generate_key().decode()
            print(f"⚠️ [crypto] ENCRYPTION_KEY 未设置，已自动生成临时密钥（重启后失效！）")
            print(f"⚠️ [crypto] 请在 .env 中设置: ENCRYPTION_KEY={_ENCRYPTION_KEY_LOCAL}")
            _fernet = Fernet(_ENCRYPTION_KEY_LOCAL.encode())
        else:
            _fernet = Fernet(_ENCRYPTION_KEY.encode())
    return _fernet

def encrypt_value(plain: str) -> str:
    """加密明文字符串，返回密文（base64 编码）"""
    if not plain:
        return plain
    try:
        return _get_fernet().encrypt(plain.encode()).decode()
    except Exception as e:
        print(f"⚠️ [crypto] 加密失败: {e}")
        return plain

def decrypt_value(encrypted: str) -> str:
    """解密密文，返回明文。如果不是密文格式则原样返回（兼容旧数据）"""
    if not encrypted:
        return encrypted
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        # 解密失败说明可能是旧的明文数据，原样返回
        return encrypted
