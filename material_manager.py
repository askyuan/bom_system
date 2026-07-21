"""
物料库管理模块 (MaterialManager)
负责物料的增删改查、编码自动生成、导入校验、生命周期预警。
"""

import json
from typing import Optional, List, Dict, Any, Tuple

from database import MaterialDatabase, BOMDatabase
from unit_converter import UnitConverter, FootprintNormalizer
from audit import AuditLogger


class MaterialManager:
    """物料库管理器。

    material_db: MaterialDatabase（物料、分类、制造商、封装等）
    bom_db:      BOMDatabase（可选，用于 delete 检查引用、recode 级联更新、导入日志）
    """

    def __init__(self, material_db, bom_db=None, user_id: int = 1):
        self.db = material_db
        self.bom_db = bom_db
        self.user_id = user_id
        self.unit_converter = UnitConverter(material_db)
        self.footprint_normalizer = FootprintNormalizer(material_db)
        self.audit = AuditLogger(bom_db, users_db=material_db) if bom_db else AuditLogger(material_db)

    # ------------------------------------------------------------------
    # 编码生成
    # ------------------------------------------------------------------

    def _next_part_number(self, category_id: int, conn=None) -> str:
        """根据分类生成下一个物料编码，如 RES-00042。"""
        def _generate(c):
            prefix = c.execute(
                "SELECT code_prefix FROM categories WHERE id = ?", (category_id,)
            ).fetchone()[0]

            # 使用 SQL MAX 直接取最大序号
            row = c.execute(
                "SELECT MAX(CAST(SUBSTR(part_number, LENGTH(?) + 2) AS INTEGER)) FROM materials WHERE part_number LIKE ?",
                (f"{prefix}-", f"{prefix}-%"),
            ).fetchone()
            max_seq = row[0] if row[0] is not None else 0

            return f"{prefix}-{max_seq + 1:05d}"

        if conn is not None:
            return _generate(conn)
        with self.db.get_connection() as conn:
            return _generate(conn)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        manufacturer_id: Optional[int] = None,
        mpn: Optional[str] = None,
        description: str = "",
        category_id: int = 1,
        value: Optional[float] = None,
        unit: Optional[str] = None,
        footprint: Optional[str] = None,
        lifecycle_status: str = "Active",
        datasheet_url: Optional[str] = None,
        default_loss_rate: Optional[float] = None,
        moq: int = 1,
        spq: int = 1,
        stock_qty: int = 0,
        conn=None,
    ) -> str:
        """
        创建物料，自动生成编码。返回 part_number。
        如果传入 conn，则使用该连接执行所有操作（调用方负责事务管理）。
        """
        # 唯一性校验
        existing = self._find_by_mpn(manufacturer_id, mpn, conn=conn)
        if existing:
            raise ValueError(
                f"物料已存在: manufacturer_id={manufacturer_id}, mpn={mpn} → {existing}"
            )

        # 封装标准化
        std_footprint = self.footprint_normalizer.normalize(footprint)

        # 单位标准化
        std_value, std_unit = value, unit
        if value is not None and unit:
            result = self.unit_converter.convert(value, unit, self._get_category_code(category_id))
            if result:
                std_value, std_unit = result

        if conn is not None:
            # 使用传入的连接：编码生成和INSERT在同一连接/事务内
            for attempt in range(10):
                part_number = self._next_part_number(category_id, conn=conn)
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO materials (
                        part_number, manufacturer_id, mpn, description, category_id,
                        value, unit, footprint, lifecycle_status, datasheet_url,
                        default_loss_rate, moq, spq, stock_qty, created_by
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        part_number, manufacturer_id, mpn, description, category_id,
                        std_value, std_unit, std_footprint, lifecycle_status, datasheet_url,
                        default_loss_rate, moq, spq, stock_qty, self.user_id,
                    ),
                )
                if cursor.rowcount > 0:
                    break  # 插入成功
            else:
                raise RuntimeError(f"无法生成唯一物料编码，已重试10次 (category_id={category_id})")
        else:
            # 默认行为：使用独立事务
            for attempt in range(10):
                part_number = self._next_part_number(category_id)
                with self.db.transaction() as new_conn:
                    cursor = new_conn.execute(
                        """INSERT OR IGNORE INTO materials (
                            part_number, manufacturer_id, mpn, description, category_id,
                            value, unit, footprint, lifecycle_status, datasheet_url,
                            default_loss_rate, moq, spq, stock_qty, created_by
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            part_number, manufacturer_id, mpn, description, category_id,
                            std_value, std_unit, std_footprint, lifecycle_status, datasheet_url,
                            default_loss_rate, moq, spq, stock_qty, self.user_id,
                        ),
                    )
                    if cursor.rowcount > 0:
                        break  # 插入成功
            else:
                raise RuntimeError(f"无法生成唯一物料编码，已重试10次 (category_id={category_id})")

        self.audit.log(
            "material.create", self.user_id,
            target_type="material", target_id=part_number,
            detail={"mpn": mpn, "description": description},
            conn=conn,
        )
        return part_number

    def update(self, part_number: str, **fields) -> bool:
        """更新物料信息。支持任意字段更新，包括清空字段（设为 NULL）。"""
        allowed = {
            "manufacturer_id", "mpn", "description", "category_id",
            "value", "unit", "footprint", "lifecycle_status",
            "datasheet_url", "default_loss_rate", "moq", "spq", "stock_qty",
        }
        # 允许清空的字段：空字符串转为 None
        nullable = {
            "manufacturer_id", "mpn", "description", "footprint",
            "unit", "value", "datasheet_url",
        }
        updates = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in nullable and (v == "" or v is None):
                updates[k] = None
            elif v is not None:
                updates[k] = v
            # v is None 且 k 不在 nullable 中 → 跳过（如 moq/spq 等不应清空）

        if not updates:
            return False

        if "footprint" in updates and updates["footprint"] is not None:
            updates["footprint"] = self.footprint_normalizer.normalize(updates["footprint"])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [part_number]

        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE materials SET {set_clause} WHERE part_number = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise ValueError(f"物料不存在: {part_number}")

        # 生命周期变更预警
        if "lifecycle_status" in updates and updates["lifecycle_status"] in ("NRND", "EOL"):
            self._check_lifecycle_impact(part_number, updates["lifecycle_status"])

        self.audit.log(
            "material.update", self.user_id,
            target_type="material", target_id=part_number,
            detail=updates,
        )
        return True

    def recode(self, part_number: str) -> str:
        """
        重新生成物料编码：根据当前分类生成新的 part_number，
        并级联更新所有引用该编码的表（bom_items、calculation_items 等）。

        返回新的 part_number。
        """
        with self.db.get_connection() as conn:
            row = conn.execute(
                """SELECT m.category_id, c.code_prefix
                   FROM materials m
                   JOIN categories c ON m.category_id = c.id
                   WHERE m.part_number = ?""",
                (part_number,),
            ).fetchone()
            if not row:
                raise ValueError(f"物料不存在: {part_number}")

            category_id = row["category_id"]
            code_prefix = row["code_prefix"]

            # 如果编码已经匹配，无需操作
            if part_number.startswith(code_prefix + "-"):
                raise ValueError(
                    f"编码 {part_number} 已与分类前缀 {code_prefix} 匹配，无需重编码"
                )

        # 生成新编码
        new_pn = self._next_part_number(category_id)

        # 级联更新：使用跨库事务
        if self.bom_db:
            with self.db.cross_db_transaction() as conn:
                # 1. 更新 bom_items（BOM 库）
                conn.execute(
                    "UPDATE bom.bom_items SET part_number = ? WHERE part_number = ?",
                    (new_pn, part_number),
                )
                # 2. 更新 calculation_items（BOM 库）
                conn.execute(
                    "UPDATE bom.calculation_items SET part_number = ? WHERE part_number = ?",
                    (new_pn, part_number),
                )
                # 3. 更新 external_part_mapping（物料库，可选）
                try:
                    conn.execute(
                        "UPDATE external_part_mapping SET part_number = ? WHERE part_number = ?",
                        (new_pn, part_number),
                    )
                except Exception:
                    pass
                # 4. 更新 materials 主键（物料库）
                conn.execute(
                    "UPDATE materials SET part_number = ? WHERE part_number = ?",
                    (new_pn, part_number),
                )
        else:
            # 无 BOM 库：仅更新物料库
            with self.db.transaction() as conn:
                try:
                    conn.execute(
                        "UPDATE external_part_mapping SET part_number = ? WHERE part_number = ?",
                        (new_pn, part_number),
                    )
                except Exception:
                    pass
                conn.execute(
                    "UPDATE materials SET part_number = ? WHERE part_number = ?",
                    (new_pn, part_number),
                )

        self.audit.log(
            "material.recode", self.user_id,
            target_type="material", target_id=new_pn,
            detail={
                "old_part_number": part_number,
                "new_part_number": new_pn,
                "category_id": category_id,
                "code_prefix": code_prefix,
            },
        )
        return new_pn

    def list_mismatched(self) -> List[Dict]:
        """
        查找编码前缀与分类前缀不匹配的物料列表。
        用于批量重编码检查。
        """
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """SELECT m.part_number, m.mpn, m.description,
                          c.code_prefix AS expected_prefix,
                          c.name AS category_name
                   FROM materials m
                   JOIN categories c ON m.category_id = c.id
                   WHERE m.part_number NOT LIKE c.code_prefix || '-%'
                   ORDER BY m.part_number"""
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, part_number: str) -> bool:
        """删除物料（仅在无BOM引用时允许）。"""
        # 检查 BOM 引用（跨库查询）
        if self.bom_db:
            with self.bom_db.get_connection() as conn:
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM bom_items WHERE part_number = ?",
                    (part_number,),
                ).fetchone()[0]
                if ref_count > 0:
                    raise ValueError(
                        f"物料 {part_number} 被 {ref_count} 条BOM明细引用，无法删除"
                    )

        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM materials WHERE part_number = ?", (part_number,)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"物料不存在: {part_number}")

        self.audit.log(
            "material.delete", self.user_id,
            target_type="material", target_id=part_number,
        )
        return True

    def get(self, part_number: str) -> Optional[Dict]:
        """获取单个物料详情。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                """SELECT m.*, c.name AS category_name, c.code_prefix,
                          mf.name AS manufacturer_name
                   FROM materials m
                   LEFT JOIN categories c ON m.category_id = c.id
                   LEFT JOIN manufacturers mf ON m.manufacturer_id = mf.id
                   WHERE m.part_number = ?""",
                (part_number,),
            ).fetchone()
            return dict(row) if row else None

    def list(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        manufacturer: Optional[str] = None,
        footprint: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """分页查询物料列表。"""
        conditions = []
        params: list = []

        if category:
            conditions.append("c.code_prefix = ?")
            params.append(category)
        if status:
            conditions.append("m.lifecycle_status = ?")
            params.append(status)
        if manufacturer:
            conditions.append("mf.name = ?")
            params.append(manufacturer)
        if footprint:
            conditions.append("m.footprint = ?")
            params.append(footprint)
        if keyword:
            conditions.append(
                "(m.mpn LIKE ? OR m.description LIKE ? OR m.part_number LIKE ?)"
            )
            params.extend([f"%{keyword}%"] * 3)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"""SELECT m.*, c.name AS category_name, mf.name AS manufacturer_name
                FROM materials m
                LEFT JOIN categories c ON m.category_id = c.id
                LEFT JOIN manufacturers mf ON m.manufacturer_id = mf.id
                WHERE {where}
                ORDER BY m.part_number
                LIMIT ? OFFSET ?"""
        )
        params.extend([limit, offset])

        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def count(self, **filters) -> int:
        conditions = []
        params: list = []
        joins = ""
        if filters.get("category"):
            conditions.append("m.category_id IN (SELECT id FROM categories WHERE code_prefix = ?)")
            params.append(filters["category"])
        if filters.get("status"):
            conditions.append("m.lifecycle_status = ?")
            params.append(filters["status"])
        if filters.get("manufacturer"):
            joins += " LEFT JOIN manufacturers mf ON m.manufacturer_id = mf.id"
            conditions.append("mf.name = ?")
            params.append(filters["manufacturer"])
        if filters.get("footprint"):
            conditions.append("m.footprint = ?")
            params.append(filters["footprint"])
        where = " AND ".join(conditions) if conditions else "1=1"
        with self.db.get_connection() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM materials m{joins} WHERE {where}", params
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_materials(
        self,
        records: List[Dict[str, Any]],
        on_duplicate: str = "skip",
    ) -> Dict[str, Any]:
        """
        批量导入物料。

        records: 物料字典列表，每个至少包含 manufacturer_name, mpn, description, category
        on_duplicate: "skip" 或 "update"

        返回导入报告。
        """
        report = {
            "total": len(records),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [],
        }

        # 1. 预加载制造商和分类
        mfrs, cats = self._load_import_catalogs()

        # 2. 收集待创建的有效行（校验+解析，但不写入）
        to_create: List[Dict] = []
        for i, rec in enumerate(records, start=1):
            try:
                result = self._validate_import_row(rec, mfrs, cats, i, on_duplicate, report)
                if result is not None:
                    to_create.append(result)
            except Exception as e:
                report["errors"].append({"row": i, "message": str(e)})

        # 3. 批量创建物料（在同一事务中）
        if to_create:
            with self.db.transaction() as conn:
                for item in to_create:
                    for attempt in range(10):
                        part_number = self._next_part_number(item["category_id"], conn=conn)
                        cursor = conn.execute(
                            """INSERT OR IGNORE INTO materials (
                                part_number, manufacturer_id, mpn, description, category_id,
                                value, unit, footprint, lifecycle_status, datasheet_url,
                                default_loss_rate, moq, spq, stock_qty, created_by
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                part_number, item["manufacturer_id"], item["mpn"], item["description"],
                                item["category_id"], item["value"], item["unit"], item["footprint"],
                                item["lifecycle_status"], item["datasheet_url"], item["default_loss_rate"],
                                item["moq"], item["spq"], item["stock_qty"], self.user_id,
                            ),
                        )
                        if cursor.rowcount > 0:
                            break
                    else:
                        report["errors"].append({
                            "row": item.get("row_num", "?"),
                            "message": f"物料 {item['mpn']} 创建失败（编码冲突）",
                        })
                        continue
                    report["created"] += 1

        # 4. 写导入日志
        status = "success" if not report["errors"] else (
            "partial" if report["created"] > 0 or report["updated"] > 0 else "failed"
        )
        self._write_import_log(report, status)

        self.audit.log(
            "material.import", self.user_id,
            target_type="material",
            detail={"created": report["created"], "updated": report["updated"],
                    "skipped": report["skipped"], "errors": len(report["errors"])},
        )

        return report

    def _load_import_catalogs(self) -> tuple:
        """预加载制造商和分类映射表。"""
        with self.db.get_connection() as conn:
            mfrs = {
                r["name"].lower(): r["id"]
                for r in conn.execute("SELECT id, name FROM manufacturers").fetchall()
            }
            cats = {
                r["code_prefix"]: r["id"]
                for r in conn.execute("SELECT id, code_prefix FROM categories").fetchall()
            }
        return mfrs, cats

    def _validate_import_row(
        self, rec: Dict, mfrs: Dict, cats: Dict,
        row_num: int, on_duplicate: str, report: Dict,
    ) -> Optional[Dict]:
        """校验并解析一条导入记录，返回待创建字段字典或 None。"""
        mfr_name = rec.get("manufacturer_name", "").strip()
        mpn = rec.get("mpn", "").strip()
        desc = rec.get("description", "").strip()
        cat_code = rec.get("category", "").strip().upper()

        if not mfr_name:
            report["errors"].append({"row": row_num, "message": "manufacturer_name 为空"})
            return None
        if not mpn:
            report["errors"].append({"row": row_num, "message": "mpn 为空"})
            return None
        if not desc:
            report["errors"].append({"row": row_num, "message": "description 为空"})
            return None

        mfr_id = mfrs.get(mfr_name.lower())
        if mfr_id is None:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO manufacturers (name, created_by) VALUES (?,?)",
                    (mfr_name, self.user_id),
                )
                mfr_id = cursor.lastrowid
            mfrs[mfr_name.lower()] = mfr_id

        cat_id = cats.get(cat_code)
        if cat_id is None:
            report["errors"].append({"row": row_num, "message": f"分类 '{cat_code}' 不存在"})
            return None

        existing = self._find_by_mpn(mfr_id, mpn)
        if existing:
            if on_duplicate == "update":
                update_fields = {"description": desc}
                if rec.get("footprint"):
                    update_fields["footprint"] = rec["footprint"]
                if rec.get("lifecycle_status"):
                    update_fields["lifecycle_status"] = rec["lifecycle_status"]
                if rec.get("datasheet_url"):
                    update_fields["datasheet_url"] = rec["datasheet_url"]
                self.update(existing, **update_fields)
                report["updated"] += 1
            else:
                report["skipped"] += 1
            return None

        value, unit = None, None
        raw_value = rec.get("value")
        raw_unit = rec.get("unit")
        if raw_value is not None:
            try:
                value = float(raw_value)
                unit = raw_unit or ""
                result = self.unit_converter.convert(value, unit, cat_code)
                if result:
                    value, unit = result
            except (ValueError, TypeError):
                pass

        return {
            "row_num": row_num,
            "manufacturer_id": mfr_id,
            "mpn": mpn,
            "description": desc,
            "category_id": cat_id,
            "value": value,
            "unit": unit,
            "footprint": rec.get("footprint"),
            "lifecycle_status": rec.get("lifecycle_status", "Active"),
            "datasheet_url": rec.get("datasheet_url"),
            "default_loss_rate": rec.get("default_loss_rate"),
            "moq": int(rec.get("moq", 1)),
            "spq": int(rec.get("spq", 1)),
            "stock_qty": 0,
        }

    def _write_import_log(self, report: Dict, status: str):
        """将导入日志写入 BOM 库。"""
        if not self.bom_db:
            return
        with self.bom_db.transaction() as conn:
            conn.execute(
                """INSERT INTO import_logs
                   (file_name, file_type, total_rows, success_rows, failed_rows,
                    validation_report, status, created_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    "batch_import",
                    "material",
                    report["total"],
                    report["created"] + report["updated"],
                    len(report["errors"]) + report["skipped"],
                    json.dumps(report["errors"], ensure_ascii=False)[:4000],
                    status,
                    self.user_id,
                ),
            )
    def _check_lifecycle_impact(self, part_number: str, new_status: str):
        """检查物料状态变更对已发布BOM的影响。"""
        if not self.bom_db:
            return
        with self.bom_db.get_connection() as conn:
            affected = conn.execute(
                """SELECT DISTINCT bh.board_name, bh.version
                   FROM bom_items bi
                   JOIN bom_headers bh ON bi.bom_id = bh.bom_id
                   WHERE bi.part_number = ? AND bh.status = 'Released'""",
                (part_number,),
            ).fetchall()

        if affected:
            detail = [dict(r) for r in affected]
            self.audit.log(
                "material.update", self.user_id,
                target_type="material", target_id=part_number,
                detail={
                    "lifecycle_warning": True,
                    "new_status": new_status,
                    "affected_boms": detail,
                },
            )

    def get_lifecycle_warnings(self, part_numbers: List[str]) -> List[Dict]:
        """检查一组物料的生命周期状态，返回有风险的物料列表。"""
        if not part_numbers:
            return []
        placeholders = ",".join("?" * len(part_numbers))
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""SELECT part_number, mpn, lifecycle_status, description
                    FROM materials
                    WHERE part_number IN ({placeholders})
                    AND lifecycle_status IN ('NRND','EOL')""",
                part_numbers,
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 制造商管理
    # ------------------------------------------------------------------

    def _find_by_mpn(self, manufacturer_id: Optional[int], mpn: Optional[str], conn=None) -> Optional[str]:
        """根据 manufacturer_id + mpn 查找物料，返回 part_number 或 None。"""
        if not mpn:
            return None
        sql = "SELECT part_number FROM materials WHERE mpn = ?"
        params: list = [mpn]
        if manufacturer_id is not None:
            sql += " AND manufacturer_id = ?"
            params.append(manufacturer_id)
        else:
            sql += " AND manufacturer_id IS NULL"
        def _query(c):
            row = c.execute(sql, params).fetchone()
            return row[0] if row else None
        if conn is not None:
            return _query(conn)
        with self.db.get_connection() as c:
            return _query(c)

    def create_manufacturer(self, name: str, alias: str = "", website: str = "") -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO manufacturers (name, alias, website, created_by) VALUES (?,?,?,?)",
                (name, alias, website, self.user_id),
            )
            return cursor.lastrowid

    def list_manufacturers(self) -> List[Dict]:
        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM manufacturers ORDER BY name"
            ).fetchall()]

    # ------------------------------------------------------------------
    # 分类管理
    # ------------------------------------------------------------------

    def list_categories(self) -> List[Dict]:
        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT c.*, p.name AS parent_name
                   FROM categories c
                   LEFT JOIN categories p ON c.parent_id = p.id
                   ORDER BY c.code_prefix"""
            ).fetchall()]

