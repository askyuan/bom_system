# ============================================================
#  物料汇算系统 - Windows 安装部署脚本 (PowerShell)
#  功能: 创建虚拟环境、安装依赖、初始化数据库和目录
#  用法: powershell -ExecutionPolicy Bypass -File setup.ps1
# ============================================================

param(
    [switch]$SkipInit,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV_DIR = Join-Path $SCRIPT_DIR "venv"
$PYTHON = Join-Path $VENV_DIR "Scripts\python.exe"
$PIP = Join-Path $VENV_DIR "Scripts\pip.exe"

function Write-Step($msg) { Write-Host "`n[$step/6] $msg" -ForegroundColor Cyan }
$step = 0

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  物料汇算系统 - 安装部署 (Windows)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

# ---------- 1. 检查 Python ----------
$step++
Write-Step "检查 Python 环境..."

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "错误: 未找到 python，请先安装 Python 3.8+" -ForegroundColor Red
    Write-Host "  下载地址: https://www.python.org/downloads/"
    Write-Host "  安装时请勾选 'Add Python to PATH'" -ForegroundColor Yellow
    exit 1
}

$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "  Python 版本: $pyVersion"
$pyMajor = [int]$pyVersion.Split(".")[0]
if ($pyMajor -lt 3) {
    exit 1
}

# ---------- 2. 创建虚拟环境 ----------
$step++
Write-Step "创建虚拟环境..."

if (Test-Path $VENV_DIR) {
    if ($Force) {
        Write-Host "  删除旧虚拟环境 (--Force)..."
        Remove-Item -Recurse -Force $VENV_DIR
    } else {
        Write-Host "  虚拟环境已存在: $VENV_DIR"
        Write-Host "  如需重建: powershell -ExecutionPolicy Bypass -File setup.ps1 -Force" -ForegroundColor Yellow
    }
}

if (-not (Test-Path $VENV_DIR)) {
    python -m venv $VENV_DIR
    if ($LASTEXITCODE -ne 0) {
        Write-Host "错误: 创建虚拟环境失败，请尝试: python -m venv --without-pip $VENV_DIR" -ForegroundColor Red
        exit 1
    }
    Write-Host "  已创建: $VENV_DIR"
}

# ---------- 3. 升级 pip ----------
$step++
Write-Step "升级 pip..."

& $PYTHON -m pip install --upgrade pip -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  pip 升级失败，继续尝试安装依赖..." -ForegroundColor Yellow
}

# ---------- 4. 安装依赖 ----------
$step++
Write-Step "安装 Python 依赖..."

$reqFile = Join-Path $SCRIPT_DIR "requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Host "错误: 未找到 requirements.txt" -ForegroundColor Red
    exit 1
}

& $PIP install -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "错误: 依赖安装失败" -ForegroundColor Red
    exit 1
}

# ---------- 5. 创建运行目录 ----------
$step++
Write-Step "创建运行目录..."

$dirs = @("exports", "uploads", "backups", "logs")
foreach ($d in $dirs) {
    $p = Join-Path $SCRIPT_DIR $d
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p | Out-Null
        Write-Host "  创建: $d/"
    }
}

# ---------- 6. 初始化数据库 ----------
if (-not $SkipInit) {
    $step++
    Write-Step "初始化数据库..."

    $db1 = Join-Path $SCRIPT_DIR "material_db.db"
    $db2 = Join-Path $SCRIPT_DIR "bom_db.db"

    if ((Test-Path $db1) -and (Test-Path $db2) -and -not $Force) {
        Write-Host "  数据库已存在，跳过初始化"
        Write-Host "  如需重建: powershell -ExecutionPolicy Bypass -File setup.ps1 -Force" -ForegroundColor Yellow
    } else {
        & $PYTHON -c @"
import os, sys
sys.path.insert(0, r"$SCRIPT_DIR")
from database import MaterialDatabase, BOMDatabase
mdb = MaterialDatabase(r"$db1", bom_db_path=r"$db2")
bdb = BOMDatabase(r"$db2", material_db_path=r"$db1")
mdb.initialize()
bdb.initialize()
print("  数据库初始化完成: material_db.db + bom_db.db")
"@
        if ($LASTEXITCODE -ne 0) {
            Write-Host "错误: 数据库初始化失败" -ForegroundColor Red
            exit 1
        }
    }
} else {
    Write-Host "`n[6/6] 跳过数据库初始化 (--SkipInit)" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  部署完成!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "启动 Web 界面:"
Write-Host "  .\start_web.bat            # 默认 http://localhost:5000"
Write-Host "  .\start_web.bat --port 8080"
Write-Host ""
Write-Host "使用 CLI:"
Write-Host "  .\venv\Scripts\python cli.py material list"
Write-Host "  .\venv\Scripts\python cli.py --help"
Write-Host ""
Write-Host "运行演示:"
Write-Host "  .\venv\Scripts\python main.py demo"
Write-Host ""
Write-Host "激活虚拟环境:"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host ""
