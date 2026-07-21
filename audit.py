"""
审计日志模块
记录所有关键业务操作到 audit_logs 表。
"""

import json
from datetime import datetime
from typing import Optional, Any


class AuditLogger:
    """操作审计日志记录器。

    db_manager: BOMDatabase（审计日志写入目标）
    users_db:   MaterialDatabase（可选，用于 query 中 JOIN users 表）
    """

    def __init__(self, db_manager, users_db=None):
        self.db = db_manager
        self.users_db = users_db

    def log(
        self,
        action: str,
        user_id: int = 1,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        detail: Optional[Any] = None,
        ip_address: Optional[str] = None,
        conn=None,
    ):
        """
        记录一条审计日志。
        如果传入 conn，则使用该连接写入（调用方负责事务管理）。
        """
        detail_json = None
        if detail is not None:
            detail_json = json.dumps(detail, ensure_ascii=False, default=str)

        if conn is not None:
            conn.execute(
                "INSERT INTO audit_logs (user_id, action, target_type, target_id, detail, ip_address) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, action, target_type, str(target_id) if target_id else None, detail_json, ip_address),
            )
        else:
            with self.db.transaction() as new_conn:
                new_conn.execute(
                    "INSERT INTO audit_logs (user_id, action, target_type, target_id, detail, ip_address) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, action, target_type, str(target_id) if target_id else None, detail_json, ip_address),
                )

    def query(
        self,
        action: Optional[str] = None,
        user_id: Optional[int] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list:
        """查询审计日志，支持多条件筛选。"""
        conditions = []
        params = []

        if action:
            conditions.append("action LIKE ?")
            params.append(f"{action}%")
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            params.append(str(target_id))
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        if self.users_db:
            # 跨库查询：audit_logs (本地) + users (物料库)
            sql = (
                f"SELECT al.*, u.display_name AS user_name "
                f"FROM audit_logs al LEFT JOIN mat.users u ON al.user_id = u.id "
                f"WHERE {where} ORDER BY al.timestamp DESC LIMIT ? OFFSET ?"
            )
            with self.db.cross_db_connection() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        else:
            # 无跨库支持：不 JOIN users
            sql = (
                f"SELECT al.*, NULL AS user_name "
                f"FROM audit_logs al "
                f"WHERE {where} ORDER BY al.timestamp DESC LIMIT ? OFFSET ?"
            )
            with self.db.get_connection() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def count(
        self,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
    ) -> int:
        conditions = []
        params = []
        if action:
            conditions.append("action LIKE ?")
            params.append(f"{action}%")
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        where = " AND ".join(conditions) if conditions else "1=1"
        with self.db.get_connection() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM audit_logs WHERE {where}", params
            ).fetchone()[0]
