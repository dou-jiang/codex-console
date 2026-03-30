"""
账号业务服务层：对路由层暴露稳定的查询/聚合接口。
"""

from __future__ import annotations

from typing import Dict, Iterator

from ..repositories.account_repository import iter_query_in_batches, query_role_tag_counts


def stream_accounts(query, *, batch_size: int = 200) -> Iterator:
    return iter_query_in_batches(query, batch_size=batch_size)


def get_role_tag_counts(db) -> Dict[str, int]:
    return query_role_tag_counts(db)
