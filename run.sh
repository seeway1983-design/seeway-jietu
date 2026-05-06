#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "[1/4] 进入项目目录: $PROJECT_DIR"

if [ ! -d .venv ]; then
  echo "未找到 .venv，请先执行 bash setup_macmini.sh"
  exit 1
fi

echo "[2/4] 激活虚拟环境"
. .venv/bin/activate

echo "[3/4] 运行环境检查"
python3 run_mvp.py --doctor

echo "[4/4] 启动默认采集流程"
python3 run_mvp.py --capture-one --city 福州市 --brand 卫龙 --strict --fast --overwrite --output-mode h5 --debug-recommendation
