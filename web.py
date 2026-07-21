"""
物料汇算系统 - Web 界面
Flask 后端，提供 REST API 和单页应用前端。
启动: python web.py [--port 5000] [--host 0.0.0.0]
"""

import os
import sys
import json
import argparse
import traceback
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    send_file, send_from_directory,
)

from database import MaterialDatabase, BOMDatabase
from material_manager import MaterialManager
from bom_processor import BOMProcessor
from calculation_engine import CalculationEngine
from report_exporter import ReportExporter
from audit import AuditLogger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_categories_cache: list = []  # 用于前端缓存分类列表

import re as _re

def _auto_increment_version(ver: str) -> str:
    """自动递增版本号：Rev1.0 → Rev1.1, V2.0 → V2.1, Rev2 → Rev3"""
    m = _re.match(r'^(.*?)(\d+)$', ver)
    if m:
        prefix, num = m.group(1), int(m.group(2))
        return f"{prefix}{num + 1}"
    return f"{ver}.1"

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db_dir: str = ".", db_path: str = "") -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit

    # 双库架构：物料库 + BOM 库
    if db_path:
        # 兼容旧版单文件路径
        material_db_path = db_path.replace("bom_system.db", "material_db.db") if "bom_system.db" in db_path else db_path.replace(".db", "_material.db")
        bom_db_path = db_path.replace("bom_system.db", "bom_db.db") if "bom_system.db" in db_path else db_path.replace(".db", "_bom.db")
    else:
        material_db_path = os.path.join(db_dir, "material_db.db")
        bom_db_path = os.path.join(db_dir, "bom_db.db")

    material_db = MaterialDatabase(material_db_path, bom_db_path=bom_db_path)
    bom_db = BOMDatabase(bom_db_path, material_db_path=material_db_path)
    material_db.initialize()
    bom_db.initialize()

    mm = MaterialManager(material_db, bom_db=bom_db)
    bom = BOMProcessor(bom_db, material_db=material_db, material_manager=mm)
    calc = CalculationEngine(bom_db, material_db=material_db)
    report = ReportExporter(bom_db, material_db=material_db)
    audit = AuditLogger(bom_db, users_db=material_db)

    UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
    EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    def ok(data=None, message="success"):
        return jsonify({"code": 200, "message": message, "data": data})

    def err(message, code=400):
        return jsonify({"code": code, "message": message}), code

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html")

    # ------------------------------------------------------------------
    # System / Stats
    # ------------------------------------------------------------------

    @app.route("/api/stats")
    def api_stats():
        with material_db.get_connection() as conn:
            mat_stats = {
                "materials": conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
                "manufacturers": conn.execute("SELECT COUNT(*) FROM manufacturers").fetchone()[0],
                "categories": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
                "nrnd_eol": conn.execute("SELECT COUNT(*) FROM materials WHERE lifecycle_status IN ('NRND','EOL')").fetchone()[0],
            }
        with bom_db.get_connection() as conn:
            bom_stats = {
                "boms_total": conn.execute("SELECT COUNT(*) FROM bom_headers").fetchone()[0],
                "boms_released": conn.execute("SELECT COUNT(*) FROM bom_headers WHERE status='Released'").fetchone()[0],
                "boms_draft": conn.execute("SELECT COUNT(*) FROM bom_headers WHERE status='Draft'").fetchone()[0],
                "calc_tasks": conn.execute("SELECT COUNT(*) FROM calculation_tasks").fetchone()[0],
                "calc_completed": conn.execute("SELECT COUNT(*) FROM calculation_tasks WHERE status='Completed'").fetchone()[0],
            }
        s = {**mat_stats, **bom_stats}
        return ok(s)

    @app.route("/api/backup", methods=["POST"])
    def api_backup():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(os.path.dirname(__file__), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        mat_path = os.path.join(backup_dir, f"material_db_{timestamp}.db")
        bom_path = os.path.join(backup_dir, f"bom_db_{timestamp}.db")
        material_db.backup(mat_path)
        bom_db.backup(bom_path)
        audit.log("system.backup", 1, target_type="system", detail={"material_path": mat_path, "bom_path": bom_path})
        return ok({"material_path": mat_path, "bom_path": bom_path})

    @app.route("/api/audit-logs")
    def api_audit_logs():
        logs = audit.query(
            action=request.args.get("action"),
            limit=int(request.args.get("limit", 50)),
            offset=int(request.args.get("offset", 0)),
        )
        return ok(logs)

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    @app.route("/api/materials")
    def api_materials_list():
        filters = dict(
            category=request.args.get("category"),
            status=request.args.get("status"),
            keyword=request.args.get("keyword"),
            manufacturer=request.args.get("manufacturer"),
            footprint=request.args.get("footprint"),
        )
        items = mm.list(
            **{k: v for k, v in filters.items() if v},
            limit=int(request.args.get("limit", 50)),
            offset=int(request.args.get("offset", 0)),
        )
        total = mm.count(**{k: v for k, v in filters.items() if v})
        return ok({"items": items, "total": total})

    @app.route("/api/materials/<pn>")
    def api_material_detail(pn):
        m = mm.get(pn)
        if not m:
            return err("物料不存在", 404)
        return ok(m)

    @app.route("/api/materials", methods=["POST"])
    def api_material_create():
        d = request.json
        try:
            mfrs = mm.list_manufacturers()
            mfr = next((m for m in mfrs if m["name"].lower() == d["manufacturer"].lower()), None)
            if not mfr:
                mfr_id = mm.create_manufacturer(d["manufacturer"])
            else:
                mfr_id = mfr["id"]

            cats = mm.list_categories()
            cat = next((c for c in cats if c["code_prefix"] == d["category"].upper()), None)
            if not cat:
                return err(f"分类不存在: {d['category']}")

            pn = mm.create(
                manufacturer_id=mfr_id,
                mpn=d["mpn"],
                description=d["description"],
                category_id=cat["id"],
                footprint=d.get("footprint"),
                lifecycle_status=d.get("lifecycle_status", "Active"),
                moq=int(d.get("moq", 1)),
                spq=int(d.get("spq", 1)),
            )
            return ok({"part_number": pn}, f"物料创建成功: {pn}")
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/<pn>", methods=["PUT"])
    def api_material_update(pn):
        d = request.json
        try:
            # 制造商名称转 ID
            if "manufacturer" in d:
                mfr_name = d.pop("manufacturer").strip()
                if mfr_name:
                    mfrs = mm.list_manufacturers()
                    mfr = next((m for m in mfrs if m["name"].lower() == mfr_name.lower()), None)
                    if mfr:
                        d["manufacturer_id"] = mfr["id"]
                    else:
                        # 制造商不存在，自动创建
                        mfr_id = mm.create_manufacturer(mfr_name)
                        d["manufacturer_id"] = mfr_id
                else:
                    # 制造商名称为空，清空制造商关联
                    d["manufacturer_id"] = None
            mm.update(pn, **d)
            return ok(message="更新成功")
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/<pn>", methods=["DELETE"])
    def api_material_delete(pn):
        try:
            mm.delete(pn)
            return ok(message="删除成功")
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/<pn>/recode", methods=["POST"])
    def api_material_recode(pn):
        """重新生成物料编码：根据当前分类生成新编码，级联更新所有 BOM 引用"""
        try:
            new_pn = mm.recode(pn)
            return ok(
                {"old_part_number": pn, "new_part_number": new_pn},
                f"编码已更新: {pn} → {new_pn}",
            )
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/mismatched")
    def api_materials_mismatched():
        """查找编码前缀与分类不匹配的物料"""
        items = mm.list_mismatched()
        return ok(items)

    @app.route("/api/materials/by-description")
    def api_materials_by_description():
        """按描述关键词查询物料，用于 BOM 编辑时按描述选择替代型号"""
        desc = request.args.get("desc", "").strip()
        if not desc:
            return err("请提供描述参数 desc")
        with material_db.get_connection() as conn:
            rows = conn.execute(
                """SELECT m.part_number, m.mpn, m.description, m.footprint,
                          m.lifecycle_status, mf.name AS manufacturer_name
                   FROM materials m
                   LEFT JOIN manufacturers mf ON m.manufacturer_id = mf.id
                   WHERE m.description = ?
                   ORDER BY m.mpn""",
                (desc,),
            ).fetchall()
        return ok([dict(r) for r in rows])

    @app.route("/api/materials/import", methods=["POST"])
    def api_material_import():
        f = request.files.get("file")
        if not f:
            return err("请上传文件")
        try:
            import pandas as pd
            path = os.path.join(UPLOAD_DIR, f.filename)
            f.save(path)
            ext = os.path.splitext(path)[1].lower()
            if ext in (".xlsx", ".xls"):
                df = pd.read_excel(path, dtype=str)
            else:
                df = pd.read_csv(path, dtype=str)
            records = df.fillna("").to_dict("records")
            result = mm.import_materials(records, on_duplicate=request.form.get("on_duplicate", "skip"))
            return ok(result)
        except ImportError:
            return err("material import 需要 pandas 库，请在服务器上运行 setup.sh 安装依赖")
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/export")
    def api_material_export():
        fmt = request.args.get("fmt", "xlsx")
        ext = ".csv" if fmt == "csv" else ".xlsx"
        path = os.path.join(EXPORT_DIR, f"materials_{datetime.now():%Y%m%d%H%M%S}{ext}")
        report.export_materials(
            path,
            category=request.args.get("category"),
            status=request.args.get("status"),
            fmt=fmt,
        )
        return send_file(path, as_attachment=True)

    # ------------------------------------------------------------------
    # Manufacturers / Categories
    # ------------------------------------------------------------------

    @app.route("/api/manufacturers")
    def api_manufacturers():
        return ok(mm.list_manufacturers())

    @app.route("/api/manufacturers", methods=["POST"])
    def api_manufacturer_create():
        d = request.json
        try:
            mid = mm.create_manufacturer(d["name"], d.get("alias", ""), d.get("website", ""))
            return ok({"id": mid})
        except Exception as e:
            return err(str(e))

    @app.route("/api/categories")
    def api_categories():
        """返回扁平列表（兼容旧接口），包含 parent_id 信息"""
        return ok(mm.list_categories())

    @app.route("/api/categories/tree")
    def api_categories_tree():
        """返回二级树形结构：[{id, name, code_prefix, children: [...]}]"""
        with material_db.get_connection() as conn:
            all_cats = conn.execute(
                """SELECT c.id, c.name, c.code_prefix, c.parent_id, c.default_loss_rate,
                          p.name AS parent_name, p.code_prefix AS parent_code_prefix
                   FROM categories c
                   LEFT JOIN categories p ON c.parent_id = p.id
                   ORDER BY c.code_prefix"""
            ).fetchall()

        # 构建树
        parents = []
        children_map = {}
        for r in all_cats:
            r = dict(r)
            if r["parent_id"] is None:
                r["children"] = []
                parents.append(r)
            else:
                children_map.setdefault(r["parent_id"], []).append(r)
        for p in parents:
            p["children"] = children_map.get(p["id"], [])
        return ok(parents)

    @app.route("/api/categories", methods=["POST"])
    def api_category_create():
        d = request.json
        try:
            name = d.get("name", "").strip()
            code_prefix = d.get("code_prefix", "").strip().upper()
            parent_id = d.get("parent_id")
            loss_rate = float(d.get("default_loss_rate", 0.03))
            if not name or not code_prefix:
                return err("分类名称和前缀不能为空")
            with material_db.transaction() as conn:
                existing = conn.execute(
                    "SELECT id FROM categories WHERE code_prefix = ?", (code_prefix,)
                ).fetchone()
                if existing:
                    return err(f"前缀 '{code_prefix}' 已存在")
                cursor = conn.execute(
                    "INSERT INTO categories (name, code_prefix, parent_id, default_loss_rate, created_by) VALUES (?,?,?,?,?)",
                    (name, code_prefix, parent_id, loss_rate, 1),
                )
                cat_id = cursor.lastrowid
            audit.log("category.create", 1, target_type="category", target_id=str(cat_id),
                      detail={"name": name, "code_prefix": code_prefix, "parent_id": parent_id})
            _categories_cache.clear()
            return ok({"id": cat_id, "code_prefix": code_prefix}, f"分类 '{name}' 创建成功")
        except Exception as e:
            return err(str(e))

    @app.route("/api/categories/<int:cat_id>", methods=["PUT"])
    def api_category_update(cat_id):
        d = request.json
        try:
            with material_db.transaction() as conn:
                cat = conn.execute(
                    "SELECT * FROM categories WHERE id = ?", (cat_id,)
                ).fetchone()
                if not cat:
                    return err("分类不存在", 404)
                fields, values = [], []
                if "name" in d:
                    fields.append("name = ?"); values.append(d["name"])
                if "code_prefix" in d:
                    new_prefix = d["code_prefix"].strip().upper()
                    dup = conn.execute(
                        "SELECT id FROM categories WHERE code_prefix = ? AND id != ?",
                        (new_prefix, cat_id),
                    ).fetchone()
                    if dup:
                        return err(f"前缀 '{new_prefix}' 已被使用")
                    fields.append("code_prefix = ?"); values.append(new_prefix)
                if "default_loss_rate" in d:
                    fields.append("default_loss_rate = ?"); values.append(float(d["default_loss_rate"]))
                if "parent_id" in d:
                    fields.append("parent_id = ?"); values.append(d["parent_id"])
                if not fields:
                    return err("无修改内容")
                values.append(cat_id)
                conn.execute(
                    f"UPDATE categories SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
            _categories_cache.clear()
            audit.log("category.update", 1, target_type="category", target_id=str(cat_id), detail=d)
            return ok(message="分类已更新")
        except Exception as e:
            return err(str(e))

    @app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
    def api_category_delete(cat_id):
        try:
            with material_db.transaction() as conn:
                cat = conn.execute(
                    "SELECT * FROM categories WHERE id = ?", (cat_id,)
                ).fetchone()
                if not cat:
                    return err("分类不存在", 404)
                # 检查是否有物料引用
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM materials WHERE category_id = ?", (cat_id,)
                ).fetchone()[0]
                if ref_count > 0:
                    return err(f"分类被 {ref_count} 个物料引用，无法删除")
                # 检查是否有子分类
                child_count = conn.execute(
                    "SELECT COUNT(*) FROM categories WHERE parent_id = ?", (cat_id,)
                ).fetchone()[0]
                if child_count > 0:
                    return err(f"分类下有 {child_count} 个子分类，请先删除子分类")
                conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            _categories_cache.clear()
            audit.log("category.delete", 1, target_type="category", target_id=str(cat_id),
                      detail={"name": cat["name"]})
            return ok(message="分类已删除")
        except Exception as e:
            return err(str(e))

    # ------------------------------------------------------------------
    # Footprints
    # ------------------------------------------------------------------

    @app.route("/api/footprints")
    def api_footprints_list():
        """返回封装列表：合并注册表和物料库实际使用情况"""
        with material_db.get_connection() as conn:
            # 1. 注册表中的封装
            registered = conn.execute(
                "SELECT * FROM footprints ORDER BY name"
            ).fetchall()

            # 2. 物料库中实际使用的封装（聚合统计）
            usage = conn.execute(
                """SELECT footprint, COUNT(*) AS material_count
                   FROM materials
                   WHERE footprint IS NOT NULL AND footprint != ''
                   GROUP BY footprint
                   ORDER BY footprint"""
            ).fetchall()

        # 3. 合并：以注册表为基础，附加使用统计
        usage_map = {r["footprint"]: r["material_count"] for r in usage}
        registered_names = set()

        result = []
        for fp in registered:
            fp = dict(fp)
            name = fp["name"]
            registered_names.add(name)
            fp["material_count"] = usage_map.get(name, 0)
            fp["registered"] = True
            result.append(fp)

        # 4. 补充物料库中存在但未注册的封装
        for fp_name, count in usage_map.items():
            if fp_name not in registered_names:
                result.append({
                    "id": None,
                    "name": fp_name,
                    "description": "",
                    "material_count": count,
                    "registered": False,
                    "created_at": None,
                    "updated_at": None,
                })

        # 按名称排序
        result.sort(key=lambda x: x["name"])
        return ok(result)

    @app.route("/api/footprints", methods=["POST"])
    def api_footprint_create():
        d = request.json
        try:
            name = d.get("name", "").strip()
            description = d.get("description", "").strip()
            if not name:
                return err("封装名称不能为空")
            with material_db.transaction() as conn:
                existing = conn.execute(
                    "SELECT id FROM footprints WHERE name = ?", (name,)
                ).fetchone()
                if existing:
                    return err(f"封装 '{name}' 已存在")
                cursor = conn.execute(
                    "INSERT INTO footprints (name, description, created_by) VALUES (?,?,?)",
                    (name, description, 1),
                )
                fp_id = cursor.lastrowid
            audit.log("footprint.create", 1, target_type="footprint", target_id=str(fp_id),
                      detail={"name": name})
            return ok({"id": fp_id, "name": name}, f"封装 '{name}' 创建成功")
        except Exception as e:
            return err(str(e))

    @app.route("/api/footprints/<int:fp_id>", methods=["PUT"])
    def api_footprint_update(fp_id):
        d = request.json
        try:
            with material_db.transaction() as conn:
                fp = conn.execute(
                    "SELECT * FROM footprints WHERE id = ?", (fp_id,)
                ).fetchone()
                if not fp:
                    return err("封装不存在", 404)
                fields, values = [], []
                if "name" in d:
                    new_name = d["name"].strip()
                    dup = conn.execute(
                        "SELECT id FROM footprints WHERE name = ? AND id != ?",
                        (new_name, fp_id),
                    ).fetchone()
                    if dup:
                        return err(f"封装名称 '{new_name}' 已被使用")
                    fields.append("name = ?"); values.append(new_name)
                if "description" in d:
                    fields.append("description = ?"); values.append(d["description"])
                if not fields:
                    return err("无修改内容")
                fields.append("updated_at = CURRENT_TIMESTAMP")
                values.append(fp_id)
                conn.execute(
                    f"UPDATE footprints SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
            audit.log("footprint.update", 1, target_type="footprint", target_id=str(fp_id), detail=d)
            return ok(message="封装已更新")
        except Exception as e:
            return err(str(e))

    @app.route("/api/footprints/<int:fp_id>", methods=["DELETE"])
    def api_footprint_delete(fp_id):
        try:
            with material_db.transaction() as conn:
                fp = conn.execute(
                    "SELECT * FROM footprints WHERE id = ?", (fp_id,)
                ).fetchone()
                if not fp:
                    return err("封装不存在", 404)
                # 检查是否有物料在使用
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM materials WHERE footprint = ?", (fp["name"],)
                ).fetchone()[0]
                if ref_count > 0:
                    return err(f"封装 '{fp['name']}' 被 {ref_count} 个物料引用，无法删除。请先修改相关物料的封装字段。")
                conn.execute("DELETE FROM footprints WHERE id = ?", (fp_id,))
            audit.log("footprint.delete", 1, target_type="footprint", target_id=str(fp_id),
                      detail={"name": fp["name"]})
            return ok(message="封装已删除")
        except Exception as e:
            return err(str(e))

    @app.route("/api/footprints/batch-register", methods=["POST"])
    def api_footprints_batch_register():
        """将物料库中使用但未注册的封装批量添加到注册表"""
        try:
            with material_db.transaction() as conn:
                # 查找物料库中使用但未注册的封装
                unregistered = conn.execute(
                    """SELECT DISTINCT m.footprint
                       FROM materials m
                       WHERE m.footprint IS NOT NULL AND m.footprint != ''
                         AND m.footprint NOT IN (SELECT name FROM footprints)"""
                ).fetchall()
                count = 0
                for row in unregistered:
                    conn.execute(
                        "INSERT INTO footprints (name, description, created_by) VALUES (?,?,?)",
                        (row["footprint"], "", 1),
                    )
                    count += 1
            if count:
                audit.log("footprint.batch_register", 1, target_type="footprint",
                          detail={"count": count})
            return ok({"registered": count}, f"已注册 {count} 个封装")
        except Exception as e:
            return err(str(e))

    # ------------------------------------------------------------------
    # BOM
    # ------------------------------------------------------------------

    @app.route("/api/boms")
    def api_boms_list():
        items = bom.list_boms(
            board_name=request.args.get("board_name"),
            status=request.args.get("status"),
            limit=int(request.args.get("limit", 100)),
            offset=int(request.args.get("offset", 0)),
        )
        return ok(items)

    @app.route("/api/boms/grouped")
    def api_boms_grouped():
        """按板卡名称分组返回 BOM，支持板卡级分页和状态筛选"""
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 10))
        status = request.args.get("status", "")
        keyword = request.args.get("keyword", "").strip()

        with bom_db.cross_db_connection() as conn:
            # 构建筛选条件
            conditions = []
            params = []
            if status:
                conditions.append("status = ?")
                params.append(status)
            if keyword:
                conditions.append("board_name LIKE ?")
                params.append(f"%{keyword}%")
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

            # 统计去重的板卡数量
            count_sql = f"SELECT COUNT(DISTINCT board_name) FROM bom_headers{where}"
            total_boards = conn.execute(count_sql, params).fetchone()[0]

            # 分页获取板卡名称列表
            board_sql = f"""SELECT DISTINCT board_name FROM bom_headers{where}
                            ORDER BY board_name LIMIT ? OFFSET ?"""
            boards = conn.execute(
                board_sql, params + [page_size, (page - 1) * page_size]
            ).fetchall()

            # 为每个板卡获取所有版本
            result = []
            for b in boards:
                bn = b["board_name"]
                # 如果有筛选条件，bom 级别也要过滤
                bom_cond = "WHERE board_name = ?"
                bom_params = [bn]
                if status:
                    bom_cond += " AND status = ?"
                    bom_params.append(status)

                boms = conn.execute(
                    f"""SELECT bh.*, u.display_name AS creator_name,
                               (SELECT COUNT(*) FROM bom_items WHERE bom_id = bh.bom_id) AS item_count
                        FROM bom_headers bh
                        LEFT JOIN mat.users u ON bh.created_by = u.id
                        {bom_cond}
                        ORDER BY bh.created_at DESC""",
                    bom_params,
                ).fetchall()
                result.append({
                    "board_name": bn,
                    "boms": [dict(bom) for bom in boms],
                })

        return ok({
            "boards": result,
            "total_boards": total_boards,
            "page": page,
            "page_size": page_size,
            "total_pages": (total_boards + page_size - 1) // page_size if page_size else 1,
        })

    @app.route("/api/boms/<int:bom_id>")
    def api_bom_detail(bom_id):
        b = bom.get_bom(bom_id)
        if not b:
            return err("BOM 不存在", 404)
        return ok(b)

    # ---------- BOM 导入：3 步流程 ----------

    @app.route("/api/boms/upload", methods=["POST"])
    def api_bom_upload():
        """步骤1：上传文件，返回列头和样本数据"""
        f = request.files.get("file")
        if not f:
            return err("请上传文件")
        try:
            import uuid
            token = uuid.uuid4().hex[:12]
            ext = os.path.splitext(f.filename)[1].lower()
            safe_name = f"{token}{ext}"
            path = os.path.join(UPLOAD_DIR, safe_name)
            f.save(path)

            info = bom.read_file_info(path, sample_rows=5)
            info["file_token"] = token
            info["file_name"] = f.filename
            info["file_path"] = safe_name
            return ok(info)
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/validate", methods=["POST"])
    def api_bom_validate():
        """步骤2：使用用户指定的列映射进行校验"""
        d = request.json
        file_path = d.get("file_path", "")
        column_mapping = d.get("column_mapping", {})
        if not file_path:
            return err("缺少 file_path")
        try:
            full_path = os.path.join(UPLOAD_DIR, file_path)
            if not os.path.exists(full_path):
                return err("文件不存在或已过期，请重新上传")

            mapping = {k: v for k, v in column_mapping.items() if v}  # 过滤空值
            report = bom.validate_import(full_path, column_mapping=mapping if mapping else None)
            return ok(report)
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/confirm-import", methods=["POST"])
    def api_bom_confirm_import():
        """步骤3：确认导入"""
        d = request.json
        file_path = d.get("file_path", "")
        board_name = d.get("board_name", "")
        version = d.get("version", "Rev1.0")
        notes = d.get("notes", "")
        column_mapping = d.get("column_mapping", {})
        if not file_path or not board_name:
            return err("缺少 file_path 或 board_name")
        try:
            full_path = os.path.join(UPLOAD_DIR, file_path)
            if not os.path.exists(full_path):
                return err("文件不存在或已过期，请重新上传")

            mapping = {k: v for k, v in column_mapping.items() if v}
            result = bom.confirm_import(
                full_path, board_name, version, notes,
                column_mapping=mapping if mapping else None,
            )
            return ok(result)
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/import", methods=["POST"])
    def api_bom_import():
        """兼容旧版单步导入（自动匹配模式）"""
        f = request.files.get("file")
        if not f:
            return err("请上传文件")
        try:
            path = os.path.join(UPLOAD_DIR, f.filename)
            f.save(path)
            board_name = request.form.get("board_name", "")
            version = request.form.get("version", "Rev1.0")
            notes = request.form.get("notes", "")

            vreport = bom.validate_import(path)
            if not vreport["valid_rows"]:
                return ok({"phase": "validate", "report": vreport, "message": "无有效数据行"})

            critical = [e for e in vreport["errors"] if isinstance(e, dict) and e.get("type") == "column"]
            if critical:
                return ok({"phase": "validate", "report": vreport, "message": "存在关键列名错误"})

            result = bom.confirm_import(path, board_name, version, notes)
            result["phase"] = "imported"
            result["report"] = vreport
            return ok(result)
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/release", methods=["POST"])
    def api_bom_release(bom_id):
        try:
            notes = request.json.get("notes", "") if request.json else ""
            bom.release(bom_id, notes=notes)
            return ok(message="BOM 已发布")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/obsolete", methods=["POST"])
    def api_bom_obsolete(bom_id):
        try:
            reason = request.json.get("reason", "") if request.json else ""
            bom.obsolete(bom_id, reason=reason)
            return ok(message="BOM 已废弃")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/compare")
    def api_bom_compare():
        a = int(request.args.get("a", 0))
        b = int(request.args.get("b", 0))
        if not a or not b:
            return err("请提供参数 a 和 b (BOM ID)")
        diff = bom.compare(a, b)
        return ok(diff)

    @app.route("/api/boms/<int:bom_id>/export")
    def api_bom_export(bom_id):
        fmt = request.args.get("fmt", "xlsx")
        ext = ".csv" if fmt == "csv" else ".xlsx"
        b = bom.get_bom(bom_id)
        name = b["board_name"].replace(" ", "_") if b else f"bom_{bom_id}"
        path = os.path.join(EXPORT_DIR, f"{name}_{datetime.now():%Y%m%d%H%M%S}{ext}")
        report.export_bom(bom_id, path, fmt)
        return send_file(path, as_attachment=True)

    # ---------- BOM 编辑（物料项增删改） ----------

    @app.route("/api/boms/<int:bom_id>/edit", methods=["POST"])
    def api_bom_edit(bom_id):
        """编辑 BOM 基本信息（板卡名称、版本说明）"""
        d = request.json or {}
        try:
            with bom_db.transaction() as conn:
                fields, values = [], []
                if "board_name" in d:
                    fields.append("board_name = ?"); values.append(d["board_name"])
                if "notes" in d:
                    fields.append("notes = ?"); values.append(d["notes"])
                if "version" in d:
                    fields.append("version = ?"); values.append(d["version"])
                if not fields:
                    return err("无修改内容")
                fields.append("updated_at = CURRENT_TIMESTAMP")
                values.append(bom_id)
                conn.execute(
                    f"UPDATE bom_headers SET {', '.join(fields)} WHERE bom_id = ?",
                    values,
                )
            audit.log("bom.edit", 1, target_type="bom", target_id=str(bom_id), detail=d)
            return ok(message="BOM 信息已更新")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/items", methods=["POST"])
    def api_bom_add_item(bom_id):
        """向 BOM 添加物料项"""
        d = request.json
        try:
            pn = d.get("part_number", "").strip()
            qty = int(d.get("quantity", 0))
            ref_des = d.get("reference_designators", "")
            if not pn or qty <= 0:
                return err("物料编码和数量不能为空")
            with bom_db.cross_db_transaction() as conn:
                # 检查 BOM 状态
                status = conn.execute(
                    "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
                ).fetchone()
                if not status:
                    return err("BOM 不存在", 404)
                if status[0] != "Draft":
                    return err("只能编辑 Draft 状态的 BOM")
                # 检查物料是否存在
                mat = conn.execute(
                    "SELECT part_number FROM mat.materials WHERE part_number = ?", (pn,)
                ).fetchone()
                if not mat:
                    return err(f"物料 {pn} 不存在")
                conn.execute(
                    """INSERT INTO bom_items
                       (bom_id, part_number, quantity, reference_designators, created_by)
                       VALUES (?,?,?,?,?)""",
                    (bom_id, pn, qty, ref_des, 1),
                )
            audit.log("bom.item.add", 1, target_type="bom", target_id=str(bom_id),
                      detail={"part_number": pn, "quantity": qty})
            return ok(message=f"已添加物料 {pn}")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/items/<int:item_id>", methods=["PUT"])
    def api_bom_update_item(bom_id, item_id):
        """更新 BOM 中某个物料项（支持更换物料 part_number）"""
        d = request.json
        try:
            with bom_db.cross_db_transaction() as conn:
                status = conn.execute(
                    "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
                ).fetchone()
                if not status or status[0] != "Draft":
                    return err("只能编辑 Draft 状态的 BOM")
                fields, values = [], []
                if "quantity" in d:
                    fields.append("quantity = ?"); values.append(int(d["quantity"]))
                if "reference_designators" in d:
                    fields.append("reference_designators = ?"); values.append(d["reference_designators"])
                if "part_number" in d:
                    new_pn = d["part_number"].strip()
                    if not new_pn:
                        return err("物料编码不能为空")
                    # 验证新物料是否存在
                    mat = conn.execute(
                        "SELECT part_number FROM mat.materials WHERE part_number = ?", (new_pn,)
                    ).fetchone()
                    if not mat:
                        return err(f"物料 {new_pn} 不存在")
                    fields.append("part_number = ?"); values.append(new_pn)
                if not fields:
                    return err("无修改内容")
                values.extend([item_id, bom_id])
                conn.execute(
                    f"UPDATE bom_items SET {', '.join(fields)} WHERE id = ? AND bom_id = ?",
                    values,
                )
            audit.log("bom.item.update", 1, target_type="bom_item", target_id=str(item_id),
                      detail={"bom_id": bom_id, **d})
            return ok(message="物料项已更新")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/items/<int:item_id>", methods=["DELETE"])
    def api_bom_remove_item(bom_id, item_id):
        """从 BOM 中移除物料项"""
        try:
            with bom_db.transaction() as conn:
                status = conn.execute(
                    "SELECT status FROM bom_headers WHERE bom_id = ?", (bom_id,)
                ).fetchone()
                if not status or status[0] != "Draft":
                    return err("只能编辑 Draft 状态的 BOM")
                conn.execute(
                    "DELETE FROM bom_items WHERE id = ? AND bom_id = ?",
                    (item_id, bom_id),
                )
            audit.log("bom.item.remove", 1, target_type="bom_item", target_id=str(item_id),
                      detail={"bom_id": bom_id})
            return ok(message="物料项已移除")
        except Exception as e:
            return err(str(e))

    # ---------- BOM 版本管理 ----------

    @app.route("/api/boms/<int:bom_id>/clone-version", methods=["POST"])
    def api_bom_clone_version(bom_id):
        """基于已有 BOM 创建新版本（复制所有物料项），可选传入修改说明"""
        d = request.json or {}
        new_version = d.get("version", "")
        change_notes = d.get("change_notes", "")
        try:
            with bom_db.transaction() as conn:
                orig = conn.execute(
                    "SELECT * FROM bom_headers WHERE bom_id = ?", (bom_id,)
                ).fetchone()
                if not orig:
                    return err("源 BOM 不存在", 404)
                board_name = orig["board_name"]
                # 自动递增版本号
                if not new_version:
                    ver = orig["version"]
                    new_version = _auto_increment_version(ver)
                # 创建新 BOM 头
                notes = change_notes or orig["notes"] or ""
                cursor = conn.execute(
                    """INSERT INTO bom_headers
                       (board_name, version, status, notes, parent_bom_id, created_by)
                       VALUES (?, ?, 'Draft', ?, ?, ?)""",
                    (board_name, new_version, notes, bom_id, 1),
                )
                new_bom_id = cursor.lastrowid
                # 复制物料项
                items = conn.execute(
                    "SELECT part_number, quantity, reference_designators, ref_count FROM bom_items WHERE bom_id = ?",
                    (bom_id,),
                ).fetchall()
                if items:
                    conn.executemany(
                        """INSERT INTO bom_items
                           (bom_id, part_number, quantity, reference_designators, ref_count, created_by)
                           VALUES (?,?,?,?,?,?)""",
                        [(new_bom_id, it["part_number"], it["quantity"],
                          it["reference_designators"], it["ref_count"], 1)
                         for it in items],
                    )
            audit.log("bom.version.create", 1, target_type="bom", target_id=str(new_bom_id),
                      detail={"parent_bom_id": bom_id, "version": new_version,
                              "items_copied": len(items), "change_notes": change_notes})
            return ok({
                "bom_id": new_bom_id, "board_name": board_name,
                "version": new_version, "items_copied": len(items),
            }, f"新版本 {new_version} 已创建")
        except Exception as e:
            return err(str(e))

    @app.route("/api/boms/<int:bom_id>/versions")
    def api_bom_versions(bom_id):
        """获取同一板卡的所有版本"""
        try:
            with bom_db.get_connection() as conn:
                board = conn.execute(
                    "SELECT board_name FROM bom_headers WHERE bom_id = ?", (bom_id,)
                ).fetchone()
                if not board:
                    return err("BOM 不存在", 404)
                versions = conn.execute(
                    """SELECT bh.*, 
                              (SELECT COUNT(*) FROM bom_items WHERE bom_id = bh.bom_id) AS item_count
                       FROM bom_headers bh
                       WHERE bh.board_name = ?
                       ORDER BY bh.created_at DESC""",
                    (board["board_name"],),
                ).fetchall()
            return ok([dict(v) for v in versions])
        except Exception as e:
            return err(str(e))

    # ------------------------------------------------------------------
    # Calculations
    # ------------------------------------------------------------------

    @app.route("/api/calculations")
    def api_calc_list():
        tasks = calc.list_tasks(
            status=request.args.get("status"),
            limit=int(request.args.get("limit", 50)),
        )
        return ok(tasks)

    @app.route("/api/calculations", methods=["POST"])
    def api_calc_create():
        d = request.json
        bom_quantities = d.get("boms", [])
        if not bom_quantities:
            return err("至少需要选择一个 BOM")
        try:
            task_id = calc.create_task(bom_quantities, async_run=False)
            task = calc.get_task(task_id)
            return ok(task)
        except Exception as e:
            return err(str(e))

    @app.route("/api/calculations/<int:task_id>")
    def api_calc_detail(task_id):
        task = calc.get_task(task_id)
        if not task:
            return err("任务不存在", 404)
        items = calc.get_items(task_id, limit=500)
        task["items"] = items
        # Include merge mappings
        with bom_db.get_connection() as conn:
            merges = conn.execute(
                """SELECT from_part_number, to_part_number
                   FROM calculation_merges WHERE task_id = ?""",
                (task_id,),
            ).fetchall()
        task["merges"] = {m["from_part_number"]: m["to_part_number"] for m in merges}
        return ok(task)

    @app.route("/api/calculations/<int:task_id>/export")
    def api_calc_export(task_id):
        fmt = request.args.get("fmt", "xlsx")
        ext = ".csv" if fmt == "csv" else ".xlsx"
        path = os.path.join(EXPORT_DIR, f"calc_{task_id}_{datetime.now():%Y%m%d%H%M%S}{ext}")
        report.export_calculation(task_id, path, fmt)
        audit.log("calculation.export", 1, target_type="calculation", target_id=str(task_id))
        return send_file(path, as_attachment=True)

    # ---------- 汇算物料合并 ----------

    @app.route("/api/calculations/<int:task_id>/merge-groups")
    def api_calc_merge_groups(task_id):
        """获取可合并的物料分组（同描述+同封装，且有2个及以上不同型号）"""
        try:
            with bom_db.cross_db_connection() as conn:
                items = conn.execute(
                    """SELECT ci.part_number, ci.final_qty, ci.theoretical_qty,
                              ci.loss_rate, ci.loss_included_qty,
                              m.mpn, m.description, m.footprint,
                              m.lifecycle_status, mf.name AS manufacturer_name
                       FROM calculation_items ci
                       JOIN mat.materials m ON ci.part_number = m.part_number
                       LEFT JOIN mat.manufacturers mf ON m.manufacturer_id = mf.id
                       WHERE ci.task_id = ?
                       ORDER BY m.description, m.footprint, m.mpn""",
                    (task_id,),
                ).fetchall()

            # Group by (description, footprint)
            groups = {}
            for row in items:
                row = dict(row)
                key = (row.get("description") or "", row.get("footprint") or "")
                groups.setdefault(key, []).append(row)

            # Filter: only groups with 2+ distinct part_numbers
            result = []
            for (desc, fp), members in groups.items():
                pns = set(m["part_number"] for m in members)
                if len(pns) >= 2 and desc:
                    result.append({
                        "description": desc,
                        "footprint": fp,
                        "items": members,
                        "total_final_qty": sum(m["final_qty"] for m in members),
                    })

            result.sort(key=lambda g: (g["description"], g["footprint"]))
            return ok(result)
        except Exception as e:
            return err(str(e))

    @app.route("/api/calculations/<int:task_id>/merges")
    def api_calc_get_merges(task_id):
        """获取已保存的合并决策"""
        try:
            with bom_db.cross_db_connection() as conn:
                rows = conn.execute(
                    """SELECT cm.from_part_number, cm.to_part_number,
                              m.mpn AS to_mpn, m.description, m.footprint
                       FROM calculation_merges cm
                       LEFT JOIN mat.materials m ON cm.to_part_number = m.part_number
                       WHERE cm.task_id = ?
                       ORDER BY cm.to_part_number""",
                    (task_id,),
                ).fetchall()
            return ok([dict(r) for r in rows])
        except Exception as e:
            return err(str(e))

    @app.route("/api/calculations/<int:task_id>/merges", methods=["POST"])
    def api_calc_save_merges(task_id):
        """保存合并决策。body: {merges: [{from_pn, to_pn}, ...]}"""
        d = request.json or {}
        merges = d.get("merges", [])
        if not merges:
            return err("请提供合并规则")
        try:
            with bom_db.cross_db_transaction() as conn:
                # Clear existing merges for this task
                conn.execute(
                    "DELETE FROM calculation_merges WHERE task_id = ?",
                    (task_id,),
                )
                # Insert new merges
                for merge in merges:
                    from_pn = merge.get("from_pn", "").strip()
                    to_pn = merge.get("to_pn", "").strip()
                    if from_pn and to_pn and from_pn != to_pn:
                        # Verify both materials exist
                        f = conn.execute(
                            "SELECT part_number FROM mat.materials WHERE part_number = ?",
                            (from_pn,),
                        ).fetchone()
                        t = conn.execute(
                            "SELECT part_number FROM mat.materials WHERE part_number = ?",
                            (to_pn,),
                        ).fetchone()
                        if not f:
                            return err(f"物料 {from_pn} 不存在")
                        if not t:
                            return err(f"物料 {to_pn} 不存在")
                        conn.execute(
                            """INSERT INTO calculation_merges
                               (task_id, from_part_number, to_part_number)
                               VALUES (?,?,?)""",
                            (task_id, from_pn, to_pn),
                        )
            audit.log("calculation.merge", 1, target_type="calculation",
                      target_id=str(task_id),
                      detail={"merges_count": len(merges)})
            return ok(message=f"已保存 {len(merges)} 条合并规则")
        except Exception as e:
            return err(str(e))

    @app.route("/api/materials/by-desc-fp")
    def api_materials_by_desc_fp():
        """按描述和封装搜索物料库，用于选择合并目标"""
        desc = request.args.get("desc", "").strip()
        fp = request.args.get("fp", "").strip()
        if not desc:
            return err("请提供描述参数")
        with material_db.get_connection() as conn:
            conditions = ["m.description = ?"]
            params = [desc]
            if fp:
                conditions.append("m.footprint = ?")
                params.append(fp)
            where = " AND ".join(conditions)
            rows = conn.execute(
                f"""SELECT m.part_number, m.mpn, m.description, m.footprint,
                           m.lifecycle_status, mf.name AS manufacturer_name
                    FROM materials m
                    LEFT JOIN manufacturers mf ON m.manufacturer_id = mf.id
                    WHERE {where}
                    ORDER BY m.mpn""",
                params,
            ).fetchall()
        return ok([dict(r) for r in rows])

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    @app.errorhandler(Exception)
    def handle_error(e):
        tb = traceback.format_exc()
        return jsonify({"code": 500, "message": str(e), "trace": tb}), 500

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="物料汇算系统 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=5000, help="端口号")
    parser.add_argument("--db-dir", default=".", help="数据库文件目录")
    parser.add_argument("--db", default="", help="兼容旧版单库路径")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()

    app = create_app(db_dir=args.db_dir, db_path=args.db)

    print("=" * 60)
    print("  物料汇算系统 - Web 界面")
    print("=" * 60)
    print(f"  地址: http://{args.host}:{args.port}")
    print(f"  数据库目录: {args.db_dir}")
    if args.db:
        print(f"  兼容旧库路径: {args.db}")
    print(f"  调试模式: {'开' if args.debug else '关'}")
    print("=" * 60)

    app.run(host=args.host, port=args.port, debug=args.debug)
