"""
物料汇算系统 - 主入口
支持 CLI 和编程两种方式调用。
"""

import os
import sys

# 确保当前目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def quick_demo():
    """快速演示：创建一个完整的物料→BOM→汇算→导出流程。"""
    from database import MaterialDatabase, BOMDatabase
    from material_manager import MaterialManager
    from bom_processor import BOMProcessor
    from calculation_engine import CalculationEngine
    from report_exporter import ReportExporter

    print("=" * 60)
    print("  物料汇算系统 - 快速演示")
    print("=" * 60)

    # 1. 初始化数据库
    material_db = MaterialDatabase("demo_material_db.db", bom_db_path="demo_bom_db.db")
    bom_db = BOMDatabase("demo_bom_db.db", material_db_path="demo_material_db.db")
    material_db.initialize()
    bom_db.initialize()
    print("\n[1/6] 数据库初始化完成")

    # 2. 创建制造商
    mm = MaterialManager(material_db, bom_db=bom_db)
    mfrs = mm.list_manufacturers()
    mfr_names = {m["name"].lower(): m["id"] for m in mfrs}

    for name in ["Texas Instruments", "Murata", "Yageo", "Vishay", "Nexperia"]:
        if name.lower() not in mfr_names:
            mfr_id = mm.create_manufacturer(name)
            mfr_names[name.lower()] = mfr_id

    print("[2/6] 制造商数据就绪")

    # 3. 创建示例物料
    cats = {c["code_prefix"]: c["id"] for c in mm.list_categories()}

    demo_materials = [
        ("Yageo",     "RC0402FR-0710KL",  "10K 1% 0402",          "RES", 10000, "Ohm", "0402"),
        ("Yageo",     "RC0402FR-074K7L",  "4.7K 1% 0402",         "RES", 4700,  "Ohm", "0402"),
        ("Yageo",     "RC0402FR-07100KL", "100K 1% 0402",         "RES", 100000,"Ohm", "0402"),
        ("Murata",    "GRM155R71C104KA88D","100nF 16V 0402",      "CAP", 100e-9,"F",   "0402"),
        ("Murata",    "GRM1555C1H100JA01D","10pF 50V 0402",       "CAP", 10e-12,"F",   "0402"),
        ("Murata",    "GRM188R61A106KE69D","10uF 10V 0603",       "CAP", 10e-6, "F",   "0603"),
        ("Texas Instruments", "LM358DR",        "双运放 SOIC-8",   "ICS", None,  None,  "SOIC-8"),
        ("Texas Instruments", "TLV7001DCKT",    "LDO 1.8V SOT-23-5","ICS", 1.8,  "V",  "SOT-23-5"),
        ("Nexperia",  "PESD5V0S1BA",     "TVS 二极管 SOD-323",    "DIO", 5.0,   "V",   "SOD-323"),
        ("Vishay",    "SI2302CDS",       "N-MOSFET SOT-23",       "TRA", None,   None,  "SOT-23"),
    ]

    created_pns = []
    for mfr, mpn, desc, cat, val, unit, fp in demo_materials:
        mfr_id = mfr_names[mfr.lower()]
        cat_id = cats[cat]
        try:
            pn = mm.create(
                manufacturer_id=mfr_id,
                mpn=mpn,
                description=desc,
                category_id=cat_id,
                value=val,
                unit=unit,
                footprint=fp,
                moq=10,
                spq=10,
            )
            created_pns.append(pn)
        except ValueError:
            # 已存在，查找
            existing = mm.list(keyword=mpn)
            if existing:
                created_pns.append(existing[0]["part_number"])

    print(f"[3/6] 创建 {len(created_pns)} 个物料")

    # 4. 创建两个示例 BOM（通过直接操作数据库，因为 CLI 导入需要文件）
    bom_proc = BOMProcessor(bom_db, material_db=material_db, material_manager=mm)

    bom_definitions = [
        {
            "board_name": "MainBoard",
            "version": "Rev1.0",
            "items": [
                (created_pns[0], 20, "R1,R2,R3,R4,R5,R6,R7,R8,R9,R10,R11,R12,R13,R14,R15,R16,R17,R18,R19,R20"),
                (created_pns[1], 10, "R21,R22,R23,R24,R25,R26,R27,R28,R29,R30"),
                (created_pns[2], 5,  "R31,R32,R33,R34,R35"),
                (created_pns[3], 30, "C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14,C15,C16,C17,C18,C19,C20,C21,C22,C23,C24,C25,C26,C27,C28,C29,C30"),
                (created_pns[4], 4,  "C31,C32,C33,C34"),
                (created_pns[5], 8,  "C35,C36,C37,C38,C39,C40,C41,C42"),
                (created_pns[6], 2,  "U1,U2"),
                (created_pns[7], 3,  "U3,U4,U5"),
                (created_pns[8], 6,  "D1,D2,D3,D4,D5,D6"),
            ],
        },
        {
            "board_name": "PowerBoard",
            "version": "Rev1.0",
            "items": [
                (created_pns[0], 15, "R1,R2,R3,R4,R5,R6,R7,R8,R9,R10,R11,R12,R13,R14,R15"),
                (created_pns[2], 8,  "R16,R17,R18,R19,R20,R21,R22,R23"),
                (created_pns[3], 20, "C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14,C15,C16,C17,C18,C19,C20"),
                (created_pns[5], 12, "C21,C22,C23,C24,C25,C26,C27,C28,C29,C30,C31,C32"),
                (created_pns[6], 4,  "U1,U2,U3,U4"),
                (created_pns[9], 6,  "Q1,Q2,Q3,Q4,Q5,Q6"),
                (created_pns[8], 4,  "D1,D2,D3,D4"),
            ],
        },
    ]

    bom_ids = []
    for bom_def in bom_definitions:
        with bom_db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO bom_headers (board_name, version, status, created_by) VALUES (?,?,?,?)",
                (bom_def["board_name"], bom_def["version"], "Draft", 1),
            )
            bom_id = cursor.lastrowid
            for pn, qty, refs in bom_def["items"]:
                ref_count = len(refs.split(",")) if refs else qty
                conn.execute(
                    "INSERT INTO bom_items (bom_id, part_number, quantity, reference_designators, ref_count, created_by) VALUES (?,?,?,?,?,?)",
                    (bom_id, pn, qty, refs, ref_count, 1),
                )
        bom_ids.append(bom_id)
        bom_proc.release(bom_id)

    print(f"[4/6] 创建 {len(bom_ids)} 个 BOM 并已发布")

    # 5. 执行汇算
    calc = CalculationEngine(bom_db, material_db=material_db)
    task_id = calc.create_task(
        [
            {"bom_id": bom_ids[0], "order_quantity": 100},  # MainBoard x100
            {"bom_id": bom_ids[1], "order_quantity": 50},   # PowerBoard x50
        ],
        async_run=False,
    )

    task = calc.get_task(task_id)
    print(f"[5/6] 汇算完成: 任务#{task_id}, 状态={task['status']}, "
          f"耗时={task['duration_ms']}ms")

    # 6. 导出报表
    exporter = ReportExporter(bom_db, material_db=material_db)
    report_path = exporter.export_calculation(task_id, "demo_calc_report.xlsx")
    print(f"[6/6] 报表已导出: {report_path}")

    # 打印汇算摘要
    items = calc.get_items(task_id)
    print(f"\n{'='*60}")
    print(f"  汇算结果摘要 ({len(items)} 种物料)")
    print(f"{'='*60}")
    print(f"{'编码':<14} {'MPN':<22} {'理论量':<8} {'含损耗':<8} {'最终量':<8}")
    print("-" * 64)
    for it in items:
        print(f"{it['part_number']:<14} {(it.get('mpn') or ''):<22} "
              f"{it['theoretical_qty']:<8} {it['loss_included_qty']:<8} "
              f"{it['final_qty']:<8}")

    total_final = sum(it["final_qty"] for it in items)
    print(f"\n总采购量: {total_final}")
    print(f"\n演示完成! 可以使用 CLI 进行更多操作:")
    print(f"  python cli.py --help")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        quick_demo()
    else:
        print("=" * 60)
        print("  物料汇算系统 v2.0")
        print("=" * 60)
        print()
        print("用法:")
        print("  python main.py demo     运行快速演示")
        print("  python cli.py --help    查看 CLI 帮助")
        print()
        print("CLI 常用命令:")
        print("  python cli.py material list")
        print("  python cli.py bom list")
        print("  python cli.py calc create --boms 1:100 2:50 --sync")
        print("  python cli.py calc status 1")
        print("  python cli.py export calc 1")
        print("  python cli.py system stats")
