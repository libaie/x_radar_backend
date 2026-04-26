from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

# from app.database import Base

class PluginAlertPayload(BaseModel):
    type: str
    message: str

class PluginRegister(BaseModel):
    plugin_id: Optional[str] = None
    name: str
    current_model_name: Optional[str] = None
    features: List[str] = []

class MatchRequest(BaseModel):
    plugin_name: str
    item_data: dict

class PluginStatusUpdate(BaseModel):
    action: str # "start", "stop"

class ModelCreate(BaseModel):
    name: str
    model_name: str  # 🌟 新增
    base_url: str    # 🌟 新增
    api_key: str
    prompt_template: str

class EmailCreate(BaseModel):
    name: str
    sender: str
    receiver: Optional[str] = None
    auth_code: str
    service: str
    port: int = 465
    html_template: Optional[str] = None # 🌟 新增

# ==========================================
# 🌟 完美对齐前端 schema.js 的多平台数据契约
# ==========================================
class ItemInfo(BaseModel):
    id: str
    title: str
    price: float
    specs: str
    url: str
    picUrl: str
    publishTime: str
    location: str

class SellerInfo(BaseModel):
    id: str
    nickname: str
    type: str
    creditRate: str
    evalCount: str
    goodRate: str
    avatarUrl: str

class FeaturesInfo(BaseModel):
    isFreeShipping: bool
    isVerified: bool
    hasVideo: bool
    isSevenDaysReturn: bool
    tags: List[str]

class ProductRecord(BaseModel):
    platform: str            # 🌟 多平台标识 (闲鱼、淘宝、转转等)
    platformCN: str          # 🌟 平台中文名称
    taskId: str
    keyword: str
    item: ItemInfo
    seller: SellerInfo
    features: FeaturesInfo
    strategyHit: str
    strategyFeatures: str

class CollectPayload(BaseModel):
    plugin_id: Optional[str] = None
    data: List[ProductRecord]

# ==========================================
# 🌟 云端任务派发结构
# ==========================================
# B21 修复：用 Pydantic model 严格校验过滤器
class FiltersSchema(BaseModel):
    isVerified: bool = False
    isPersonal: bool = False
    isFreeShip: bool = False
    isResell: bool = False

class CloudTaskRequest(BaseModel):
    platformEN: str = "goofish"
    keywords: List[str]  # 允许前端用逗号分隔传多个词，后端负责拆分派发
    minPrice: Optional[float] = None
    maxPrice: Optional[float] = None
    maxPages: int = 1
    sortType: str = "time_desc"
    timeRange: int = 0
    # B9 修复：用 default_factory 替代可变默认值
    filters: FiltersSchema = Field(default_factory=FiltersSchema)
    mustInclude: List[str] = []
    negativeKeywords: List[str] = []
    task_type: str = "sequential"
    dispatchDelays: list = [10, 25]  # 派发延迟，单位秒,支持随机范围 [min, max]
    target_plugin_id: Optional[str] = None  # 🆕 指定节点，为空则自动分配
    task_group_id: Optional[str] = None  # 🆕 指定任务组，优先级高于 target_plugin_id

# ==========================================
# 🆕 私聊整合 Pydantic 模型
# ==========================================

class CookieSync(BaseModel):
    plugin_id: str
    cookies: str

class ConversationCreate(BaseModel):
    plugin_id: str
    product_id: Optional[str] = None
    seller_id: str
    seller_name: Optional[str] = None
    item_id: Optional[str] = None
    item_title: Optional[str] = None
    item_price: Optional[float] = None
    ai_decision: Optional[str] = None
    max_price: Optional[float] = None
    floor_price: Optional[float] = None

class ManualMessage(BaseModel):
    content: str

class TakeoverToggle(BaseModel):
    mode: str = "ai"  # ai / manual
