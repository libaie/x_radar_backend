#!/bin/bash
# X-Radar 部署脚本
# 用法: bash deploy.sh

set -e

PROJECT_DIR="/opt/xianyu_backend"
VENV_DIR="$PROJECT_DIR/.venv"

echo "🚀 开始部署 X-Radar..."

# 1. 创建项目目录
echo "📁 创建项目目录..."
mkdir -p $PROJECT_DIR

# 2. 复制项目文件（排除开发文件）
echo "📦 复制文件..."
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.db' \
  --exclude='.git' --exclude='*.pyc' --exclude='trigger_chats.py' \
  ./ $PROJECT_DIR/

# 3. 复制生产环境配置
cp .env.production $PROJECT_DIR/.env

# 4. 创建虚拟环境
echo "🐍 创建虚拟环境..."
python3 -m venv $VENV_DIR
source $VENV_DIR/bin/activate

# 5. 安装依赖
echo "📦 安装依赖..."
pip install --upgrade pip
pip install -r $PROJECT_DIR/requirements.txt

# 6. 创建 systemd 服务文件
echo "⚙️ 创建 systemd 服务..."
cat > /etc/systemd/system/xianyu-api.service << 'EOF'
[Unit]
Description=X-Radar API Server
After=network.target mysql.service redis.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/xianyu_backend
Environment="PYTHONPATH=/opt/xianyu_backend"
ExecStart=/opt/xianyu_backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 15001 --ws websockets
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/xianyu-worker.service << 'EOF'
[Unit]
Description=X-Radar Worker
After=network.target mysql.service redis.service xianyu-api.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/xianyu_backend
Environment="PYTHONPATH=/opt/xianyu_backend"
ExecStart=/opt/xianyu_backend/.venv/bin/python -m app.service.worker
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 7. 启动服务
echo "🚀 启动服务..."
systemctl daemon-reload
systemctl enable xianyu-api xianyu-worker
systemctl restart xianyu-api
sleep 3
systemctl restart xianyu-worker

# 8. 检查状态
echo ""
echo "===== 部署完成 ====="
systemctl status xianyu-api --no-pager -l
echo ""
systemctl status xianyu-worker --no-pager -l
echo ""
echo "访问: http://$(hostname -I | awk '{print $1}'):15001/"
