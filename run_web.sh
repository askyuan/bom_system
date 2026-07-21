#!/usr/bin/env bash
# ============================================================
# 物料汇算系统 - Web 界面启动器
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# 检查虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo "虚拟环境未初始化，请先运行:"
    echo "  bash setup.sh"
    exit 1
fi

source "$VENV_DIR/bin/activate"

# 默认参数
HOST="${BOM_HOST:-0.0.0.0}"
PORT="${BOM_PORT:-5000}"
DB="${BOM_DB:-bom_system.db}"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --db)   DB="$2";   shift 2 ;;
        --debug) DEBUG="--debug"; shift ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

echo "============================================================"
echo "  物料汇算系统 - Web 界面"
echo "============================================================"
echo "  地址: http://$HOST:$PORT"
echo "  数据库: $DB"
echo ""
echo "  按 Ctrl+C 停止服务"
echo "============================================================"

python3 "$SCRIPT_DIR/web.py" --host "$HOST" --port "$PORT" --db "$DB" $DEBUG
