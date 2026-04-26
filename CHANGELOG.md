# CHANGELOG - X-Radar 分布式控制中枢 (Backend)

## v1.1 (2026-04-24) — 安全加固 + 性能优化

### 🔴 安全修复
1. **硬编码凭证清除** (worker.py)
   - SMTP 授权码、DeepSeek API Key 等全部改为环境变量读取
   - 环境变量: DEFAULT_EMAIL_SENDER, DEFAULT_EMAIL_AUTH_CODE, DEFAULT_MODEL_API_KEY 等

2. **email_service.py sendmail 收件人 BUG**
   - `server.sendmail(sender, sender, ...)` → `server.sendmail(sender, receiver or sender, ...)`
   - 报警邮件之前一直发给了发件人自己

3. **API Token 中间件** (main.py)
   - 新增 `X-API-Token` 请求头认证，保护所有 `/api/*` 端点
   - 设置 `API_TOKEN` 环境变量启用，留空则跳过（开发模式）

4. **CORS 配置修复** (main.py)
   - `allow_origins=["*"]` + `allow_credentials=True` 矛盾配置 → 明确指定允许的 origin
   - 通过 `CORS_ORIGINS` 环境变量配置，逗号分隔

5. **index.html 硬编码 IP** → 动态推断 `window.location.host`

6. **ws_test.py 敏感文件** → 已加入 .gitignore

7. **Product.item_id unique 约束** (models.py)
   - 全局唯一 → 联合唯一 `(item_id, platform)`，防止跨平台 ID 碰撞

### 🟡 重要改进
8. **Pydantic 可变默认参数** (schemas.py)
   - `filters: dict = {...}` → `filters: FiltersSchema = Field(default_factory=FiltersSchema)`
   - 新增 `FiltersSchema(BaseModel)` 严格类型校验

9. **插件断线状态同步** (main.py)
   - WebSocket 断开时更新数据库 `plugin.status = "inactive"`

10. **N+1 查询优化** (main.py)
    - `list_plugins` 从 31 次查询 → 1 次 joinedload 查询

11. **collect 接口异步优化** (main.py)
    - `def` → `async def`，Redis 去重改用 Pipeline 批量执行

12. **requirements.txt 补依赖**
    - 新增 `openai>=1.0.0` 和 `jinja2>=3.0.0`

13. **Email.auth_code 加密建议** (models.py)
    - 添加 Fernet 对称加密方案注释

14. **asyncio.get_event_loop() → get_running_loop()** (ws_manager.py)
    - 3 处 deprecated 调用全部修复

### 💡 前端优化
15. **假数据图表** → 使用真实 total_products 占位，标注"开发中"
16. **WS 重连退避** → 指数退避 `min(5s * 2^n, 60s)`
17. **Visibility API** → 页面不可见时停止轮询，回到前台立即刷新
18. **.gitignore 创建** → 排除 __pycache__、.venv、.db、ws_test.py 等

### 🔗 前后端交叉
19. **Chrome 扩展 fallbackData 重传** (background.js)
    - WS 重连后自动检查本地兜底队列，有数据则批量 POST 到 `/api/collect`
    - 重传成功后清空本地队列，失败则保留

---
## v1.0 — 初始版本
- FastAPI + SQLAlchemy + SQLite + Redis
- WebSocket 双向通信（插件 + 管理端）
- 分布式调度引擎 + 看门狗
- AI 评估 + 邮件推送
- Vue 3 + Element Plus 管理大屏
