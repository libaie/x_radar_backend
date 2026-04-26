# 闲鱼雷达后台管理系统

基于 FastAPI + SQLite + Redis + Vue 3 的轻量级自动化采购后台。

## 📦 项目结构

```
xianyu_backend/
├── app/
│   ├── main.py           # FastAPI 服务入口
│   ├── models.py         # SQLAlchemy ORM 模型
│   ├── schemas.py        # Pydantic 数据校验
│   ├── database.py       # 数据库连接
│   └── service/
│       └── worker.py     # 独立评估Worker（规则引擎 + 邮件）
├── requirements.txt
└── xianyu_backend.db     # 运行时生成的 SQLite 数据库
```

## 🚀 快速启动

### 1. 安装依赖

```bash
cd D:\kaifa\xianyu\xianyu_backend
pip install -r requirements.txt
```

### 2. 启动 API 服务（终端1）

```bash
uvicorn app.main:app --host 0.0.0.0 --port 5000 --reload
```

访问 `http://127.0.0.1:5000/docs` 查看 Swagger 文档。

### 3. 启动评估 Worker（终端2）

```bash
python -m app.service.worker
```

### 4. 打开前端管理界面

双击打开 `D:\kaifa\xianyu\xianyu_frontend\index.html`。

## 🔌 插件对接

你的 Chrome 插件需要将采集到的商品数据 POST 到 `http://127.0.0.1:5000/api/collect`，格式示例：

```json
{
  "plugin_id": "插件ID（可选）",
  "data": [
    {
      "商品ID": "123456",
      "标题": "iPhone 15 256G",
      "价格": 4500,
      "链接": "https://www.goofish.com/item?id=123456",
      "外观与配置": "黑色|国行|在保",
      "...": "其他字段"
    }
  ]
}
```

## 📋 功能清单

- ✅ 插件注册与状态控制
- ✅ 独立插件队列（多插件并发）
- ✅ 规则引擎评估商品
- ✅ SMTP 邮件通知
- ✅ 模型、邮箱的 CRUD
- ✅ 插件配置绑定（选择模型/邮箱）
- ✅ 插件日志接收与展示

## ⚙️ 接口概览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/plugin/register` | 插件注册 |
| POST | `/api/plugin/status/{id}` | 启停插件 |
| POST | `/api/plugin/{id}/config` | 绑定模型/邮箱 |
| POST | `/api/plugin/{id}/log` | 接收插件日志 |
| GET  | `/api/plugin/{id}/logs` | 获取插件日志 |
| POST | `/api/collect` | 接收商品数据 |
| GET  | `/api/dashboard/stats` | 仪表盘统计 |
| GET  | `/api/plugins` | 插件列表（含绑定信息） |
| GET/POST/PUT/DELETE | `/api/models` | 模型管理 |
| GET/POST/PUT/DELETE | `/api/emails` | 邮箱管理 |

## 🔧 扩展大模型评估

在 `app/service/worker.py` 的 `rule_based_evaluate` 函数中，替换为真实的 LLM 调用即可。

---

祝你使用愉快！🛡️
