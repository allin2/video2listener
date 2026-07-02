#!/bin/bash
# video2listener 一键部署脚本
set -euo pipefail

echo "=== video2listener 部署 ==="
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Python 环境
echo "[1/5] 创建虚拟环境..."
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
echo "[2/5] 安装依赖..."
pip install --upgrade pip
pip install -e .

# 3. 初始化目录
echo "[3/5] 初始化数据目录..."
mkdir -p data db

# 4. 配置检查
if [ ! -f config.yaml ]; then
    echo "错误: config.yaml 不存在"
    exit 1
fi

echo ""
echo "=== 环境变量配置 ==="
echo "请确保已设置以下环境变量（写入 ~/.bashrc 或 /etc/environment）："
echo "  export VIDEO2LISTENER_DEEPSEEK_API_KEY=\"your-key\""
echo "  export VIDEO2LISTENER_FEISHU_APP_ID=\"your-app-id\""
echo "  export VIDEO2LISTENER_FEISHU_APP_SECRET=\"your-app-secret\""
echo ""

# 5. systemd 服务
echo "[5/5] 配置 systemd 服务..."
SERVICE_FILE="/etc/systemd/system/video2listener.service"

if [ -f deploy/video2listener.service ]; then
    sudo cp deploy/video2listener.service "$SERVICE_FILE"
    sudo sed -i "s|/path/to/video2listener|$PROJECT_DIR|g" "$SERVICE_FILE"
    sudo sed -i "s|/path/to/venv|$PROJECT_DIR/venv|g" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable video2listener
    sudo systemctl start video2listener
    echo "systemd 服务已启动"
    echo "状态: sudo systemctl status video2listener"
    echo "日志: sudo journalctl -u video2listener -f"
else
    echo "手动启动: $PROJECT_DIR/venv/bin/uvicorn src.bot.server:app --host 0.0.0.0 --port 8080"
fi

echo ""
echo "=== 部署完成 ==="
