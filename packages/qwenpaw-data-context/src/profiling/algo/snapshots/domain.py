"""业务域快照：两张关系表，不是嵌套 JSON。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Container, Mapping

import pandas as pd

from .base import empty_frame, ensure_required

#: 域本身。domain_id 是**算法层的局部序号**（0,1,2…），只用于关联两张表；
#: promote 时由编排层替换成数据库分配的真实 id。
REQUIRED_DOMAINS: tuple[str, ...] = (
    "domain_id",
    "domain_name",
    "display_name",
    "description",
)

#: 表 → 域的归属。**每张表一行。**
REQUIRED_MEMBERS: tuple[str, ...] = (
    "table_name",
    "domain_id",
    "domain_name",
)

DOMAINS_KEY = "domains"
MEMBERS_KEY = "members"


@dataclass(frozen=True)
class DomainSnapshot:
    """域清单 + 成员归属。

    ------------------------------------------------------------------
    为什么拆两张表
    ------------------------------------------------------------------
    主键不同：``domains`` 以 domain_name 为主键，``members`` 以 table_name
    为主键。**主键不同的两张表合并不了** —— 这也是「把 domain 并进
    table_meta」那个方案的硬伤：它最多能并进 members，domains 仍要单独存，
    代价全付了收益只有一半。

    连带收益：旧版 ``member_tables`` 是 JSON 字符串，下游要解析、失败只能
    丢掉整条；现在 members 直接 join。而且 **``compile_domain_of`` 整个
    消失** —— members 表就是那个函数的物化形式，不必让 LLM 生成 Python 代码
    再 ``exec`` 它（实打实的安全隐患），覆盖率也从「编译后逐表求值」变成
    ``len(members) / len(tables)``。
    """

    domains: pd.DataFrame = field(default_factory=lambda: empty_frame(REQUIRED_DOMAINS))
    members: pd.DataFrame = field(default_factory=lambda: empty_frame(REQUIRED_MEMBERS))

    def to_frames(self) -> Mapping[str, pd.DataFrame]:
        return {DOMAINS_KEY: self.domains, MEMBERS_KEY: self.members}

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> DomainSnapshot:
        return cls.build(frames.get(DOMAINS_KEY), frames.get(MEMBERS_KEY))

    @classmethod
    def build(
        cls,
        domains: pd.DataFrame | None,
        members: pd.DataFrame | None,
    ) -> DomainSnapshot:
        clean_domains = ensure_required(domains, REQUIRED_DOMAINS, label="域清单")
        clean_members = ensure_required(members, REQUIRED_MEMBERS, label="域成员")
        if len(clean_members):
            clean_members["table_name"] = clean_members["table_name"].astype(str)
        return cls(domains=clean_domains, members=clean_members)

    def is_empty(self) -> bool:
        return len(self.domains) == 0 and len(self.members) == 0

    # -- 投影 ------------------------------------------------------------

    def domain_of(self, table_name: str | None) -> str | None:
        """表 → 域。members 的一次查表，取代旧版 ``domain_of`` 编译函数。

        未命中时退到末段短名（``public.orders`` → ``orders``）。
        """
        if not table_name or len(self.members) == 0:
            return None
        name = str(table_name).strip()
        lookup = dict(
            zip(self.members["table_name"].astype(str), self.members["domain_name"])
        )
        hit = lookup.get(name) or lookup.get(name.rsplit(".", 1)[-1])
        return str(hit) if hit else None

    def tables(self) -> frozenset[str]:
        """已归域的表名。"""
        if len(self.members) == 0:
            return frozenset()
        return frozenset(self.members["table_name"].astype(str))

    def coverage(self, table_names: Container[str] | frozenset[str]) -> float:
        """归到域的表占比。DOMAIN 任务的重试判据。"""
        expected = frozenset(table_names)
        if not expected:
            return 1.0
        return len(self.tables() & expected) / len(expected)


__all__ = [
    "DOMAINS_KEY",
    "MEMBERS_KEY",
    "REQUIRED_DOMAINS",
    "REQUIRED_MEMBERS",
    "DomainSnapshot",
]
