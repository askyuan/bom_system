#!/usr/bin/env bash
# ============================================================
# 物料汇算系统 - 环境部署脚本
# 适用于 ARM Linux (Orange Pi / Raspberry Pi / Ubuntu / Debian)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "============================================================"
echo "  物料汇算系统 - 环境部署"
echo "============================================================"

# 1. 检查 python3
echo ""
echo "[1/4] 检查 Python 环境..."

if ! command -v python3 &>/dev/null; then
    echo "错误: 未找到 python3，请先安装:"
    echo "  sudo apt install python3 python3-full python3-venv"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python 版本: $PY_VERSION"

# 2. 确保 venv 模块可用
echo ""
echo "[2/4] 检查 venv 模块..."

if ! python3 -c "import venv" 2>/dev/null; then
    echo "  venv 模块缺失，正在安装..."
    sudo apt install -y python3-venv python3-full 2>/dev/null || {
        echo "  警告: apt 安装失败，请手动安装 python3-venv"
    }
fi

# 3. 创建虚拟环境
echo ""
echo "[3/4] 创建虚拟环境..."

if [ -d "$VENV_DIR" ]; then
    echo "  虚拟环境已存在: $VENV_DIR"
    echo "  如需重建，请先删除: rm -rf $VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
    echo "  已创建: $VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 4. 安装依赖
echo ""
echo "[4/4] 安装 Python 依赖..."

pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q

echo ""
echo "============================================================"
echo "  部署完成!"
echo "============================================================"
echo ""
echo "使用方法:"
echo "  ./run.sh demo                  # 运行演示"
echo "  ./run.sh material list         # 查询物料列表"
echo "  ./run.sh bom list              # 列出 BOM"
echo "  ./run.sh --help                # 查看全部命令"
echo ""
echo "或直接激活虚拟环境:"
echo "  source venv/bin/activate"
echo "  python cli.py --help"
echo ""
