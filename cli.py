"""
CLI 交互界面
提供命令行操作入口，覆盖物料库、BOM、汇算、导出等全部核心操作。
"""

import os
import sys
import time
import json
import argparse
from typing import Optional

from database import MaterialDatabase, BOMDatabase
from material_manager import MaterialManager
from bom_processor import BOMProcessor
from calculation_engine import CalculationEngine
from report_exporter import ReportExporter
from audit import AuditLogger


class CLI:
    """物料汇算系统命令行界面。"""

    def __init__(self, db_dir: str = "."):
        material_db_path = os.path.join(db_dir, "material_db.db")
        bom_db_path = os.path.join(db_dir, "bom_db.db")
        self.material_db = MaterialDatabase(material_db_path, bom_db_path=bom_db_path)
        self.bom_db = BOMDatabase(bom_db_path, material_db_path=material_db_path)
        self.material_db.initialize()
        self.bom_db.initialize()
        self.mm = MaterialManager(self.material_db, bom_db=self.bom_db)
        self.bom = BOMProcessor(self.bom_db, material_db=self.material_db, material_manager=self.mm)
        self.calc = CalculationEngine(self.bom_db, material_db=self.material_db)
        self.report = ReportExporter(self.bom_db, material_db=self.material_db)
        self.audit = AuditLogger(self.bom_db, users_db=self.material_db)

    # ==================================================================
    # 物料库操作
    # ==================================================================

    def material_create(self, args):
        """创建物料"""
        # 查找制造商
        mfrs = self.mm.list_manufacturers()
        mfr = next((m for m in mfrs if m["name"].lower() == args.manufacturer.lower()), None)
        if not mfr:
            print(f"制造商 '{args.manufacturer}' 不存在，正在创建...")
            mfr_id = self.mm.create_manufacturer(args.manufacturer)
        else:
            mfr_id = mfr["id"]

        # 查找分类
        cats = self.mm.list_categories()
        cat = next((c for c in cats if c["code_prefix"] == args.category.upper()), None)
        if not cat:
            print(f"错误: 分类 '{args.category}' 不存在")
            print(f"可用分类: {', '.join(c['code_prefix'] for c in cats)}")
            return
        cat_id = cat["id"]

        pn = self.mm.create(
            manufacturer_id=mfr_id,
            mpn=args.mpn,
            description=args.description,
            category_id=cat_id,
            value=args.value,
            unit=args.unit,
            footprint=args.footprint,
            lifecycle_status=args.status,
            moq=args.moq,
            spq=args.spq,
        )
        print(f"物料创建成功: {pn}")

    def material_list(self, args):
        """查询物料列表"""
        materials = self.mm.list(
            category=args.category,
            status=args.status,
            keyword=args.keyword,
            limit=args.limit,
        )
        if not materials:
            print("未找到匹配的物料")
            return

        print(f"\n{'编码':<14} {'MPN':<18} {'描述':<24} {'分类':<6} {'状态':<8} {'封装':<12}")
        print("-" * 90)
        for m in materials:
            print(f"{m['part_number']:<14} {m['mpn']:<18} "
                  f"{(m['description'] or '')[:22]:<24} "
                  f"{(m['category_name'] or ''):<6} "
                  f"{m['lifecycle_status']:<8} "
                  f"{(m['footprint'] or ''):<12}")
        print(f"\n共 {len(materials)} 条记录")

    def material_detail(self, args):
        """查看物料详情"""
        m = self.mm.get(args.part_number)
        if not m:
            print(f"物料不存在: {args.part_number}")
            return
        print(f"\n=== 物料详情: {m['part_number']} ===")
        for key, val in m.items():
            if val is not None and val != "" and key != "created_by":
                print(f"  {key}: {val}")

    def material_import(self, args):
        """批量导入物料"""
        try:
            import pandas as pd
        except ImportError:
            print("错误: material import 需要 pandas 库，请先运行 setup.sh 安装依赖")
            return

        if not os.path.exists(args.file):
            print(f"文件不存在: {args.file}")
            return

        ext = os.path.splitext(args.file)[1].lower()
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(args.file, dtype=str)
        elif ext == ".csv":
            df = pd.read_csv(args.file, dtype=str)
        else:
            print(f"不支持的文件格式: {ext}")
            return

        records = df.fillna("").to_dict("records")
        print(f"读取到 {len(records)} 条记录，正在导入...")

        report = self.mm.import_materials(records, on_duplicate=args.on_duplicate)
        print(f"\n导入完成:")
        print(f"  新增: {report['created']}")
        print(f"  更新: {report['updated']}")
        print(f"  跳过: {report['skipped']}")
        print(f"  错误: {len(report['errors'])}")

        if report["errors"]:
            print("\n错误详情:")
            for err in report["errors"][:10]:
                print(f"  行{err['row']}: {err['message']}")
            if len(report["errors"]) > 10:
                print(f"  ... 还有 {len(report['errors']) - 10} 条错误")

    # ==================================================================
    # BOM 操作
    # ==================================================================

    def bom_import(self, args):
        """导入 BOM"""
        if not os.path.exists(args.file):
            print(f"文件不存在: {args.file}")
            return

        print("阶段一：预校验...")
        report = self.bom.validate_import(args.file)

        print(f"\n校验结果:")
        print(f"  总行数: {report['total_rows']}")
        print(f"  有效行: {len(report['valid_rows'])}")
        print(f"  错误数: {len(report['errors'])}")
        print(f"  警告数: {len(report['warnings'])}")

        if report["errors"]:
            print("\n错误详情:")
            for err in report["errors"][:10]:
                if isinstance(err, dict):
                    print(f"  {err.get('message', err)}")
                else:
                    print(f"  {err}")

        if report["warnings"]:
            print("\n警告:")
            for w in report["warnings"][:5]:
                print(f"  {w}")

        if not report["valid_rows"]:
            print("\n无有效数据，导入终止")
            return

        critical_errors = [e for e in report["errors"] if isinstance(e, dict) and e.get("type") == "column"]
        if critical_errors:
            print("\n存在关键列名错误，无法导入")
            return

        if not args.yes:
            confirm = input(f"\n确认导入 {len(report['valid_rows'])} 条数据？(y/N): ")
            if confirm.lower() != "y":
                print("已取消导入")
                return

        print("\n阶段二：确认入库...")
        result = self.bom.confirm_import(
            args.file,
            board_name=args.board_name,
            version=args.version,
            notes=args.notes or "",
        )
        print(f"\n导入成功!")
        print(f"  BOM ID: {result['bom_id']}")
        print(f"  板卡: {result['board_name']}")
        print(f"  版本: {result['version']}")
        print(f"  物料数: {result['items_imported']}")

    def bom_validate(self, args):
        """仅执行 BOM 预校验（不入库）"""
        if not os.path.exists(args.file):
            print(f"文件不存在: {args.file}")
            return
        report = self.bom.validate_import(args.file)
        print(f"校验结果: {report['total_rows']}行, "
              f"有效{len(report['valid_rows'])}行, "
              f"错误{len(report['errors'])}条, "
              f"警告{len(report['warnings'])}条")
        if report["errors"]:
            for err in report["errors"]:
                print(f"  错误: {err}")
        if report["warnings"]:
            for w in report["warnings"]:
                print(f"  警告: {w}")

    def bom_list(self, args):
        """列出 BOM"""
        boms = self.bom.list_boms(
            board_name=args.board_name,
            status=args.status,
        )
        if not boms:
            print("未找到 BOM")
            return

        print(f"\n{'ID':<6} {'板卡名称':<16} {'版本':<10} {'状态':<10} {'发布日期':<20} {'创建人':<10}")
        print("-" * 80)
        for b in boms:
            print(f"{b['bom_id']:<6} {b['board_name']:<16} {b['version']:<10} "
                  f"{b['status']:<10} {(b['release_date'] or '-'):<20} "
                  f"{(b.get('creator_name') or '-'):<10}")

    def bom_detail(self, args):
        """查看 BOM 详情"""
        bom = self.bom.get_bom(args.bom_id)
        if not bom:
            print(f"BOM 不存在: {args.bom_id}")
            return

        print(f"\n=== BOM: {bom['board_name']} {bom['version']} ===")
        print(f"  状态: {bom['status']}")
        print(f"  创建时间: {bom['created_at']}")
        if bom.get('notes'):
            print(f"  说明: {bom['notes']}")
        print(f"\n  物料明细 ({len(bom['items'])} 项):")
        print(f"  {'编码':<14} {'MPN':<18} {'描述':<20} {'数量':<6} {'位号(合并)':<30}")
        print("  " + "-" * 92)

        from ref_designator import format_designators
        for item in bom["items"]:
            ref_display = format_designators(item.get("reference_designators") or "")
            status_mark = ""
            if item.get("lifecycle_status") in ("NRND", "EOL"):
                status_mark = f" [{item['lifecycle_status']}]"
            print(f"  {item['part_number']:<14} {(item.get('mpn') or ''):<18} "
                  f"{(item.get('description') or '')[:18]:<20} "
                  f"{item['quantity']:<6} {ref_display:<30}{status_mark}")

    def bom_release(self, args):
        """发布 BOM"""
        self.bom.release(args.bom_id, notes=args.notes or "")
        print(f"BOM {args.bom_id} 已发布")

    def bom_obsolete(self, args):
        """废弃 BOM"""
        self.bom.obsolete(args.bom_id, reason=args.reason or "")
        print(f"BOM {args.bom_id} 已废弃")

    def bom_compare(self, args):
        """BOM 版本对比"""
        diff = self.bom.compare(args.bom_id_a, args.bom_id_b)

        print(f"\n=== BOM 对比: #{args.bom_id_a} vs #{args.bom_id_b} ===")

        if diff["added"]:
            print(f"\n新增物料 ({len(diff['added'])} 项):")
            for item in diff["added"]:
                print(f"  + {item['part_number']} {item.get('mpn','')} "
                      f"{item.get('description','')} (数量: {item['quantity']})")

        if diff["removed"]:
            print(f"\n删除物料 ({len(diff['removed'])} 项):")
            for item in diff["removed"]:
                print(f"  - {item['part_number']} {item.get('mpn','')} "
                      f"{item.get('description','')} (数量: {item['quantity']})")

        if diff["changed"]:
            print(f"\n数量变更 ({len(diff['changed'])} 项):")
            for item in diff["changed"]:
                sign = "+" if item["diff"] > 0 else ""
                print(f"  ~ {item['part_number']} {item.get('mpn','')}: "
                      f"{item['qty_a']} → {item['qty_b']} ({sign}{item['diff']})")

        if not any(diff.values()):
            print("\n两个版本完全相同，无差异")

        if args.export:
            bom_a = self.bom.get_bom(args.bom_id_a)
            bom_b = self.bom.get_bom(args.bom_id_b)
            name_a = f"{bom_a['board_name']} {bom_a['version']}" if bom_a else f"BOM#{args.bom_id_a}"
            name_b = f"{bom_b['board_name']} {bom_b['version']}" if bom_b else f"BOM#{args.bom_id_b}"
            self.report.export_bom_diff(diff, name_a, name_b, args.export)
            print(f"\n对比报表已导出: {args.export}")

    # ==================================================================
    # 汇算操作
    # ==================================================================

    def calc_create(self, args):
        """创建汇算任务"""
        # 解析 BOM 列表：格式为 "bom_id:qty,bom_id:qty,..."
        bom_quantities = []
        for entry in args.boms:
            parts = entry.split(":")
            if len(parts) != 2:
                print(f"格式错误: '{entry}'，应为 bom_id:生产数量")
                return
            try:
                bom_id = int(parts[0])
                order_qty = int(parts[1])
            except ValueError:
                print(f"格式错误: '{entry}'，bom_id 和生产数量必须为整数")
                return
            bom_quantities.append({"bom_id": bom_id, "order_quantity": order_qty})

        print(f"正在创建汇算任务（{len(bom_quantities)} 个 BOM）...")
        task_id = self.calc.create_task(bom_quantities, async_run=not args.sync)
        print(f"汇算任务已创建: #{task_id}")

        if args.sync:
            task = self.calc.get_task(task_id)
            print(f"状态: {task['status']}")
            if task["status"] == "Completed":
                print(f"耗时: {task['duration_ms']}ms")
                if task.get("stats"):
                    s = task["stats"]
                    print(f"物料种类: {s['total_parts']}")
                    print(f"总采购量: {s['total_purchase_qty']}")
            elif task["status"] == "Failed":
                print(f"错误: {task['error_message']}")
        else:
            print("后台执行中，使用 'calc status <task_id>' 查看进度")

    def calc_status(self, args):
        """查看汇算任务状态"""
        task = self.calc.get_task(args.task_id)
        if not task:
            print(f"任务不存在: {args.task_id}")
            return

        print(f"\n=== 汇算任务 #{task['task_id']} ===")
        print(f"  状态: {task['status']}")
        print(f"  创建时间: {task['created_at']}")
        if task["started_at"]:
            print(f"  开始时间: {task['started_at']}")
        if task["completed_at"]:
            print(f"  完成时间: {task['completed_at']}")
        if task["duration_ms"]:
            print(f"  耗时: {task['duration_ms']}ms")
        if task["error_message"]:
            print(f"  错误: {task['error_message']}")

        if task.get("boms"):
            print(f"\n  参与汇算的 BOM:")
            for b in task["boms"]:
                print(f"    {b['board_name']} {b['version']} × {b['order_quantity']}")

        if task.get("stats"):
            s = task["stats"]
            print(f"\n  汇算统计:")
            print(f"    物料种类: {s['total_parts']}")
            print(f"    总采购量: {s['total_purchase_qty']}")

    def calc_items(self, args):
        """查看汇算明细"""
        items = self.calc.get_items(args.task_id, limit=args.limit)
        if not items:
            print("无汇算明细（任务可能未完成）")
            return

        print(f"\n{'编码':<14} {'MPN':<16} {'描述':<18} {'理论量':<8} {'损耗率':<8} "
              f"{'含损耗':<8} {'最终量':<8} {'状态':<6}")
        print("-" * 96)

        for it in items:
            status_mark = it["lifecycle_status"]
            if status_mark in ("NRND", "EOL"):
                status_mark = f"!{status_mark}"
            print(f"{it['part_number']:<14} {(it.get('mpn') or ''):<16} "
                  f"{(it.get('description') or '')[:16]:<18} "
                  f"{it['theoretical_qty']:<8} "
                  f"{it['loss_rate']:<8.1%} "
                  f"{it['loss_included_qty']:<8} "
                  f"{it['final_qty']:<8} "
                  f"{status_mark:<6}")

        total = self.calc.count_items(args.task_id)
        if total > args.limit:
            print(f"\n显示前 {args.limit} 条，共 {total} 条")

    def calc_list(self, args):
        """列出汇算任务"""
        tasks = self.calc.list_tasks(status=args.status)
        if not tasks:
            print("无汇算任务")
            return

        print(f"\n{'ID':<6} {'状态':<12} {'创建时间':<20} {'耗时(ms)':<10} {'创建人':<10}")
        print("-" * 64)
        for t in tasks:
            print(f"{t['task_id']:<6} {t['status']:<12} "
                  f"{t['created_at']:<20} "
                  f"{(t['duration_ms'] or '-'):<10} "
                  f"{(t.get('creator_name') or '-'):<10}")

    # ==================================================================
    # 导出操作
    # ==================================================================

    def export_calc(self, args):
        """导出汇算报表"""
        fmt = "csv" if args.csv else "xlsx"
        ext = ".csv" if fmt == "csv" else ".xlsx"
        output = args.output or f"calc_report_{args.task_id}{ext}"
        path = self.report.export_calculation(args.task_id, output, fmt)
        print(f"汇算报表已导出: {path}")

    def export_materials(self, args):
        """导出物料库"""
        fmt = "csv" if args.csv else "xlsx"
        ext = ".csv" if fmt == "csv" else ".xlsx"
        output = args.output or f"materials{ext}"
        path = self.report.export_materials(
            output, category=args.category, status=args.status, fmt=fmt,
        )
        print(f"物料库已导出: {path}")

    def export_bom(self, args):
        """导出 BOM"""
        fmt = "csv" if args.csv else "xlsx"
        ext = ".csv" if fmt == "csv" else ".xlsx"
        bom = self.bom.get_bom(args.bom_id)
        name = bom["board_name"].replace(" ", "_") if bom else f"bom_{args.bom_id}"
        ver = bom["version"].replace(".", "") if bom else ""
        output = args.output or f"{name}_{ver}{ext}"
        path = self.report.export_bom(args.bom_id, output, fmt)
        print(f"BOM 已导出: {path}")

    # ==================================================================
    # 系统操作
    # ==================================================================

    def system_backup(self, args):
        """数据库备份"""
        from datetime import datetime as dt
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = args.dir or "backups"
        os.makedirs(backup_dir, exist_ok=True)
        mat_backup_path = os.path.join(backup_dir, f"material_db_{timestamp}.db")
        bom_backup_path = os.path.join(backup_dir, f"bom_db_{timestamp}.db")
        self.material_db.backup(mat_backup_path)
        self.bom_db.backup(bom_backup_path)
        self.audit.log("system.backup", 1, target_type="system",
                       detail={"material_path": mat_backup_path, "bom_path": bom_backup_path})
        print(f"备份完成:")
        print(f"  物料库: {mat_backup_path}")
        print(f"  BOM库: {bom_backup_path}")

    def system_check(self, args):
        """数据库完整性检查"""
        mat_ok = self.material_db.integrity_check()
        bom_ok = self.bom_db.integrity_check()
        print(f"物料库完整性检查: {'通过' if mat_ok else '失败!'}")
        print(f"BOM库完整性检查: {'通过' if bom_ok else '失败!'}")

    def system_stats(self, args):
        """系统统计"""
        # Material stats
        with self.material_db.get_connection() as conn:
            mat_stats = {
                "materials": conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
                "manufacturers": conn.execute("SELECT COUNT(*) FROM manufacturers").fetchone()[0],
                "nrnd_eol": conn.execute(
                    "SELECT COUNT(*) FROM materials WHERE lifecycle_status IN ('NRND','EOL')"
                ).fetchone()[0],
            }
        # BOM stats
        with self.bom_db.get_connection() as conn:
            bom_stats = {
                "bom_headers": conn.execute("SELECT COUNT(*) FROM bom_headers").fetchone()[0],
                "bom_released": conn.execute("SELECT COUNT(*) FROM bom_headers WHERE status='Released'").fetchone()[0],
                "calculation_tasks": conn.execute("SELECT COUNT(*) FROM calculation_tasks").fetchone()[0],
            }

        print("\n=== 系统统计 ===")
        print(f"  物料总数: {mat_stats['materials']}")
        print(f"  制造商数: {mat_stats['manufacturers']}")
        print(f"  BOM 总数: {bom_stats['bom_headers']} (已发布: {bom_stats['bom_released']})")
        print(f"  汇算任务: {bom_stats['calculation_tasks']}")
        if mat_stats['nrnd_eol'] > 0:
            print(f"  ⚠ NRND/EOL 物料: {mat_stats['nrnd_eol']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bom",
        description="物料汇算系统 CLI",
    )
    parser.add_argument("--db-dir", default=".", help="数据库文件目录")
    sub = parser.add_subparsers(dest="command", help="子命令")

    # ---- material ----
    mat = sub.add_parser("material", aliases=["mat"], help="物料库管理")
    mat_sub = mat.add_subparsers(dest="action")

    p = mat_sub.add_parser("create", help="创建物料")
    p.add_argument("--manufacturer", "-m", required=True, help="制造商名称")
    p.add_argument("--mpn", required=True, help="制造商型号")
    p.add_argument("--description", "-d", required=True, help="描述")
    p.add_argument("--category", "-c", required=True, help="分类代码 (如 RES, CAP)")
    p.add_argument("--value", type=float, help="数值")
    p.add_argument("--unit", help="单位")
    p.add_argument("--footprint", "-f", help="封装")
    p.add_argument("--status", default="Active", choices=["Active", "NRND", "EOL"])
    p.add_argument("--moq", type=int, default=1)
    p.add_argument("--spq", type=int, default=1)

    p = mat_sub.add_parser("list", help="查询物料列表")
    p.add_argument("--category", "-c", help="按分类筛选")
    p.add_argument("--status", "-s", help="按状态筛选")
    p.add_argument("--keyword", "-k", help="关键词搜索")
    p.add_argument("--limit", type=int, default=50)

    p = mat_sub.add_parser("detail", help="查看物料详情")
    p.add_argument("part_number", help="物料编码")

    p = mat_sub.add_parser("import", help="批量导入物料")
    p.add_argument("file", help="文件路径 (CSV/Excel)")
    p.add_argument("--on-duplicate", default="skip", choices=["skip", "update"])

    # ---- bom ----
    bom = sub.add_parser("bom", help="BOM 管理")
    bom_sub = bom.add_subparsers(dest="action")

    p = bom_sub.add_parser("import", help="导入 BOM")
    p.add_argument("file", help="BOM 文件路径")
    p.add_argument("--board-name", "-b", required=True, help="板卡名称")
    p.add_argument("--version", "-v", default="Rev1.0", help="版本号")
    p.add_argument("--notes", "-n", help="版本说明")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认直接导入")

    p = bom_sub.add_parser("validate", help="仅预校验 BOM（不入库）")
    p.add_argument("file", help="BOM 文件路径")

    p = bom_sub.add_parser("list", help="列出 BOM")
    p.add_argument("--board-name", "-b", help="按板卡名称筛选")
    p.add_argument("--status", "-s", help="按状态筛选")

    p = bom_sub.add_parser("detail", help="查看 BOM 详情")
    p.add_argument("bom_id", type=int, help="BOM ID")

    p = bom_sub.add_parser("release", help="发布 BOM")
    p.add_argument("bom_id", type=int)
    p.add_argument("--notes", "-n", help="发布说明")

    p = bom_sub.add_parser("obsolete", help="废弃 BOM")
    p.add_argument("bom_id", type=int)
    p.add_argument("--reason", "-r", help="废弃原因")

    p = bom_sub.add_parser("compare", aliases=["diff"], help="BOM 版本对比")
    p.add_argument("bom_id_a", type=int, help="BOM A 的 ID")
    p.add_argument("bom_id_b", type=int, help="BOM B 的 ID")
    p.add_argument("--export", "-e", help="导出对比报表路径")

    # ---- calc ----
    calc = sub.add_parser("calc", help="汇算管理")
    calc_sub = calc.add_subparsers(dest="action")

    p = calc_sub.add_parser("create", help="创建汇算任务")
    p.add_argument("--boms", nargs="+", required=True,
                   help="BOM 列表，格式: bom_id:生产数量 (如 1:100 2:50)")
    p.add_argument("--sync", action="store_true", help="同步执行（等待完成）")

    p = calc_sub.add_parser("status", help="查看汇算任务状态")
    p.add_argument("task_id", type=int)

    p = calc_sub.add_parser("items", help="查看汇算明细")
    p.add_argument("task_id", type=int)
    p.add_argument("--limit", type=int, default=200)

    p = calc_sub.add_parser("list", help="列出汇算任务")
    p.add_argument("--status", "-s", help="按状态筛选")

    # ---- export ----
    exp = sub.add_parser("export", help="报表导出")
    exp_sub = exp.add_subparsers(dest="action")

    p = exp_sub.add_parser("calc", help="导出汇算报表")
    p.add_argument("task_id", type=int)
    p.add_argument("--output", "-o", help="输出文件路径")
    p.add_argument("--csv", action="store_true", help="导出为 CSV 格式")

    p = exp_sub.add_parser("materials", help="导出物料库")
    p.add_argument("--output", "-o", help="输出文件路径")
    p.add_argument("--category", "-c", help="按分类筛选")
    p.add_argument("--status", "-s", help="按状态筛选")
    p.add_argument("--csv", action="store_true")

    p = exp_sub.add_parser("bom", help="导出 BOM")
    p.add_argument("bom_id", type=int)
    p.add_argument("--output", "-o", help="输出文件路径")
    p.add_argument("--csv", action="store_true")

    # ---- system ----
    sys_cmd = sub.add_parser("system", aliases=["sys"], help="系统管理")
    sys_sub = sys_cmd.add_subparsers(dest="action")

    p = sys_sub.add_parser("backup", help="数据库备份")
    p.add_argument("--dir", "-d", default="backups", help="备份目录")

    sys_sub.add_parser("check", help="数据库完整性检查")
    sys_sub.add_parser("stats", help="系统统计")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cli = CLI(db_dir=args.db_dir)

    # 命令路由
    cmd = args.command
    action = getattr(args, "action", None)

    routes = {
        ("material", "create"): cli.material_create,
        ("mat", "create"): cli.material_create,
        ("material", "list"): cli.material_list,
        ("mat", "list"): cli.material_list,
        ("material", "detail"): cli.material_detail,
        ("mat", "detail"): cli.material_detail,
        ("material", "import"): cli.material_import,
        ("mat", "import"): cli.material_import,
        ("bom", "import"): cli.bom_import,
        ("bom", "validate"): cli.bom_validate,
        ("bom", "list"): cli.bom_list,
        ("bom", "detail"): cli.bom_detail,
        ("bom", "release"): cli.bom_release,
        ("bom", "obsolete"): cli.bom_obsolete,
        ("bom", "compare"): cli.bom_compare,
        ("bom", "diff"): cli.bom_compare,
        ("calc", "create"): cli.calc_create,
        ("calc", "status"): cli.calc_status,
        ("calc", "items"): cli.calc_items,
        ("calc", "list"): cli.calc_list,
        ("export", "calc"): cli.export_calc,
        ("export", "materials"): cli.export_materials,
        ("export", "bom"): cli.export_bom,
        ("system", "backup"): cli.system_backup,
        ("sys", "backup"): cli.system_backup,
        ("system", "check"): cli.system_check,
        ("sys", "check"): cli.system_check,
        ("system", "stats"): cli.system_stats,
        ("sys", "stats"): cli.system_stats,
    }

    handler = routes.get((cmd, action))
    if handler:
        try:
            handler(args)
        except Exception as e:
            print(f"错误: {e}")
    else:
        # 尝试打印子命令帮助
        for sub_action in parser._subparsers._actions:
            if hasattr(sub_action, '_name_parser_map') and cmd in sub_action._name_parser_map:
                sub_action._name_parser_map[cmd].print_help()
                return
        parser.print_help()


if __name__ == "__main__":
    main()
