"""
BOM 管理模块 (BOMProcessor)
负责 BOM 导入（两阶段）、版本状态机、版本对比。
"""

import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from database import MaterialDatabase, BOMDatabase
from ref_designator import parse_designators, count_designators, format_designators
from audit import AuditLogger


class BOMImportValidator:
    """BOM 导入预校验器（阶段一）。"""

    REQUIRED_COLUMNS_MAP = {
        "part_number": ["物料编码", "part_number", "partnumber", "pn", "编码"],
        "quantity":    ["数量", "quantity", "qty", "用量", "单板用量"],
    }
    OPTIONAL_COLUMNS_MAP = {
        "mpn":        ["制造商型号", "mpn", "manufacturer_part_number", "型号"],
        "manufacturer":["制造商", "manufacturer", "mfr", "厂商", "厂家"],
        "description":["描述", "description", "desc", "物料描述"],
        "footprint":  ["封装", "footprint", "package"],
        "ref_des":    ["位号", "reference_designators", "ref_des", "reference", "位号列表"],
    }

    @staticmethod
    def _guess_category_code(mpn: str, description: str) -> str:
        """根据 MPN 和描述推断物料分类编码。"""
        text = f"{mpn} {description}".upper()
        rules = [
            (["RES", "电阻", "OHM", "Ω", "KΩ", "MΩ"], "RES"),
            (["CAP", "电容", "UF", "NF", "PF", "ΜF"], "CAP"),
            (["IND", "电感", "HENRY", "UH", "NH", "MH", "FERRITE", "磁珠"], "IND"),
            (["DIODE", "二极管", "LED", "ZENER", "SCHOTTKY", "TVS"], "DIO"),
            (["TRANSISTOR", "MOSFET", "BJT", "NPN", "PNP", "晶体管", "三极管"], "TRA"),
            (["IC", "芯片", "MCU", "FPGA", "DSP", "OPAMP", "REGULATOR",
              "LDO", "ADC", "DAC", "SN74", "STM", "ESP", "TPS", "LM",
              "逻辑", "运放", "驱动", "电源管理"], "ICS"),
            (["CONN", "连接器", "HEADER", "SOCKET", "USB", "JACK", "PLUG", "端子"], "CON"),
            (["SCREW", "螺母", "垫片", "散热", "HEATSINK", "外壳", "支架"], "MEC"),
        ]
        for keywords, code in rules:
            if any(kw in text for kw in keywords):
                return code
        return "MISC"

    def __init__(self, material_db):
        self.material_db = material_db

    def validate(self, df, column_mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        对 DataFrame 执行预校验，返回校验报告（不写入数据库）。
        column_mapping: 用户手动指定的列映射，格式 {"part_number": "文件列名", "quantity": "文件列名", ...}
                        为 None 时使用自动匹配。
        """
        report: Dict[str, Any] = {
            "total_rows": len(df),
            "errors": [],
            "warnings": [],
            "column_mapping": {},
            "valid_rows": [],
        }

        # 1. 列名匹配：优先使用用户手动映射
        if column_mapping:
            col_map = column_mapping
        else:
            col_map = self._map_columns(df.columns.tolist())
        report["column_mapping"] = col_map

        if "part_number" not in col_map and "mpn" not in col_map:
            report["errors"].append({
                "type": "column",
                "message": "未找到物料编码或MPN列。支持的列名: "
                           + ", ".join(self.REQUIRED_COLUMNS_MAP["part_number"])
            })
            return report

        if "quantity" not in col_map:
            report["errors"].append({
                "type": "column",
                "message": "未找到数量列。支持的列名: "
                           + ", ".join(self.REQUIRED_COLUMNS_MAP["quantity"])
            })
            return report

        # 2. 预加载物料库用于关联检查
        # 2. 收集文件中的物料编码和MPN，按需批查询（避免全表加载）
        file_pns: set = set()
        file_mpns: set = set()
        pn_col = col_map.get("part_number")
        mpn_col = col_map.get("mpn")
        for idx, row in df.iterrows():
            if pn_col:
                v = str(row.get(pn_col, "")).strip()
                if v and v not in ("nan", "None"):
                    file_pns.add(v)
            if mpn_col:
                v = str(row.get(mpn_col, "")).strip()
                if v and v not in ("nan", "None"):
                    file_mpns.add(v)

        materials: Dict[str, str] = {}
        mpn_index: Dict[str, tuple] = {}
        with self.material_db.get_connection() as conn:
            if file_pns:
                placeholders = ",".join("?" * len(file_pns))
                for r in conn.execute(
                    f"SELECT part_number, lifecycle_status FROM materials WHERE part_number IN ({placeholders})",
                    list(file_pns),
                ).fetchall():
                    materials[r["part_number"]] = r["lifecycle_status"]
            if file_mpns:
                placeholders = ",".join("?" * len(file_mpns))
                for r in conn.execute(
                    f"SELECT mpn, part_number, lifecycle_status FROM materials WHERE mpn IN ({placeholders})",
                    list(file_mpns),
                ).fetchall():
                    if r["mpn"]:
                        mpn_index[r["mpn"].upper()] = (r["part_number"], r["lifecycle_status"])

        # 3. 逐行校验
        seen_parts = {}
        for idx, row in df.iterrows():
            row_num = idx + 2  # Excel 行号（从1开始 + 表头）
            row_errors = []

            pn = str(row.get(col_map.get("part_number", ""), "")).strip() if col_map.get("part_number") else ""
            qty_raw = row.get(col_map.get("quantity", ""))
            mpn_val = str(row.get(col_map.get("mpn", ""), "")).strip() if col_map.get("mpn") else ""
            mfr_val = str(row.get(col_map.get("manufacturer", ""), "")).strip() if col_map.get("manufacturer") else ""
            desc_val = str(row.get(col_map.get("description", ""), "")).strip() if col_map.get("description") else ""
            fp_val = str(row.get(col_map.get("footprint", ""), "")).strip() if col_map.get("footprint") else ""
            ref_des = str(row.get(col_map.get("ref_des", ""), "")).strip() if col_map.get("ref_des") else ""

            # 清理 nan 值
            # 清理 nan 值（pandas 读取空单元格后可能返回 "nan" 字符串）
            mpn_val = mpn_val if mpn_val != "nan" else ""
            mfr_val = mfr_val if mfr_val != "nan" else ""
            desc_val = desc_val if desc_val != "nan" else ""
            fp_val = fp_val if fp_val != "nan" else ""
            ref_des = ref_des if ref_des != "nan" else ""

            # 数量校验
            try:
                qty = int(qty_raw)
                if qty <= 0:
                    row_errors.append(f"行{row_num}: 数量必须为正整数，当前值={qty_raw}")
            except (ValueError, TypeError):
                row_errors.append(f"行{row_num}: 数量格式错误，值='{qty_raw}'")
                qty = 0

            # 物料关联
            resolved_pn = None
            lifecycle = None
            auto_create = False

            if pn:
                if pn in materials:
                    resolved_pn = pn
                    lifecycle = materials[pn]
                elif mpn_val:
                    # 编码不在库中但有 MPN → 标记自动创建
                    upper_mpn = mpn_val.upper()
                    if upper_mpn in mpn_index:
                        resolved_pn, lifecycle = mpn_index[upper_mpn]
                    else:
                        auto_create = True
                        report["warnings"].append(
                            f"行{row_num}: 物料编码 '{pn}' 不存在，将自动创建"
                        )
                else:
                    auto_create = True
                    report["warnings"].append(
                        f"行{row_num}: 物料编码 '{pn}' 不存在且无 MPN，将自动创建"
                    )
            elif mpn_val:
                upper_mpn = mpn_val.upper()
                if upper_mpn in mpn_index:
                    resolved_pn, lifecycle = mpn_index[upper_mpn]
                else:
                    auto_create = True
                    report["warnings"].append(
                        f"行{row_num}: MPN '{mpn_val}' 不在库中，将自动创建物料"
                    )
            else:
                row_errors.append(f"行{row_num}: 物料编码和MPN均为空，无法处理")

            # 生命周期预警
            if lifecycle and lifecycle in ("NRND", "EOL"):
                report["warnings"].append(
                    f"行{row_num}: 物料 {resolved_pn} 状态为 {lifecycle}，不建议新设计使用"
                )

            # 重复检测
            if resolved_pn:
                if resolved_pn in seen_parts:
                    report["warnings"].append(
                        f"行{row_num}: 物料 {resolved_pn} 已在本BOM第{seen_parts[resolved_pn]}行出现，建议合并"
                    )
                else:
                    seen_parts[resolved_pn] = row_num

            if row_errors:
                report["errors"].extend(row_errors)
            elif (resolved_pn or auto_create) and qty > 0:
                ref_count = count_designators(ref_des) if ref_des else qty
                report["valid_rows"].append({
                    "row_num": row_num,
                    "part_number": resolved_pn or "",
                    "quantity": qty,
                    "reference_designators": ref_des,
                    "ref_count": ref_count,
                    "auto_create": auto_create,
                    "mpn": mpn_val,
                    "manufacturer": mfr_val,
                    "description": desc_val,
                    "footprint": fp_val,
                })

        return report

    def _map_columns(self, columns: List[str]) -> Dict[str, str]:
        """将文件中的列名映射到标准字段名。"""
        mapping = {}
        col_lower = {c.lower().strip(): c for c in columns}

        for field, aliases in {**self.REQUIRED_COLUMNS_MAP, **self.OPTIONAL_COLUMNS_MAP}.items():
            for alias in aliases:
                if alias.lower() in col_lower:
                    mapping[field] = col_lower[alias.lower()]
                    break
        return mapping


class BOMProcessor:
    """BOM 处理器：导入、版本管理、对比。

    bom_db:      BOMDatabase（BOM、汇算、审计等）
    material_db: MaterialDatabase（物料、分类、制造商等）
    """

    def __init__(self, bom_db, material_db=None, user_id: int = 1, material_manager=None):
        self.db = bom_db
        self.material_db = material_db
        self.user_id = user_id
        self.validator = BOMImportValidator(material_db) if material_db else None
        self.audit = AuditLogger(bom_db, users_db=material_db)
        self.mm = material_manager  # MaterialManager，用于导入时自动创建物料

    # ------------------------------------------------------------------
    # 导入（两阶段）
    # ------------------------------------------------------------------

    def validate_import(self, file_path: str, column_mapping: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        阶段一：预校验。读取文件并校验，返回报告（不写入数据库）。
        column_mapping: 用户手动指定的列映射，为 None 时使用自动匹配。
        """
        if not self.validator:
            raise ValueError("未配置物料库，无法执行 BOM 校验")
        df = self._read_file(file_path)
        report = self.validator.validate(df, column_mapping=column_mapping)
        report["file_name"] = os.path.basename(file_path)
        return report

    def read_file_info(self, file_path: str, sample_rows: int = 5) -> Dict[str, Any]:
        """
        读取文件头信息和前几行样本数据，用于前端列映射界面。
        返回 {"headers": [...], "samples": [[...], ...], "total_rows": N}
        """
        df = self._read_file(file_path)
        headers = df.columns.tolist()
        samples = []
        for i, (_, row) in enumerate(df.iterrows()):
            if i >= sample_rows:
                break
            samples.append([str(v) if v is not None and str(v) != "nan" else "" for v in row])

        # 自动匹配建议
        auto_mapping = self.validator._map_columns(headers)

        return {
            "headers": headers,
            "samples": samples,
            "total_rows": len(df),
            "auto_mapping": auto_mapping,
        }

    def confirm_import(
        self,
        file_path: str,
        board_name: str,
        version: str = "Rev1.0",
        notes: str = "",
        column_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        阶段二：确认入库。在事务内写入 BOM 头和明细。
        当 valid_row 中 auto_create=True 时，自动创建缺失物料（需要 self.mm）。
        """
        report = self.validate_import(file_path, column_mapping=column_mapping)
        if report["errors"]:
            critical = [e for e in report["errors"]
                        if isinstance(e, dict) and e.get("type") == "column"]
            if critical:
                raise ValueError(f"存在关键错误，无法导入: {critical}")

        valid_rows = report["valid_rows"]
        if not valid_rows:
            raise ValueError("无有效数据行可导入")

        # 自动创建缺失物料
        auto_created_count = 0
        auto_rows = [r for r in valid_rows if r.get("auto_create")]
        if auto_rows:
            if not self.mm:
                raise ValueError(
                    f"有 {len(auto_rows)} 行物料在库中不存在，但未配置自动创建。"
                    "请先在物料库中手动添加这些物料。"
                )
            auto_created_count = self._auto_create_materials(auto_rows)

        # 再次校验以确保所有 part_number 已解析
        for r in valid_rows:
            if not r.get("part_number"):
                raise ValueError(
                    f"行{r.get('row_num', '?')}: 物料自动创建失败，part_number 为空"
                )

        # 合并相同 part_number 的行（自动创建后可能出现重复）
        valid_rows = self._merge_duplicate_rows(valid_rows)

        with self.db.transaction() as conn:
            # 创建 BOM 头
            cursor = conn.execute(
                """INSERT INTO bom_headers
                   (board_name, version, status, notes, created_by)
                   VALUES (?,?, 'Draft', ?, ?)""",
                (board_name, version, notes, self.user_id),
            )
            bom_id = cursor.lastrowid

            # 批量插入明细
            conn.executemany(
                """INSERT INTO bom_items
                   (bom_id, part_number, quantity, reference_designators, ref_count, created_by)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (bom_id, r["part_number"], r["quantity"],
                     r["reference_designators"], r["ref_count"], self.user_id)
                    for r in valid_rows
                ],
            )

        # 写导入日志
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO import_logs
                   (file_name, file_type, target_bom_id, total_rows, success_rows,
                    failed_rows, validation_report, status, created_by)
                   VALUES (?,?,?,?,?,?,?, 'success', ?)""",
                (
                    report["file_name"], "bom", bom_id,
                    report["total_rows"], len(valid_rows),
                    len(report["errors"]),
                    json.dumps(report.get("warnings", [])[:20], ensure_ascii=False)[:4000],
                    self.user_id,
                ),
            )

        self.audit.log(
            "bom.import", self.user_id,
            target_type="bom", target_id=str(bom_id),
            detail={"board_name": board_name, "version": version,
                    "items": len(valid_rows), "auto_created": auto_created_count},
        )

        return {
            "bom_id": bom_id,
            "board_name": board_name,
            "version": version,
            "items_imported": len(valid_rows),
            "auto_created_materials": auto_created_count,
            "warnings": report.get("warnings", []),
        }


    @staticmethod
    def _merge_duplicate_rows(rows: List[Dict]) -> List[Dict]:
        """合并相同 part_number 的行（累加数量、位号）。"""
        merged: Dict[str, Dict] = {}
        for r in rows:
            pn = r["part_number"]
            if pn in merged:
                merged[pn]["quantity"] += r["quantity"]
                merged[pn]["ref_count"] += r.get("ref_count", r["quantity"])
                old_rd = merged[pn].get("reference_designators", "")
                new_rd = r.get("reference_designators", "")
                if old_rd and new_rd:
                    merged[pn]["reference_designators"] = f"{old_rd},{new_rd}"
                elif new_rd:
                    merged[pn]["reference_designators"] = new_rd
            else:
                merged[pn] = dict(r)
        return list(merged.values())

    def _auto_create_materials(self, auto_rows: List[Dict]) -> int:
        """
        为标记 auto_create=True 的行自动创建物料。
        使用物料库（material_db）创建制造商和物料，
        直接修改传入的 row 字典，将 part_number 填入新生成的编码。
        返回自动创建的物料数量。
        """
        if not self.material_db or not self.mm:
            raise ValueError("未配置物料库或物料管理器，无法自动创建物料")

        created = 0
        seen_mpns: Dict[str, str] = {}  # mpn_upper -> part_number

        # 1. 预加载制造商和分类索引（物料库）
        with self.material_db.get_connection() as conn:
            mfrs = {
                r["name"].lower(): r["id"]
                for r in conn.execute("SELECT id, name FROM manufacturers").fetchall()
            }
            cats = {
                r["code_prefix"]: r["id"]
                for r in conn.execute("SELECT id, code_prefix FROM categories").fetchall()
            }

        # 2. 在物料库事务中创建缺失的制造商
        with self.material_db.transaction() as conn:
            for r in auto_rows:
                mfr_name = r.get("manufacturer", "").strip()
                if mfr_name and mfr_name.lower() not in mfrs:
                    row = conn.execute(
                        "SELECT id FROM manufacturers WHERE LOWER(name) = ?",
                        (mfr_name.lower(),),
                    ).fetchone()
                    if row:
                        mfr_id = row[0]
                    else:
                        cursor = conn.execute(
                            "INSERT INTO manufacturers (name, created_by) VALUES (?,?)",
                            (mfr_name, 1),
                        )
                        mfr_id = cursor.lastrowid
                    mfrs[mfr_name.lower()] = mfr_id

        # 3. 逐个创建物料（mm.create 使用自身的物料库连接）
        for r in auto_rows:
            mpn = r.get("mpn", "").strip()
            mfr_name = r.get("manufacturer", "").strip()
            desc = r.get("description", "").strip()
            fp = r.get("footprint", "").strip()

            # 同一批次内相同 MPN 只创建一次
            if mpn and mpn.upper() in seen_mpns:
                r["part_number"] = seen_mpns[mpn.upper()]
                continue

            mfr_id = mfrs.get(mfr_name.lower()) if mfr_name else None
            if mfr_id is None:
                # 取第一个制造商作为默认
                with self.material_db.get_connection() as conn:
                    row = conn.execute("SELECT id FROM manufacturers LIMIT 1").fetchone()
                    if row:
                        mfr_id = row[0]
                    else:
                        with self.material_db.transaction() as tc:
                            cursor = tc.execute(
                                "INSERT INTO manufacturers (name, created_by) VALUES (?,?)",
                                ("Unknown", 1),
                            )
                            mfr_id = cursor.lastrowid
                        mfrs["unknown"] = mfr_id

            # 推断分类
            cat_code = self.validator._guess_category_code(mpn, desc)
            cat_id = cats.get(cat_code)
            if cat_id is None:
                cat_id = cats.get("MISC")
            if cat_id is None:
                with self.material_db.get_connection() as conn:
                    row = conn.execute("SELECT id FROM categories LIMIT 1").fetchone()
                    cat_id = row[0] if row else 1

            # 创建物料（mm.create 使用自身连接，无需传 conn）
            try:
                pn = self.mm.create(
                    manufacturer_id=mfr_id,
                    mpn=mpn or f"UNKNOWN-{r.get('row_num', 0)}",
                    description=desc or f"自动创建 (行{r.get('row_num', '?')})",
                    category_id=cat_id,
                    footprint=fp or None,
                )
                r["part_number"] = pn
                if mpn:
                    seen_mpns[mpn.upper()] = pn
                created += 1
            except ValueError:
                # 物料已存在（相同 manufacturer + MPN）
                existing = self.mm._find_by_mpn(mfr_id, mpn)
                if existing:
                    r["part_number"] = existing
                    if mpn:
                        seen_mpns[mpn.upper()] = existing

        return created

    def _read_file(self, file_path: str):
        import pandas as pd
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".xlsx", ".xls"):
            return pd.read_excel(file_path, dtype=str)
        elif ext == ".csv":
            # 尝试多种编码以兼容中文文件（Windows/Linux 均适用）
            for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin1"):
                try:
                    return pd.read_csv(file_path, dtype=str, encoding=enc)
                except (UnicodeDecodeError, UnicodeError):
                    continue
            raise ValueError("无法识别 CSV 文件编码，请转换为 UTF-8 后重试")
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    # ------------------------------------------------------------------
    # 版本状态机
    # ------------------------------------------------------------------

    def release(self, bom_id: int, approved_by: Optional[int] = None, notes: str = "") -> bool:
        """将 BOM 从 Draft 发布为 Released。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"BOM 不存在: {bom_id}")
            if row["status"] != "Draft":
                raise ValueError(f"BOM 当前状态为 {row['status']}，只有 Draft 状态可发布")

        approver = approved_by or self.user_id
        now = datetime.now().isoformat()

        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE bom_headers SET status='Released', release_date=?,
                   approved_by=?, approved_at=?, notes=COALESCE(NULLIF(?,''), notes)
                   WHERE bom_id=?""",
                (now, approver, now, notes, bom_id),
            )

        self.audit.log(
            "bom.release", self.user_id,
            target_type="bom", target_id=str(bom_id),
        )
        return True

    def obsolete(self, bom_id: int, reason: str = "") -> bool:
        """将 BOM 标记为 Obsolete。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"BOM 不存在: {bom_id}")
            if row["status"] != "Released":
                raise ValueError(f"BOM 当前状态为 {row['status']}，只有 Released 状态可废弃")

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE bom_headers SET status='Obsolete' WHERE bom_id=?",
                (bom_id,),
            )

        self.audit.log(
            "bom.obsolete", self.user_id,
            target_type="bom", target_id=str(bom_id),
            detail={"reason": reason},
        )
        return True

    def revise(self, bom_id: int, new_version: str) -> int:
        """基于已发布版本创建新的 Draft 修订版。返回新 bom_id。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM bom_headers WHERE bom_id = ?", (bom_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"BOM 不存在: {bom_id}")
            if row["status"] != "Released":
                raise ValueError("只能基于已发布版本创建修订版")

            items = conn.execute(
                "SELECT * FROM bom_items WHERE bom_id = ?", (bom_id,)
            ).fetchall()

        with self.db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO bom_headers
                   (board_name, version, status, notes, created_by)
                   VALUES (?,?, 'Draft', ?, ?)""",
                (row["board_name"], new_version,
                 f"基于 {row['version']} 修订", self.user_id),
            )
            new_bom_id = cursor.lastrowid

            if items:
                conn.executemany(
                    """INSERT INTO bom_items
                       (bom_id, part_number, quantity, reference_designators, ref_count, created_by)
                       VALUES (?,?,?,?,?,?)""",
                    [
                        (new_bom_id, it["part_number"], it["quantity"],
                         it["reference_designators"], it["ref_count"], self.user_id)
                        for it in items
                    ],
                )

        self.audit.log(
            "bom.update", self.user_id,
            target_type="bom", target_id=str(new_bom_id),
            detail={"revised_from": bom_id, "new_version": new_version},
        )
        return new_bom_id

    def get_latest_released(self, board_name: str) -> Optional[Dict]:
        """获取指定机型最新已发布版本的 BOM。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                """SELECT * FROM bom_headers
                   WHERE board_name = ? AND status = 'Released'
                   ORDER BY release_date DESC LIMIT 1""",
                (board_name,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_bom(self, bom_id: int) -> Optional[Dict]:
        with self.db.get_connection() as conn:
            header = conn.execute(
                "SELECT * FROM bom_headers WHERE bom_id = ?", (bom_id,)
            ).fetchone()
            if not header:
                return None

        with self.db.cross_db_connection() as conn:
            items = conn.execute(
                """SELECT bi.*, m.mpn, m.description, m.footprint, m.lifecycle_status,
                          mf.name AS manufacturer_name
                   FROM bom_items bi
                   LEFT JOIN mat.materials m ON bi.part_number = m.part_number
                   LEFT JOIN mat.manufacturers mf ON m.manufacturer_id = mf.id
                   WHERE bi.bom_id = ?
                   ORDER BY bi.part_number""",
                (bom_id,),
            ).fetchall()

            result = dict(header)
            result["items"] = [dict(it) for it in items]
            return result

    def list_boms(
        self,
        board_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        conditions = []
        params: list = []
        if board_name:
            conditions.append("board_name LIKE ?")
            params.append(f"%{board_name}%")
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        with self.db.cross_db_connection() as conn:
            rows = conn.execute(
                f"""SELECT bh.*, u.display_name AS creator_name
                    FROM bom_headers bh
                    LEFT JOIN mat.users u ON bh.created_by = u.id
                    WHERE {where}
                    ORDER BY bh.board_name, bh.version
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 版本对比 (Diff)
    # ------------------------------------------------------------------

    def compare(self, bom_id_a: int, bom_id_b: int) -> Dict[str, List[Dict]]:
        """
        对比两个 BOM 的差异。

        返回：
        {
            "added": [...],       # B 有 A 无
            "removed": [...],     # A 有 B 无
            "changed": [...],     # 数量变化
        }
        """
        with self.db.cross_db_connection() as conn:
            items_a = {
                r["part_number"]: dict(r)
                for r in conn.execute(
                    """SELECT bi.*, m.mpn, m.description
                       FROM bom_items bi
                       LEFT JOIN mat.materials m ON bi.part_number = m.part_number
                       WHERE bi.bom_id = ?""",
                    (bom_id_a,),
                ).fetchall()
            }
            items_b = {
                r["part_number"]: dict(r)
                for r in conn.execute(
                    """SELECT bi.*, m.mpn, m.description
                       FROM bom_items bi
                       LEFT JOIN mat.materials m ON bi.part_number = m.part_number
                       WHERE bi.bom_id = ?""",
                    (bom_id_b,),
                ).fetchall()
            }

        result = {"added": [], "removed": [], "changed": []}
        all_parts = set(items_a.keys()) | set(items_b.keys())

        for pn in sorted(all_parts):
            in_a = pn in items_a
            in_b = pn in items_b

            if in_b and not in_a:
                result["added"].append(items_b[pn])
            elif in_a and not in_b:
                result["removed"].append(items_a[pn])
            elif items_a[pn]["quantity"] != items_b[pn]["quantity"]:
                result["changed"].append({
                    "part_number": pn,
                    "mpn": items_a[pn].get("mpn"),
                    "description": items_a[pn].get("description"),
                    "qty_a": items_a[pn]["quantity"],
                    "qty_b": items_b[pn]["quantity"],
                    "diff": items_b[pn]["quantity"] - items_a[pn]["quantity"],
                })

        return result
