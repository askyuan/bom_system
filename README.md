# 物料汇算系统 (BOM System)

物料全生命周期管理系统：物料库管理、BOM 导入、物料汇算、报表导出、审计日志。

## 功能特性

- **物料库管理**：分类、制造商、封装标准化、单位转换、生命周期状态管理
- **BOM 管理**：两阶段导入（校验→确认）、版本状态机、版本对比
- **物料汇算**：跨多 BOM 需求合并计算，支持损耗率、MOQ、SPQ、异步执行、增量汇算
- **报表导出**：Excel / CSV 格式，支持汇算报表、物料库、BOM、版本差异
- **双界面**：Web 管理界面 + CLI 命令行
- **审计日志**：所有关键操作记录

## 环境要求

| 依赖 | 版本 |
|------|------|
| Python | 3.8+ |
| pip | 最新版 |
| openpyxl | >= 3.0.0 |
| flask | >= 2.0.0 |
| pandas | >= 1.3.0 (批量导入时需要) |

## 快速安装

### Windows

```powershell
# 一键安装（创建虚拟环境 + 安装依赖 + 初始化数据库）
setup.bat

# 或者使用 PowerShell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### Linux / macOS / ARM (树莓派/香橙派)

```bash
bash setup.sh
```

### 手动安装

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 启动服务

### Web 界面

```bash
# Windows
start_web.bat                    # 默认 http://localhost:5000
start_web.bat --port 8080        # 自定义端口
start_web.bat --debug            # 调试模式

# Linux
./run_web.sh
./run_web.sh --port 8080
```

### CLI

```bash
# Linux (自动激活虚拟环境)
./run.sh material list
./run.sh bom list
./run.sh --help

# Windows (手动激活或直接使用 venv 中的 python)
venv\Scripts\python cli.py material list
```

### 快速演示

```bash
./run.sh demo                    # Linux
venv\Scripts\python main.py demo # Windows
```

## 常用命令

### 物料管理

```bash
python cli.py material list                    # 列出物料
python cli.py material create --mpn "RC0402FR-0710KL" --category RES --manufacturer "Yageo" --description "10K 1% 0402"
python cli.py material import --file materials.xlsx
```

### BOM 管理

```bash
python cli.py bom list                         # 列出 BOM
python cli.py bom validate --file bom.xlsx     # 校验导入
python cli.py bom import --file bom.xlsx --board "MainBoard" --version "Rev1.0"
python cli.py bom release 1                    # 发布 BOM
python cli.py bom compare 1 2                  # 对比两个 BOM
```

### 物料汇算

```bash
python cli.py calc create --boms 1:100 2:50    # 汇算 (MainBoard x100, PowerBoard x50)
python cli.py calc status 1                    # 查看状态
python cli.py calc items 1                     # 查看明细
python cli.py export calc 1                    # 导出报表
```

### 系统管理

```bash
python cli.py system backup                    # 备份数据库
python cli.py system check                     # 完整性检查
python cli.py system stats                     # 系统统计
```

## 项目结构

```
bom_system/
├── audit.py              # 审计日志
├── bom_processor.py      # BOM 导入/管理
├── calculation_engine.py # 物料汇算引擎
├── cli.py                # 命令行界面
├── database.py           # 数据库 (双库架构: 物料库 + BOM库)
├── main.py               # 主入口/演示
├── material_manager.py   # 物料库管理
├── ref_designator.py     # 位号解析/合并
├── report_exporter.py    # 报表导出 (Excel/CSV)
├── unit_converter.py     # 单位转换
├── web.py                # Web 界面 (Flask)
├── templates/            # Web 前端模板
├── uploads/              # 上传文件目录
├── exports/              # 导出文件目录
├── backups/              # 数据库备份目录
├── setup.ps1 / setup.bat # Windows 安装部署
├── setup.sh              # Linux 安装部署
└── requirements.txt      # Python 依赖
```

## 数据库

系统使用双库架构：

- `material_db.db` — 物料主数据（物料、分类、制造商、封装、单位转换）
- `bom_db.db` — BOM 数据（BOM、汇算任务、审计日志、导入日志）

两个数据库通过 SQLite `ATTACH DATABASE` 支持跨库查询。

## 备份

```bash
# 数据库备份到 backups/ 目录
python cli.py system backup

# 或手动复制
cp material_db.db bom_db.db backups/
```

## 部署到服务器 (Linux)

```bash
# 1. 拉取代码
git clone https://github.com/askyuan/bom_system.git
cd bom_system

# 2. 安装
bash setup.sh

# 3. 后台运行 Web 服务 (使用 nohup)
nohup ./run_web.sh --port 5000 > logs/web.log 2>&1 &

# 4. 开机自启 (systemd)
# 创建 /etc/systemd/system/bom_system.service:
#   [Unit]
#   Description=BOM System
#   After=network.target
#
#   [Service]
#   WorkingDirectory=/path/to/bom_system
#   ExecStart=/path/to/bom_system/venv/bin/python /path/to/bom_system/web.py --port 5000
#   Restart=always
#
#   [Install]
#   WantedBy=multi-user.target

# 启用服务
# sudo systemctl enable bom_system
# sudo systemctl start bom_system
```
