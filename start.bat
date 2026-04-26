@echo off
chcp 65001 >nul
title 二手商品搜索雷达 - 启动控制台

:: 🌟 核心修复：强行设置项目根目录到环境变量
:: 这会让所有的 "from app.xxx" 变成绝对有效的路径
set PYTHONPATH=%CD%

echo ==========================================
echo       🚀 二手商品搜索雷达 V4.3 启动程序
echo ==========================================

if exist "platform.venv\Scripts\activate.bat" (
    echo 🟢 正在激活虚拟环境...
    call platform.venv\Scripts\activate.bat
) else (
    echo ❌ 未找到虚拟环境，请检查路径！
    pause
    exit /b
)

:: 启动主服务
echo 📡 正在启动主服务 (app.main)...
:: 注意：这里去掉了多余的 call，直接启动
start "Radar API" cmd /k "chcp 65001 >nul && set PYTHONPATH=%CD% && call platform.venv\Scripts\activate.bat && uvicorn app.main:app --host 0.0.0.0 --port 15001 --ws websockets --reload"

timeout /t 5 /nobreak >nul

:: 启动 Worker
echo 🤖 正在启动队列消费者 (worker.py)...
start "Radar Worker" cmd /k "chcp 65001 >nul && set PYTHONPATH=%CD% && call platform.venv\Scripts\activate.bat && python -m app.service.worker"

echo.
echo ✅ 路径环境已重置，服务启动中...
pause