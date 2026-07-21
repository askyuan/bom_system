#!/usr/bin/env bash
# ============================================================
# 物料汇算系统 - 便捷启动器
# 自动激活虚拟环境并转发所有参数给 CLI
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# 检查虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo "虚拟环境未初始化，请先运行:"
    echo "  bash setup.sh"
    exit 1
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 处理 main.py demo 的特殊情况
if [ "$1" = "demo" ]; then
    python3 "$SCRIPT_DIR/main.py" demo
else
    # 转发所有参数给 cli.py
    python3 "$SCRIPT_DIR/cli.py" "$@"
fi
