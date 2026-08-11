#!/usr/bin/env bash
# ============================================================
# 物料汇算系统 - 环境部署脚本 (Linux / macOS / ARM)
# 适用于 Ubuntu / Debian / Raspberry Pi / Orange Pi
# 用法: bash setup.sh [--skip-init] [--force]
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
SKIP_INIT=0
FORCE=0

for arg in "$@"; do
    case "$arg" in
        --skip-init) SKIP_INIT=1 ;;
        --force) FORCE=1 ;;
    esac
done

echo "============================================================"
echo "  物料汇算系统 - 环境部署"
echo "============================================================"

# 1. 检查 python3
echo ""
echo "[1/7] 检查 Python 环境..."

if ! command -v python3 &>/dev/null; then
    echo "错误: 未找到 python3，请先安装:"
    echo "  sudo apt install python3 python3-full python3-venv"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python 版本: $PY_VERSION"

# 2. 确保 venv 模块可用
echo ""
echo "[2/7] 检查 venv 模块..."

if ! python3 -c "import venv" 2>/dev/null; then
    echo "  venv 模块缺失，正在安装..."
    sudo apt install -y python3-venv python3-full 2>/dev/null || {
        echo "  警告: apt 安装失败，请手动安装 python3-venv"
    }
fi

# 3. 创建虚拟环境
echo ""
echo "[3/7] 创建虚拟环境..."

if [ -d "$VENV_DIR" ]; then
    if [ "$FORCE" = "1" ]; then
        echo "  删除旧虚拟环境 (--force)..."
        rm -rf "$VENV_DIR"
    else
        echo "  虚拟环境已存在: $VENV_DIR"
        echo "  如需重建: bash setup.sh --force"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  已创建: $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# 4. 升级 pip
echo ""
echo "[4/7] 升级 pip..."

pip install --upgrade pip -q

# 5. 安装依赖
echo ""
echo "[5/7] 安装 Python 依赖..."

pip install -r "$SCRIPT_DIR/requirements.txt"

# 6. 创建运行目录
echo ""
echo "[6/7] 创建运行目录..."

mkdir -p "$SCRIPT_DIR/exports" "$SCRIPT_DIR/uploads" "$SCRIPT_DIR/backups" "$SCRIPT_DIR/logs"
echo "  目录已就绪: exports/ uploads/ backups/ logs/"

# 7. 初始化数据库
echo ""
echo "[7/7] 初始化数据库..."

if [ "$SKIP_INIT" = "1" ]; then
    echo "  跳过数据库初始化 (--skip-init)"
elif [ -f "$SCRIPT_DIR/material_db.db" ] && [ -f "$SCRIPT_DIR/bom_db.db" ] && [ "$FORCE" != "1" ]; then
    echo "  数据库已存在，跳过初始化"
    echo "  如需重建: bash setup.sh --force"
else
    python3 - "$SCRIPT_DIR" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from database import MaterialDatabase, BOMDatabase
db_dir = sys.argv[1]
mdb = MaterialDatabase(os.path.join(db_dir, "material_db.db"), bom_db_path=os.path.join(db_dir, "bom_db.db"))
bdb = BOMDatabase(os.path.join(db_dir, "bom_db.db"), material_db_path=os.path.join(db_dir, "material_db.db"))
mdb.initialize()
bdb.initialize()
print("  数据库初始化完成: material_db.db + bom_db.db")
EOF
fi

echo ""
echo "============================================================"
echo "  部署完成!"
echo "============================================================"
echo ""
echo "启动 Web 界面:"
echo "  ./run_web.sh                    # 默认 http://localhost:5000"
echo "  ./run_web.sh --port 8080"
echo ""
echo "使用 CLI:"
echo "  ./run.sh material list"
echo "  ./run.sh --help"
echo ""
echo "运行演示:"
echo "  ./run.sh demo"
echo ""
echo "激活虚拟环境:"
echo "  source venv/bin/activate"
echo ""
