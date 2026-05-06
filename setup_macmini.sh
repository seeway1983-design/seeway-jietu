#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "[1/6] 检查 Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  echo "未检测到 Homebrew，请先安装 Homebrew: https://brew.sh/"
  exit 1
fi

echo "[2/6] 安装系统依赖"
brew install android-platform-tools node

echo "[3/6] 安装 Appium"
npm install -g appium

echo "[4/6] 创建 Python 虚拟环境"
python3 -m venv .venv

echo "[5/6] 安装 Python 依赖"
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "[6/6] 准备本地配置模板"
if [ ! -f config/device.local.json ]; then
  cp config/device.json config/device.local.json
  echo "已创建 config/device.local.json，请填写实际 adb_serial"
fi

if [ ! -f config/real_device_profile.local.json ]; then
  cp config/real_device_profile.json config/real_device_profile.local.json
  echo "已创建 config/real_device_profile.local.json，请按设备情况调整"
fi

echo "初始化完成。下一步建议执行："
echo "1. source .venv/bin/activate"
echo "2. 编辑 config/device.local.json"
echo "3. adb devices"
echo "4. bash tools/start_appium_server.sh"
echo "5. python3 run_mvp.py --doctor"
