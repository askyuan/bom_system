"""
汇算引擎 (CalculationEngine)
核心功能：跨多 BOM 物料需求合并计算，含损耗率、MOQ、SPQ 计算。
支持异步执行和增量汇算。
"""

import json
import traceback
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, List, Dict, Any

from database import BOMDatabase, MaterialDatabase

# 线程池：共享后台线程，避免每次创建新线程
_calc_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="calc")

from ref_designator import format_designators
from audit import AuditLogger


class CalculationEngine:
    """物料汇算引擎。"""

    def __init__(self, bom_db, material_db=None, user_id: int = 1):
        self.db = bom_db
        self.material_db = material_db
        self.user_id = user_id
        self.audit = AuditLogger(bom_db, users_db=material_db)

    # ------------------------------------------------------------------
    # 汇算任务管理
    # ------------------------------------------------------------------

    def create_task(
        self,
        bom_quantities: List[Dict[str, Any]],
        async_run: bool = True,
    ) -> int:
        """
        创建汇算任务。

        bom_quantities: [{"bom_id": 1, "order_quantity": 100}, ...]
        async_run: 是否异步执行（True 时后台线程执行）

        返回 task_id。
        """
        if not bom_quantities:
            raise ValueError("至少需要选择一个 BOM")

        # 校验所有 BOM 状态
        with self.db.get_connection() as conn:
            for bq in bom_quantities:
                row = conn.execute(
                    "SELECT status, board_name, version FROM bom_headers WHERE bom_id = ?",
                    (bq["bom_id"],),
                ).fetchone()
                if not row:
                    raise ValueError(f"BOM 不存在: {bq['bom_id']}")
                if row["status"] != "Released":
                    raise ValueError(
                        f"BOM {row['board_name']} {row['version']} 状态为 {row['status']}，"
                        "只有 Released 状态的 BOM 可参与汇算"
                    )
                if bq["order_quantity"] <= 0:
                    raise ValueError(f"生产数量必须为正整数: {bq['order_quantity']}")

        # 创建任务记录
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO calculation_tasks (status, created_by) VALUES ('Pending', ?)",
                (self.user_id,),
            )
            task_id = cursor.lastrowid

            conn.executemany(
                "INSERT INTO calculation_boms (task_id, bom_id, order_quantity) VALUES (?,?,?)",
                [(task_id, bq["bom_id"], bq["order_quantity"]) for bq in bom_quantities],
            )

        self.audit.log(
            "calculation.create", self.user_id,
            target_type="calculation", target_id=str(task_id),
            detail={"boms": bom_quantities},
        )

        if async_run:
            _calc_executor.submit(self._run_task, task_id)
        else:
            self._run_task(task_id)

        return task_id

    def _run_task(self, task_id: int):
        """执行汇算计算（可在后台线程中运行）。"""
        start_time = time.time()

        # 更新状态为 Running
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE calculation_tasks SET status='Running', started_at=? WHERE task_id=?",
                (datetime.now().isoformat(), task_id),
            )

        try:
            self._execute_calculation(task_id)
            duration_ms = int((time.time() - start_time) * 1000)

            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE calculation_tasks
                       SET status='Completed', completed_at=?, duration_ms=?
                       WHERE task_id=?""",
                    (datetime.now().isoformat(), duration_ms, task_id),
                )

            self.audit.log(
                "calculation.run", self.user_id,
                target_type="calculation", target_id=str(task_id),
                detail={"status": "Completed", "duration_ms": duration_ms},
            )

        except Exception as e:
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE calculation_tasks
                       SET status='Failed', error_message=?, completed_at=?
                       WHERE task_id=?""",
                    (f"{e}\n{traceback.format_exc()}", datetime.now().isoformat(), task_id),
                )

            self.audit.log(
                "calculation.run", self.user_id,
                target_type="calculation", target_id=str(task_id),
                detail={"status": "Failed", "error": str(e)},
            )

    def _execute_calculation(self, task_id: int):
        """核心汇算逻辑。"""
        # 从 material_db 获取全局配置
        with self.material_db.get_connection() as mat_conn:
            global_loss_rate = float(
                mat_conn.execute(
                    "SELECT value FROM system_config WHERE key='default_loss_rate'"
                ).fetchone()[0]
            )
            max_len = int(
                mat_conn.execute(
                    "SELECT value FROM system_config WHERE key='ref_display_max_len'"
                ).fetchone()[0]
            )

        # 从 bom_db 获取任务关联的 BOM 及生产数量
        with self.db.get_connection() as conn:
            boms = conn.execute(
                "SELECT bom_id, order_quantity FROM calculation_boms WHERE task_id = ?",
                (task_id,),
            ).fetchall()

        # 聚合所有 BOM 的物料需求（跨库 JOIN）
        aggregated: Dict[str, Dict] = {}

        with self.db.cross_db_connection() as conn:
            for bom_row in boms:
                bom_id = bom_row["bom_id"]
                order_qty = bom_row["order_quantity"]

                # 获取 BOM 明细（跨库关联 materials / categories）
                items = conn.execute(
                    """SELECT bi.part_number, bi.quantity, bi.reference_designators,
                              m.default_loss_rate, m.moq, m.spq, m.stock_qty,
                              c.default_loss_rate AS cat_loss_rate,
                              m.mpn, m.description, m.footprint, m.lifecycle_status,
                              bh.board_name, bh.version
                       FROM bom_items bi
                       JOIN mat.materials m ON bi.part_number = m.part_number
                       LEFT JOIN mat.categories c ON m.category_id = c.id
                       JOIN bom_headers bh ON bi.bom_id = bh.bom_id
                       WHERE bi.bom_id = ?""",
                    (bom_id,),
                ).fetchall()

                for item in items:
                    pn = item["part_number"]
                    subtotal = item["quantity"] * order_qty

                    if pn not in aggregated:
                        aggregated[pn] = {
                            "part_number": pn,
                            "mpn": item["mpn"],
                            "description": item["description"],
                            "footprint": item["footprint"],
                            "lifecycle_status": item["lifecycle_status"],
                            "moq": item["moq"],
                            "spq": item["spq"],
                            "stock_qty": item["stock_qty"],
                            "loss_rate": item["default_loss_rate"]
                                if item["default_loss_rate"] is not None
                                else (item["cat_loss_rate"]
                                    if item["cat_loss_rate"] is not None
                                    else global_loss_rate),
                            "theoretical_qty": 0,
                            "sources": [],
                        }

                    aggregated[pn]["theoretical_qty"] += subtotal

                    # 位号合并显示
                    ref_display = ""
                    if item["reference_designators"]:
                        ref_display = format_designators(
                            item["reference_designators"], max_len
                        )

                    aggregated[pn]["sources"].append({
                        "bom_id": bom_id,
                        "board_name": item["board_name"],
                        "version": item["version"],
                        "unit_qty": item["quantity"],
                        "order_qty": order_qty,
                        "subtotal": subtotal,
                        "ref_designators": ref_display,
                    })

        # 计算最终采购量并写入明细
        with self.db.transaction() as conn:
            # 先清除旧明细（增量汇算场景）
            conn.execute(
                "DELETE FROM calculation_items WHERE task_id = ?", (task_id,)
            )

            for pn, data in aggregated.items():
                theo = data["theoretical_qty"]
                loss_rate = data["loss_rate"]
                moq = data["moq"] or 1
                spq = data["spq"] or 1
                stock = data["stock_qty"] or 0

                # 含损耗需求量
                loss_included = math.ceil(theo * (1 + loss_rate))

                # 建议采购量（扣减库存）
                suggested = max(loss_included - stock, 0)

                # 最终采购量（满足 SPQ）
                if spq > 1 and suggested > 0:
                    final = math.ceil(suggested / spq) * spq
                else:
                    final = suggested

                # 满足 MOQ
                if final > 0 and final < moq:
                    final = moq

                conn.execute(
                    """INSERT INTO calculation_items
                       (task_id, part_number, theoretical_qty, loss_rate,
                        loss_included_qty, stock_qty, suggested_qty, final_qty,
                        source_details)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, pn, theo, loss_rate,
                        loss_included, stock, suggested, final,
                        json.dumps(data["sources"], ensure_ascii=False),
                    ),
                )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_task(self, task_id: int) -> Optional[Dict]:
        """获取汇算任务状态及摘要。"""
        with self.db.get_connection() as conn:
            task = conn.execute(
                "SELECT * FROM calculation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not task:
                return None

            result = dict(task)

            # 关联的 BOM（均为 BOM 库表）
            boms = conn.execute(
                """SELECT cb.bom_id, cb.order_quantity,
                          bh.board_name, bh.version
                   FROM calculation_boms cb
                   JOIN bom_headers bh ON cb.bom_id = bh.bom_id
                   WHERE cb.task_id = ?""",
                (task_id,),
            ).fetchall()
            result["boms"] = [dict(b) for b in boms]

        # 统计（跨库关联 materials）
        if result["status"] == "Completed":
            with self.db.cross_db_connection() as xconn:
                stats = xconn.execute(
                    """SELECT COUNT(*) AS total_parts,
                              SUM(final_qty) AS total_purchase_qty,
                              SUM(CASE WHEN source_details LIKE '%NRND%' OR source_details LIKE '%EOL%'
                                  THEN 1 ELSE 0 END) AS warning_parts
                       FROM calculation_items ci
                       JOIN mat.materials m ON ci.part_number = m.part_number
                       WHERE ci.task_id = ?""",
                    (task_id,),
                ).fetchone()
                result["stats"] = dict(stats) if stats else {}

        return result

    def get_items(
        self,
        task_id: int,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict]:
        """获取汇算明细列表。"""
        with self.db.cross_db_connection() as conn:
            rows = conn.execute(
                """SELECT ci.*, m.mpn, m.description, m.footprint,
                          m.lifecycle_status, c.name AS category_name,
                          mf.name AS manufacturer_name
                   FROM calculation_items ci
                   JOIN mat.materials m ON ci.part_number = m.part_number
                   LEFT JOIN mat.categories c ON m.category_id = c.id
                   LEFT JOIN mat.manufacturers mf ON m.manufacturer_id = mf.id
                   WHERE ci.task_id = ?
                   ORDER BY ci.part_number
                   LIMIT ? OFFSET ?""",
                (task_id, limit, offset),
            ).fetchall()

            result = []
            for r in rows:
                item = dict(r)
                if item.get("source_details"):
                    try:
                        item["sources"] = json.loads(item["source_details"])
                    except json.JSONDecodeError:
                        item["sources"] = []
                result.append(item)
            return result

    def count_items(self, task_id: int) -> int:
        with self.db.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM calculation_items WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        conditions = []
        params: list = []
        if status:
            conditions.append("ct.status = ?")
            params.append(status)
        where = " AND ".join(conditions) if conditions else "1=1"

        with self.db.cross_db_connection() as conn:
            rows = conn.execute(
                f"""SELECT ct.*, u.display_name AS creator_name
                    FROM calculation_tasks ct
                    LEFT JOIN mat.users u ON ct.created_by = u.id
                    WHERE {where}
                    ORDER BY ct.created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 增量汇算（P2 阶段）
    # ------------------------------------------------------------------

    def incremental_add_bom(
        self,
        parent_task_id: int,
        bom_id: int,
        order_quantity: int,
    ) -> int:
        """
        在已有汇算基础上追加 BOM。
        创建新任务，复制原任务明细后加上新 BOM 的贡献。
        """
        with self.db.get_connection() as conn:
            parent = conn.execute(
                "SELECT status FROM calculation_tasks WHERE task_id = ?",
                (parent_task_id,),
            ).fetchone()
            if not parent or parent["status"] != "Completed":
                raise ValueError("父任务不存在或未完成")

            new_bom = conn.execute(
                "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
            ).fetchone()
            if not new_bom or new_bom["status"] != "Released":
                raise ValueError("BOM 不存在或未发布")

        # 复制原任务的 BOM 关联
        with self.db.get_connection() as conn:
            orig_boms = conn.execute(
                "SELECT bom_id, order_quantity FROM calculation_boms WHERE task_id = ?",
                (parent_task_id,),
            ).fetchall()

        all_boms = [dict(b) for b in orig_boms]
        all_boms.append({"bom_id": bom_id, "order_quantity": order_quantity})

        # 创建新任务
        new_task_id = self.create_task(all_boms, async_run=False)

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE calculation_tasks SET parent_task_id = ? WHERE task_id = ?",
                (parent_task_id, new_task_id),
            )

        return new_task_id

    def incremental_remove_bom(
        self,
        parent_task_id: int,
        bom_id_to_remove: int,
    ) -> int:
        """从已有汇算中移除某个 BOM，创建新任务。"""
        with self.db.get_connection() as conn:
            orig_boms = conn.execute(
                "SELECT bom_id, order_quantity FROM calculation_boms WHERE task_id = ?",
                (parent_task_id,),
            ).fetchall()

        remaining = [
            {"bom_id": b["bom_id"], "order_quantity": b["order_quantity"]}
            for b in orig_boms
            if b["bom_id"] != bom_id_to_remove
        ]

        if not remaining:
            raise ValueError("移除后无剩余 BOM")

        new_task_id = self.create_task(remaining, async_run=False)

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE calculation_tasks SET parent_task_id = ? WHERE task_id = ?",
                (parent_task_id, new_task_id),
            )

        return new_task_id

