"""mg-088 自动探查：表结构指纹（§2.3 增量判定）。

指纹 = ``sha1(table_name + "|" + sorted("col|type|pk|nullable"))``——结构指纹：
含列名/类型/主键/可空；**不含** comment 与 sample_values（裁决 Q6：comment 变更
不触发重写，samples 易变）。比对发生在编辑器层（explore_state.fingerprint），
图保持 W3 契约不新增属性（§4.6）。
"""

from __future__ import annotations

import hashlib


def table_fingerprint(full_name: str, columns) -> str:
    """两段全名表的结构指纹（列序无关）。"""
    lines = sorted(
        f"{c.name}|{c.type}|{bool(c.pk)}|{bool(c.nullable)}" for c in columns
    )
    payload = full_name + "|" + "\n".join(lines)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
