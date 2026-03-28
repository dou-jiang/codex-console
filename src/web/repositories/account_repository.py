"""
账号仓储层：封装常见查询与聚合。
"""

from __future__ import annotations

from typing import Dict, Iterator

from sqlalchemy import case, func

from ...database.models import Account


def iter_query_in_batches(query, *, batch_size: int = 200) -> Iterator[Account]:
    """
    分批迭代 ORM Query，避免一次性 all() 全量加载。
    """
    safe_batch = max(50, min(1000, int(batch_size or 200)))
    offset = 0
    while True:
        rows = query.offset(offset).limit(safe_batch).all()
        if not rows:
            break
        for row in rows:
            yield row
        if len(rows) < safe_batch:
            break
        offset += safe_batch


def query_role_tag_counts(db) -> Dict[str, int]:
    """
    SQL 聚合统计 role_tag/account_label，返回 parent/child/none 计数。
    """
    role_text = func.lower(func.trim(func.coalesce(Account.role_tag, "")))
    label_text = func.lower(func.trim(func.coalesce(Account.account_label, "")))
    resolved_role_expr = case(
        (role_text == "parent", "parent"),
        (role_text == "child", "child"),
        (label_text.in_(["mother", "parent"]), "parent"),
        (label_text == "child", "child"),
        else_="none",
    )
    rows = (
        db.query(resolved_role_expr.label("role"), func.count(Account.id).label("cnt"))
        .group_by(resolved_role_expr)
        .all()
    )
    result = {"parent": 0, "child": 0, "none": 0}
    for row in rows:
        role_value = str(getattr(row, "role", row[0]) or "none").strip().lower()
        count_value = int(getattr(row, "cnt", row[1]) or 0)
        if role_value not in result:
            role_value = "none"
        result[role_value] += count_value
    return result
