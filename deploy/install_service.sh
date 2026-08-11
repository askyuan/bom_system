#!/usr/bin/env bash
# ============================================================
#  物料汇算系统 - systemd 服务安装脚本 (Linux)
#
#  用法:
#    bash install_service.sh                    # 默认端口 5000，当前用户
#    bash install_service.sh --port 8080        # 自定义端口
#    bash install_service.sh --user nginx       # 指定运行用户
#    bash install_service.sh --host 127.0.0.1   # 仅本机访问
#    bash install_service.sh --uninstall        # 卸载服务
#
#  服务名称: bom_system
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="bom_system"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# 默认参数
HOST="0.0.0.0"
PORT="5000"
RUN_USER=""
UNINSTALL=0

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)    PORT="$2"; shift 2 ;;
        --host)    HOST="$2"; shift 2 ;;
        --user)    RUN_USER="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)
            echo "用法: bash install_service.sh [--port 5000] [--host 0.0.0.0] [--user NAME] [--uninstall]"
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 卸载 ----------
if [ "$UNINSTALL" = "1" ]; then
    echo "正在卸载服务 ${SERVICE_NAME}..."
    if [ -f "$SERVICE_FILE" ]; then
        sudo systemctl stop ${SERVICE_NAME} 2>/dev/null || true
        sudo systemctl disable ${SERVICE_NAME} 2>/dev/null || true
        sudo rm -f "$SERVICE_FILE"
        sudo systemctl daemon-reload
        echo "✅ 服务已卸载"
    else
        echo "服务未安装"
    fi
    exit 0
fi

# ---------- 前置检查 ----------
if [ ! -f "$PROJECT_DIR/web.py" ]; then
    echo "错误: 未找到 web.py，请在 deploy/ 目录或项目根目录执行本脚本"
    exit 1
fi

VENV_DIR="$PROJECT_DIR/venv"
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "错误: 虚拟环境不存在，请先运行: bash $PROJECT_DIR/setup.sh"
    exit 1
fi

# 确定运行用户
if [ -z "$RUN_USER" ]; then
    RUN_USER="$(id -un)"
fi
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

echo "============================================================"
echo "  安装 systemd 服务: ${SERVICE_NAME}"
echo "============================================================"
echo "  项目目录: $PROJECT_DIR"
echo "  Python:   $VENV_DIR/bin/python"
echo "  监听地址: $HOST:$PORT"
echo "  运行用户: $RUN_USER"
echo ""

# ---------- 生成服务文件 ----------
TEMPLATE="$SCRIPT_DIR/bom_system.service.template"
if [ ! -f "$TEMPLATE" ]; then
    echo "错误: 未找到模板文件 $TEMPLATE"
    exit 1
fi

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__VENV_PYTHON__|$VENV_DIR/bin/python|g" \
    -e "s|__HOST__|$HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
    "$TEMPLATE" > "/tmp/${SERVICE_NAME}.service"

echo "已生成服务配置:"
cat "/tmp/${SERVICE_NAME}.service"

# ---------- 安装 ----------
echo ""
read -p "确认安装该服务? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消"
    exit 1
fi

sudo cp "/tmp/${SERVICE_NAME}.service" "$SERVICE_FILE"
rm -f "/tmp/${SERVICE_NAME}.service"

# 确保项目目录对运行用户可访问
if [ "$RUN_USER" != "$(id -un)" ]; then
    echo "提示: 请确保 $RUN_USER 对 $PROJECT_DIR 有读写权限"
fi

# ---------- 启动服务 ----------
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

sleep 2

# ---------- 检查状态 ----------
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo ""
    echo "============================================================"
    echo "  ✅ 服务已启动!"
    echo "============================================================"
    echo "  访问地址: http://$HOST:$PORT"
    echo ""
    echo "常用命令:"
    echo "  systemctl status ${SERVICE_NAME}    # 查看状态"
    echo "  systemctl restart ${SERVICE_NAME}   # 重启"
    echo "  systemctl stop ${SERVICE_NAME}      # 停止"
    echo "  systemctl disable ${SERVICE_NAME}   # 取消开机自启"
    echo "  journalctl -u ${SERVICE_NAME} -f    # 查看日志"
    echo ""
    echo "卸载: bash $SCRIPT_DIR/install_service.sh --uninstall"
else
    echo ""
    echo "⚠️  服务启动失败，请查看日志:"
    echo "  journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi
