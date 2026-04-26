from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid
from .database import Base

def get_utc_now():
    return datetime.now(timezone.utc)

# ==========================================
# 🆕 多租户 — 用户表
# ==========================================
class User(Base):
    __tablename__ = "users"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(255), default="user")          # admin / user
    status = Column(String(255), default="active")      # active / disabled
    max_plugins = Column(Integer, default=3)             # 🆕 绑定节点上限
    created_at = Column(DateTime, default=get_utc_now)
    expires_at = Column(DateTime, nullable=True)   # NULL = 永不过期

    plugins = relationship("Plugin", back_populates="owner")
    products = relationship("Product", back_populates="owner")
    tasks = relationship("Task", back_populates="owner")
    emails = relationship("Email", back_populates="owner")

class Plugin(Base):
    __tablename__ = "plugins"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=False)  # 🆕 归属用户
    name = Column(String(255), nullable=False)
    status = Column(String(255), default="inactive")
    registered_at = Column(DateTime, default=get_utc_now)
    last_heartbeat = Column(DateTime, nullable=True)
    alert_email_id = Column(String(255), ForeignKey("emails.id"), nullable=True)
    model_id = Column(String(255), ForeignKey("models.id"), nullable=True)
    chat_model_id = Column(String(255), ForeignKey("models.id"), nullable=True)
    email_id = Column(String(255), ForeignKey("emails.id"), nullable=True)
    owner = relationship("User", back_populates="plugins")
    email = relationship("Email", foreign_keys="[Plugin.email_id]")
    alert_email = relationship("Email", foreign_keys="[Plugin.alert_email_id]")
    model = relationship("Model", foreign_keys="[Plugin.model_id]", back_populates="plugins")
    chat_model = relationship("Model", foreign_keys="[Plugin.chat_model_id]")
    logs = relationship("PluginLog", back_populates="plugin")

class Model(Base):
    __tablename__ = "models"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=True)  # 🆕 归属用户
    name = Column(String(255), nullable=False)
    model_name = Column(String(255), nullable=False, default="deepseek-chat")
    base_url = Column(String(255), nullable=False, default="https://api.deepseek.com")
    api_key = Column(String(255), nullable=False)
    prompt_template = Column(Text, nullable=True)
    plugins = relationship("Plugin", foreign_keys="[Plugin.model_id]", back_populates="model")

class Email(Base):
    __tablename__ = "emails"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=False)  # 🆕 归属用户
    name = Column(String(255), default="未命名通道")
    sender = Column(String(255), nullable=False)
    receiver = Column(String(255), nullable=True)
    auth_code = Column(String(255), nullable=False)
    service = Column(String(255), nullable=False)
    port = Column(Integer, default=465)
    html_template = Column(Text, nullable=True)
    owner = relationship("User", back_populates="emails")

class PluginLog(Base):
    __tablename__ = "plugin_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    plugin_id = Column(String(255), ForeignKey("plugins.id"))
    level = Column(String(255), default="INFO")
    message = Column(Text)
    timestamp = Column(DateTime, default=get_utc_now)
    plugin = relationship("Plugin", back_populates="logs")

class Product(Base):
    __tablename__ = "products"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=False)  # 🆕 归属用户
    item_id = Column(String(255), nullable=False)
    platform = Column(String(255), default="goofish")
    platformCN = Column(String(255), default="未知平台")
    title = Column(Text)
    price = Column(String(255))
    raw_data = Column(Text)
    ai_evaluation = Column(Text, nullable=True)
    status = Column(String(255), default="pending")
    created_at = Column(DateTime, default=get_utc_now)
    owner = relationship("User", back_populates="products")
    __table_args__ = (
        UniqueConstraint('item_id', 'platform', 'user_id', name='uq_item_platform_user'),
    )

class Task(Base):
    __tablename__ = "tasks"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=False)  # 🆕 归属用户
    platformEN = Column(String(255), nullable=False)
    keywords = Column(Text, nullable=False)
    params_json = Column(Text, nullable=False)
    task_type = Column(String(255), default="sequential")
    status = Column(String(255), default="active")
    created_at = Column(DateTime, default=get_utc_now)
    owner = relationship("User", back_populates="tasks")


class TaskGroup(Base):
    """任务组 — 用户自定义节点组合，用于快速派发"""
    __tablename__ = "task_groups"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    plugin_ids = Column(Text, nullable=False)  # JSON array: ["id1", "id2"]
    created_at = Column(DateTime, default=get_utc_now)

# ==========================================
# 私聊整合模块 — 闲鱼账号/Cookie/对话/消息
# ==========================================

class CookieStore(Base):
    """闲鱼账号 Cookie 存储（每个 plugin_id 对应一个闲鱼账号）"""
    __tablename__ = "cookie_store"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    plugin_id = Column(String(255), unique=True, nullable=False)
    owner_id = Column(String(255), ForeignKey("users.id"), nullable=True)  # 🆕 归属用户
    user_id = Column(String(255), nullable=True)                   # 闲鱼用户ID (unb cookie)
    cookie_enc = Column(Text, nullable=False)
    ws_token = Column(Text, nullable=True)
    ws_token_exp = Column(DateTime, nullable=True)
    status = Column(String(255), default="active")
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

class Conversation(Base):
    """AI 与卖家的对话记录"""
    __tablename__ = "conversations"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    plugin_id = Column(String(255), nullable=False)
    product_id = Column(String(255), nullable=True)
    seller_id = Column(String(255), nullable=False)
    seller_name = Column(String(255), nullable=True)
    item_id = Column(String(255), nullable=True)
    item_title = Column(String(255), nullable=True)
    item_price = Column(Float, nullable=True)
    ai_decision = Column(String(255), nullable=True)
    max_price = Column(Float, nullable=True)
    floor_price = Column(Float, nullable=True)
    stage = Column(String(255), default="opening")
    result = Column(String(255), nullable=True)
    final_price = Column(Float, nullable=True)
    cid = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)
    __table_args__ = (
        UniqueConstraint('plugin_id', 'seller_id', 'item_id', name='uq_plugin_seller_item'),
    )

class ChatMessage(Base):
    """单条聊天消息"""
    __tablename__ = "chat_messages"
    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String(255), ForeignKey("conversations.id"), nullable=False)
    sender = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    msg_type = Column(String(255), default="text")
    stage = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=get_utc_now)
    conversation = relationship("Conversation", backref="messages")
