#!/bin/bash
# video2listener 一键部署脚本
set -euo pipefail

echo "=== video2listener 部署 ==="
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

VENV_DIR="$PROJECT_DIR/.venv"

# 1. Python 环境（目录名与 CLAUDE.md / launchd plist 保持一致：.venv）
echo "[1/5] 创建虚拟环境..."
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

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
echo "服务密钥请写入 /etc/video2listener.env（不要写进单元文件，空值会覆盖外部变量）："
echo "  sudo cp deploy/env.example /etc/video2listener.env"
echo "  sudo chmod 600 /etc/video2listener.env"
echo "需要填写的变量（仅这三个会被读取）："
echo "  VIDEO2LISTENER_DEEPSEEK_API_KEY"
echo "  VIDEO2LISTENER_DEEPSEEK_BASE_URL"
echo "  VIDEO2LISTENER_MIMI_API_KEY"
echo ""

# 5. systemd 服务
echo "[5/5] 配置 systemd 服务..."
SERVICE_FILE="/etc/systemd/system/video2listener.service"

if [ -f deploy/video2listener.service ]; then
    sudo cp deploy/video2listener.service "$SERVICE_FILE"
    sudo sed -i "s|/path/to/video2listener|$PROJECT_DIR|g" "$SERVICE_FILE"
    sudo sed -i "s|/path/to/venv|$VENV_DIR|g" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable video2listener
    sudo systemctl start video2listener
    echo "systemd 服务已启动"
    echo "状态: sudo systemctl status video2listener"
    echo "日志: sudo journalctl -u video2listener -f"
else
    echo "手动启动: $VENV_DIR/bin/uvicorn src.web.server:app --host 0.0.0.0 --port 8080"
fi

echo ""
echo "=== 部署完成 ==="
