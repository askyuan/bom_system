"""
数据库初始化与管理模块（双库架构）
- MaterialDatabase: 物料库（物料、分类、制造商、封装、单位转换）
- BOMDatabase: BOM库（BOM、汇算、审计日志、导入日志）
两个数据库通过 SQLite ATTACH DATABASE 支持跨库查询。
"""

import sqlite3
import os
from contextlib import contextmanager

# ============================================================
# 物料库 Schema
# ============================================================

MATERIAL_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    display_name    TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('Admin','Engineer','Planner','Purchaser','Viewer')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 制造商表
CREATE TABLE IF NOT EXISTS manufacturers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    alias           TEXT,
    website         TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by      INTEGER REFERENCES users(id)
);

-- 分类表（支持层级）
CREATE TABLE IF NOT EXISTS categories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    code_prefix         TEXT    NOT NULL UNIQUE,
    parent_id           INTEGER REFERENCES categories(id),
    default_loss_rate   REAL    NOT NULL DEFAULT 0.03,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          INTEGER REFERENCES users(id)
);

-- 单位转换规则表
CREATE TABLE IF NOT EXISTS unit_conversions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alias           TEXT    NOT NULL UNIQUE,
    standard_unit   TEXT    NOT NULL,
    factor          REAL    NOT NULL,
    category        TEXT
);

-- 物料主数据表
CREATE TABLE IF NOT EXISTS materials (
    part_number         TEXT PRIMARY KEY,
    manufacturer_id     INTEGER REFERENCES manufacturers(id),
    mpn                 TEXT,
    description         TEXT,
    category_id         INTEGER NOT NULL REFERENCES categories(id),
    value               REAL,
    unit                TEXT,
    footprint           TEXT,
    lifecycle_status    TEXT    NOT NULL DEFAULT 'Active'
                            CHECK (lifecycle_status IN ('Active','NRND','EOL')),
    datasheet_url       TEXT,
    default_loss_rate   REAL,
    moq                 INTEGER NOT NULL DEFAULT 1,
    spq                 INTEGER NOT NULL DEFAULT 1,
    stock_qty           INTEGER NOT NULL DEFAULT 0,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          INTEGER REFERENCES users(id),
    UNIQUE (manufacturer_id, mpn)
);

-- 封装表
CREATE TABLE IF NOT EXISTS footprints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by      INTEGER REFERENCES users(id)
);

-- 封装别名表
CREATE TABLE IF NOT EXISTS footprint_aliases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alias           TEXT    NOT NULL UNIQUE,
    standard_name   TEXT    NOT NULL
);

-- 外部编码映射表
CREATE TABLE IF NOT EXISTS external_part_mapping (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number         TEXT NOT NULL REFERENCES materials(part_number),
    external_code       TEXT NOT NULL,
    external_system     TEXT,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 系统配置表
CREATE TABLE IF NOT EXISTS system_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_materials_category ON materials(category_id);
CREATE INDEX IF NOT EXISTS idx_materials_status    ON materials(lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_materials_mpn       ON materials(mpn);

-- 触发器
CREATE TRIGGER IF NOT EXISTS trg_materials_updated_at
AFTER UPDATE ON materials
FOR EACH ROW BEGIN
    UPDATE materials SET updated_at = CURRENT_TIMESTAMP WHERE part_number = NEW.part_number;
END;

CREATE TRIGGER IF NOT EXISTS trg_manufacturers_updated_at
AFTER UPDATE ON manufacturers
FOR EACH ROW BEGIN
    UPDATE manufacturers SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_categories_updated_at
AFTER UPDATE ON categories
FOR EACH ROW BEGIN
    UPDATE categories SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_users_updated_at
AFTER UPDATE ON users
FOR EACH ROW BEGIN
    UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_footprints_updated_at
AFTER UPDATE ON footprints
FOR EACH ROW BEGIN
    UPDATE footprints SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
"""

# ============================================================
# BOM 库 Schema
# ============================================================

BOM_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- BOM 表头
CREATE TABLE IF NOT EXISTS bom_headers (
    bom_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_name      TEXT    NOT NULL,
    version         TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'Draft'
                        CHECK (status IN ('Draft','Released','Obsolete')),
    release_date    DATETIME,
    approved_by     INTEGER,
    approved_at     DATETIME,
    notes           TEXT,
    parent_bom_id   INTEGER REFERENCES bom_headers(bom_id),
    change_notes    TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by      INTEGER,
    UNIQUE (board_name, version)
);

-- BOM 明细表
CREATE TABLE IF NOT EXISTS bom_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    bom_id                  INTEGER NOT NULL REFERENCES bom_headers(bom_id) ON DELETE CASCADE,
    part_number             TEXT    NOT NULL,
    quantity                INTEGER NOT NULL CHECK (quantity > 0),
    reference_designators   TEXT,
    ref_count               INTEGER NOT NULL DEFAULT 0,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by              INTEGER,
    UNIQUE (bom_id, part_number)
);

-- 汇算任务表
CREATE TABLE IF NOT EXISTS calculation_tasks (
    task_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status              TEXT    NOT NULL DEFAULT 'Pending'
                            CHECK (status IN ('Pending','Running','Completed','Failed')),
    parent_task_id      INTEGER REFERENCES calculation_tasks(task_id),
    error_message       TEXT,
    started_at          DATETIME,
    completed_at        DATETIME,
    duration_ms         INTEGER,
    result_file_path    TEXT,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          INTEGER
);

-- 汇算明细表
CREATE TABLE IF NOT EXISTS calculation_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES calculation_tasks(task_id) ON DELETE CASCADE,
    part_number         TEXT    NOT NULL,
    theoretical_qty     INTEGER NOT NULL DEFAULT 0,
    loss_rate           REAL    NOT NULL DEFAULT 0,
    loss_included_qty   INTEGER NOT NULL DEFAULT 0,
    stock_qty           INTEGER NOT NULL DEFAULT 0,
    suggested_qty       INTEGER NOT NULL DEFAULT 0,
    final_qty           INTEGER NOT NULL DEFAULT 0,
    source_details      TEXT    NOT NULL DEFAULT '[]',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 汇算任务-BOM关联表
CREATE TABLE IF NOT EXISTS calculation_boms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES calculation_tasks(task_id) ON DELETE CASCADE,
    bom_id              INTEGER NOT NULL REFERENCES bom_headers(bom_id),
    order_quantity      INTEGER NOT NULL CHECK (order_quantity > 0),
    UNIQUE (task_id, bom_id)
);

-- 汇算物料合并表
CREATE TABLE IF NOT EXISTS calculation_merges (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES calculation_tasks(task_id) ON DELETE CASCADE,
    from_part_number    TEXT    NOT NULL,
    to_part_number      TEXT    NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (task_id, from_part_number)
);

-- 导入日志表
CREATE TABLE IF NOT EXISTS import_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name           TEXT    NOT NULL,
    file_type           TEXT    NOT NULL CHECK (file_type IN ('bom','material')),
    target_bom_id       INTEGER REFERENCES bom_headers(bom_id),
    total_rows          INTEGER NOT NULL DEFAULT 0,
    success_rows        INTEGER NOT NULL DEFAULT 0,
    failed_rows         INTEGER NOT NULL DEFAULT 0,
    validation_report   TEXT,
    status              TEXT    NOT NULL CHECK (status IN ('success','partial','failed')),
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by          INTEGER
);

-- 操作审计日志表
CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id         INTEGER,
    action          TEXT    NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    detail          TEXT,
    ip_address      TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_bom_headers_status  ON bom_headers(status);
CREATE INDEX IF NOT EXISTS idx_bom_headers_board   ON bom_headers(board_name);
CREATE INDEX IF NOT EXISTS idx_bom_items_part       ON bom_items(part_number);
CREATE INDEX IF NOT EXISTS idx_bom_items_bom        ON bom_items(bom_id);
CREATE INDEX IF NOT EXISTS idx_calc_items_task      ON calculation_items(task_id);
CREATE INDEX IF NOT EXISTS idx_calc_items_part      ON calculation_items(part_number);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp      ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user           ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action         ON audit_logs(action);

-- 触发器
CREATE TRIGGER IF NOT EXISTS trg_bom_headers_updated_at
AFTER UPDATE ON bom_headers
FOR EACH ROW BEGIN
    UPDATE bom_headers SET updated_at = CURRENT_TIMESTAMP WHERE bom_id = NEW.bom_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_bom_items_updated_at
AFTER UPDATE ON bom_items
FOR EACH ROW BEGIN
    UPDATE bom_items SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_calc_tasks_updated_at
AFTER UPDATE ON calculation_tasks
FOR EACH ROW BEGIN
    UPDATE calculation_tasks SET updated_at = CURRENT_TIMESTAMP WHERE task_id = NEW.task_id;
END;
"""

# ============================================================
# 种子数据
# ============================================================

SEED_USERS = [
    ("admin", "系统管理员", "Admin"),
]

SEED_CATEGORIES_TOP = [
    ("电阻",       "RES",  0.03),
    ("电容",       "CAP",  0.03),
    ("电感",       "IND",  0.03),
    ("二极管",     "DIO",  0.02),
    ("晶体管",     "TRA",  0.02),
    ("IC芯片",     "ICS",  0.01),
    ("连接器",     "CON",  0.01),
    ("结构件",     "MEC",  0.00),
    ("其他",       "MISC", 0.03),
]

SEED_CATEGORIES_SUB = [
    ("贴片电阻",   "RES-SMD",  "RES", 0.03),
    ("插件电阻",   "RES-TH",   "RES", 0.03),
    ("功率电阻",   "RES-PWR",  "RES", 0.03),
    ("排阻",       "RES-ARR",  "RES", 0.03),
    ("贴片电容",   "CAP-SMD",  "CAP", 0.03),
    ("钽电容",     "CAP-TAN",  "CAP", 0.03),
    ("铝电解电容", "CAP-ALU",  "CAP", 0.03),
    ("薄膜电容",   "CAP-FIL",  "CAP", 0.03),
    ("陶瓷插件电容","CAP-TH",  "CAP", 0.03),
    ("贴片电感",   "IND-SMD",  "IND", 0.03),
    ("插件电感",   "IND-TH",   "IND", 0.03),
    ("磁珠",       "IND-FER",  "IND", 0.03),
    ("功率电感",   "IND-PWR",  "IND", 0.03),
    ("肖特基二极管","DIO-SCH",  "DIO", 0.02),
    ("稳压二极管", "DIO-ZEN",  "DIO", 0.02),
    ("TVS二极管",  "DIO-TVS",  "DIO", 0.02),
    ("LED",        "DIO-LED",  "DIO", 0.02),
    ("整流二极管", "DIO-REC",  "DIO", 0.02),
    ("MOSFET",     "TRA-MOS",  "TRA", 0.02),
    ("三极管(BJT)","TRA-BJT",  "TRA", 0.02),
    ("IGBT",       "TRA-IGBT", "TRA", 0.02),
    ("MCU",        "ICS-MCU",  "ICS", 0.01),
    ("电源管理IC", "ICS-PMIC", "ICS", 0.01),
    ("运放/比较器","ICS-AMP",  "ICS", 0.01),
    ("逻辑IC",     "ICS-LOG",  "ICS", 0.01),
    ("ADC/DAC",    "ICS-ADC",  "ICS", 0.01),
    ("存储IC",     "ICS-MEM",  "ICS", 0.01),
    ("接口IC",     "ICS-IF",   "ICS", 0.01),
    ("传感器",     "ICS-SEN",  "ICS", 0.01),
    ("排针/排母",  "CON-PIN",  "CON", 0.01),
    ("USB连接器",  "CON-USB",  "CON", 0.01),
    ("FPC连接器",  "CON-FPC",  "CON", 0.01),
    ("板对板连接器","CON-BTB", "CON", 0.01),
    ("端子",       "CON-TERM", "CON", 0.01),
    ("螺丝/螺母",  "MEC-SCR",  "MEC", 0.00),
    ("散热片",     "MEC-HSK",  "MEC", 0.00),
    ("外壳/支架",  "MEC-CASE", "MEC", 0.00),
]

SEED_CATEGORIES = SEED_CATEGORIES_TOP  # 兼容旧版

SEED_FOOTPRINTS = [
    ("01005",  "英制 01005 (0.4×0.2mm)"),
    ("0201",   "英制 0201 (0.6×0.3mm)"),
    ("0402",   "英制 0402 (1.0×0.5mm)"),
    ("0603",   "英制 0603 (1.6×0.8mm)"),
    ("0805",   "英制 0805 (2.0×1.25mm)"),
    ("1206",   "英制 1206 (3.2×1.6mm)"),
    ("1210",   "英制 1210 (3.2×2.5mm)"),
    ("2010",   "英制 2010 (5.0×2.5mm)"),
    ("2512",   "英制 2512 (6.3×3.2mm)"),
    ("SOT-23",    "小外形晶体管 3脚"),
    ("SOT-23-5",  "小外形晶体管 5脚"),
    ("SOT-23-6",  "小外形晶体管 6脚"),
    ("SOT-89",    "中功率晶体管"),
    ("SOT-223",   "中功率晶体管"),
    ("SOP-8",     "小外形封装 8脚"),
    ("SOP-16",    "小外形封装 16脚"),
    ("SSOP-8",    "缩窄小外形 8脚"),
    ("TSSOP-8",   "薄型缩窄小外形 8脚"),
    ("TSSOP-16",  "薄型缩窄小外形 16脚"),
    ("TSSOP-20",  "薄型缩窄小外形 20脚"),
    ("QFN-16",    "无引脚方形扁平 16脚"),
    ("QFN-20",    "无引脚方形扁平 20脚"),
    ("QFN-24",    "无引脚方形扁平 24脚"),
    ("QFN-32",    "无引脚方形扁平 32脚"),
    ("QFN-48",    "无引脚方形扁平 48脚"),
    ("QFP-32",    "方形扁平 32脚"),
    ("QFP-48",    "方形扁平 48脚"),
    ("QFP-64",    "方形扁平 64脚"),
    ("QFP-100",   "方形扁平 100脚"),
    ("LQFP-48",   "薄型方形扁平 48脚"),
    ("LQFP-64",   "薄型方形扁平 64脚"),
    ("LQFP-100",  "薄型方形扁平 100脚"),
    ("LQFP-144",  "薄型方形扁平 144脚"),
    ("BGA-256",   "球栅阵列 256球"),
    ("BGA-484",   "球栅阵列 484球"),
    ("WLCSP",     "晶圆级芯片封装"),
    ("DIP-8",     "双列直插 8脚"),
    ("DIP-14",    "双列直插 14脚"),
    ("DIP-16",    "双列直插 16脚"),
    ("DIP-28",    "双列直插 28脚"),
    ("TO-92",     "小功率晶体管直插"),
    ("TO-220",    "中功率晶体管/稳压器"),
    ("TO-252",    "中功率贴片 (DPAK)"),
    ("TO-263",    "中功率贴片 (D2PAK)"),
    ("SMD",       "通用贴片封装"),
    ("THT",       "通用直插封装"),
    ("MODULE",    "模块封装"),
]

SEED_UNIT_CONVERSIONS = [
    ("Ohm",    "Ohm", 1.0,    "RES"),
    ("ohm",    "Ohm", 1.0,    "RES"),
    ("Ω",      "Ohm", 1.0,    "RES"),
    ("R",      "Ohm", 1.0,    "RES"),
    ("K",      "Ohm", 1e3,    "RES"),
    ("k",      "Ohm", 1e3,    "RES"),
    ("kΩ",     "Ohm", 1e3,    "RES"),
    ("Kohm",   "Ohm", 1e3,    "RES"),
    ("kohm",   "Ohm", 1e3,    "RES"),
    ("M",      "Ohm", 1e6,    "RES"),
    ("Mohm",   "Ohm", 1e6,    "RES"),
    ("MΩ",     "Ohm", 1e6,    "RES"),
    ("F",      "F",   1.0,     "CAP"),
    ("mF",     "F",   1e-3,    "CAP"),
    ("uF",     "F",   1e-6,    "CAP"),
    ("μF",     "F",   1e-6,    "CAP"),
    ("microF", "F",   1e-6,    "CAP"),
    ("nF",     "F",   1e-9,    "CAP"),
    ("pF",     "F",   1e-12,   "CAP"),
    ("H",      "H",   1.0,     "IND"),
    ("mH",     "H",   1e-3,    "IND"),
    ("uH",     "H",   1e-6,    "IND"),
    ("μH",     "H",   1e-6,    "IND"),
    ("nH",     "H",   1e-9,    "IND"),
    ("A",      "A",   1.0,     None),
    ("mA",     "A",   1e-3,    None),
    ("uA",     "A",   1e-6,    None),
    ("μA",     "A",   1e-6,    None),
    ("V",      "V",   1.0,     None),
    ("mV",     "V",   1e-3,    None),
    ("kV",     "V",   1e3,     None),
    ("W",      "W",   1.0,     None),
    ("mW",     "W",   1e-3,    None),
    ("kW",     "W",   1e3,     None),
    ("Hz",     "Hz",  1.0,     None),
    ("kHz",    "Hz",  1e3,     None),
    ("MHz",    "Hz",  1e6,     None),
    ("GHz",    "Hz",  1e9,     None),
]

SEED_FOOTPRINT_ALIASES = [
    ("sot23",      "SOT-23"),
    ("sot23-3",    "SOT-23"),
    ("sot23-5",    "SOT-23-5"),
    ("sot-23-5",   "SOT-23-5"),
    ("sot23-6",    "SOT-23-6"),
    ("sot-23-6",   "SOT-23-6"),
    ("sot223",     "SOT-223"),
    ("sot-223",    "SOT-223"),
    ("soic8",      "SOIC-8"),
    ("soic-8",     "SOIC-8"),
    ("so8",        "SOIC-8"),
    ("so-8",       "SOIC-8"),
    ("soic16",     "SOIC-16"),
    ("soic-16",    "SOIC-16"),
    ("so16",       "SOIC-16"),
    ("tssop8",     "TSSOP-8"),
    ("tssop-8",    "TSSOP-8"),
    ("tssop16",    "TSSOP-16"),
    ("tssop-16",   "TSSOP-16"),
    ("tssop20",    "TSSOP-20"),
    ("tssop-20",   "TSSOP-20"),
    ("qfn16",      "QFN-16"),
    ("qfn-16",     "QFN-16"),
    ("qfn20",      "QFN-20"),
    ("qfn-20",     "QFN-20"),
    ("qfn24",      "QFN-24"),
    ("qfn-24",     "QFN-24"),
    ("qfn32",      "QFN-32"),
    ("qfn-32",     "QFN-32"),
    ("qfn48",      "QFN-48"),
    ("qfn-48",     "QFN-48"),
    ("qfp44",      "QFP-44"),
    ("qfp-44",     "QFP-44"),
    ("qfp48",      "QFP-48"),
    ("qfp-48",     "QFP-48"),
    ("qfp64",      "QFP-64"),
    ("qfp-64",     "QFP-64"),
    ("qfp100",     "QFP-100"),
    ("qfp-100",    "QFP-100"),
    ("bga256",     "BGA-256"),
    ("bga-256",    "BGA-256"),
    ("lqfp48",     "LQFP-48"),
    ("lqfp-48",    "LQFP-48"),
    ("lqfp64",     "LQFP-64"),
    ("lqfp-64",    "LQFP-64"),
    ("lqfp100",    "LQFP-100"),
    ("lqfp-100",   "LQFP-100"),
    ("dip8",       "DIP-8"),
    ("dip-8",      "DIP-8"),
    ("dip16",      "DIP-16"),
    ("dip-16",     "DIP-16"),
    ("dip28",      "DIP-28"),
    ("dip-28",     "DIP-28"),
    ("0201",       "0201"),
    ("0402",       "0402"),
    ("0603",       "0603"),
    ("0805",       "0805"),
    ("1206",       "1206"),
    ("1210",       "1210"),
    ("2512",       "2512"),
]

SEED_SYSTEM_CONFIG = [
    ("backup_frequency",    "daily",    "备份频率: daily / weekly"),
    ("backup_retain_days",  "30",       "每日备份保留天数"),
    ("backup_time",         "02:00",    "每日备份执行时间（HH:MM）"),
    ("default_loss_rate",   "0.03",     "全局默认损耗率"),
    ("ref_display_max_len", "120",      "位号合并显示最大字符数"),
]


# ============================================================
# 基类
# ============================================================

class _BaseDB:
    """数据库管理器基类：连接管理、事务、跨库连接。"""

    def __init__(self, db_path: str, other_db_path: str = ""):
        self.db_path = os.path.abspath(db_path) if db_path else db_path
        self.other_db_path = os.path.abspath(other_db_path) if other_db_path else other_db_path
        self._ensure_dir()

    def _ensure_dir(self):
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _configure(self, conn):
        """为连接设置通用 PRAGMA。"""
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def get_connection(self):
        """获取单库连接（上下文管理器）。"""
        conn = sqlite3.connect(self.db_path)
        self._configure(conn)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """在事务中执行操作，异常时自动回滚。"""
        with self.get_connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def backup(self, backup_path: str):
        """使用 SQLite Online Backup API 创建一致性备份。"""
        with self.get_connection() as src_conn:
            dst_conn = sqlite3.connect(backup_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()

    def integrity_check(self) -> bool:
        """执行完整性检查。"""
        with self.get_connection() as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return result[0] == "ok"


# ============================================================
# 物料库
# ============================================================

class MaterialDatabase(_BaseDB):
    """物料库：物料、分类、制造商、封装、单位转换、系统配置。"""

    def __init__(self, db_path: str = "material_db.db", bom_db_path: str = ""):
        super().__init__(db_path, bom_db_path)

    def initialize(self):
        """创建所有表、索引、触发器，并灌入种子数据（幂等操作）。"""
        with self.get_connection() as conn:
            conn.executescript(MATERIAL_SCHEMA_SQL)
            self._migrate_schema(conn)
            self._seed_data(conn)
            conn.commit()

    def _migrate_schema(self, conn):
        """迁移旧版 schema：放宽 materials 表的 NOT NULL 约束。"""
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(materials)").fetchall()}
        # 检查需要放宽 NOT NULL 约束的列 (notnull=1 表示有约束)
        mfr_col = cols.get("manufacturer_id")
        mpn_col = cols.get("mpn")
        desc_col = cols.get("description")
        needs_migrate = False
        if mfr_col and mfr_col[3] == 1:  # notnull
            needs_migrate = True
        if mpn_col and mpn_col[3] == 1:
            needs_migrate = True
        if desc_col and desc_col[3] == 1:
            needs_migrate = True
        if not needs_migrate:
            return

        # 重建表以放宽约束
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS _materials_new (
                part_number         TEXT PRIMARY KEY,
                manufacturer_id     INTEGER REFERENCES manufacturers(id),
                mpn                 TEXT,
                description         TEXT,
                category_id         INTEGER NOT NULL REFERENCES categories(id),
                value               REAL,
                unit                TEXT,
                footprint           TEXT,
                lifecycle_status    TEXT    NOT NULL DEFAULT 'Active'
                                        CHECK (lifecycle_status IN ('Active','NRND','EOL')),
                datasheet_url       TEXT,
                default_loss_rate   REAL,
                moq                 INTEGER NOT NULL DEFAULT 1,
                spq                 INTEGER NOT NULL DEFAULT 1,
                stock_qty           INTEGER NOT NULL DEFAULT 0,
                created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by          INTEGER REFERENCES users(id),
                UNIQUE (manufacturer_id, mpn)
            );
            INSERT OR IGNORE INTO _materials_new SELECT * FROM materials;
            DROP TABLE materials;
            ALTER TABLE _materials_new RENAME TO materials;
        """)

    def _seed_data(self, conn):
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO users (username, display_name, role) VALUES (?,?,?)",
                SEED_USERS,
            )

        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            for name, code_prefix, loss_rate in SEED_CATEGORIES_TOP:
                conn.execute(
                    "INSERT INTO categories (name, code_prefix, parent_id, default_loss_rate) "
                    "VALUES (?,?,NULL,?)",
                    (name, code_prefix, loss_rate),
                )
            for name, code_prefix, parent_prefix, loss_rate in SEED_CATEGORIES_SUB:
                parent_id = conn.execute(
                    "SELECT id FROM categories WHERE code_prefix = ?",
                    (parent_prefix,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO categories (name, code_prefix, parent_id, default_loss_rate) "
                    "VALUES (?,?,?,?)",
                    (name, code_prefix, parent_id, loss_rate),
                )

        if conn.execute("SELECT COUNT(*) FROM unit_conversions").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO unit_conversions (alias, standard_unit, factor, category) "
                "VALUES (?,?,?,?)",
                SEED_UNIT_CONVERSIONS,
            )

        if conn.execute("SELECT COUNT(*) FROM system_config").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO system_config (key, value, description) VALUES (?,?,?)",
                SEED_SYSTEM_CONFIG,
            )

    @contextmanager
    def cross_db_connection(self):
        """获取附加了 BOM 库的连接。BOM 表通过 bom.xxx 访问。"""
        if not self.other_db_path:
            raise RuntimeError("未配置 BOM 库路径，无法执行跨库查询")
        conn = sqlite3.connect(self.db_path)
        self._configure(conn)
        conn.execute(f"ATTACH DATABASE '{self.other_db_path}' AS bom")
        try:
            yield conn
        finally:
            try:
                conn.execute("DETACH DATABASE bom")
            except Exception:
                pass
            conn.close()

    @contextmanager
    def cross_db_transaction(self):
        """跨库事务（本地为物料库，附加 BOM 库）。"""
        with self.cross_db_connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_config(self, key: str, default: str = "") -> str:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_config(self, key: str, value: str, description: str = ""):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO system_config (key, value, description) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "description=excluded.description, updated_at=CURRENT_TIMESTAMP",
                (key, value, description),
            )


# ============================================================
# BOM 库
# ============================================================

class BOMDatabase(_BaseDB):
    """BOM 库：BOM、汇算、审计日志、导入日志。"""

    def __init__(self, db_path: str = "bom_db.db", material_db_path: str = ""):
        super().__init__(db_path, material_db_path)

    def initialize(self):
        """创建所有表、索引、触发器（幂等操作）。"""
        with self.get_connection() as conn:
            conn.executescript(BOM_SCHEMA_SQL)
            # 迁移：为已有数据库添加新列（幂等）
            for col_sql in [
                "ALTER TABLE bom_headers ADD COLUMN parent_bom_id INTEGER REFERENCES bom_headers(bom_id)",
                "ALTER TABLE bom_headers ADD COLUMN change_notes TEXT",
            ]:
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass
            conn.commit()

    @contextmanager
    def cross_db_connection(self):
        """获取附加了物料库的连接。物料表通过 mat.xxx 访问。"""
        if not self.other_db_path:
            raise RuntimeError("未配置物料库路径，无法执行跨库查询")
        conn = sqlite3.connect(self.db_path)
        self._configure(conn)
        conn.execute(f"ATTACH DATABASE '{self.other_db_path}' AS mat")
        try:
            yield conn
        finally:
            try:
                conn.execute("DETACH DATABASE mat")
            except Exception:
                pass
            conn.close()

    @contextmanager
    def cross_db_transaction(self):
        """跨库事务（本地为 BOM 库，附加物料库）。"""
        with self.cross_db_connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


# ============================================================
# 兼容旧版
# ============================================================

# 旧代码中 from database import DatabaseManager 仍可工作
DatabaseManager = MaterialDatabase
