"""表层来源的抽象基类。

``fetch()`` 是**模板方法**：拉表元数据 → 拉列元数据 → 探枚举 → 组装，
这套顺序对所有来源一致。方言与来源的差异落在三个可覆写的步骤上。

子类只需回答三个问题：有哪些表、每张表有哪些列、要不要探枚举。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Sequence

import pandas as pd

from ..config import AlgoConfig
from ..snapshots.table import TableSnapshot

logger = logging.getLogger(__name__)


class TableInput(ABC):
    """表层来源。产出必须符合 CANONICAL_OVERVIEW / CANONICAL_COLUMNS。

    子类只需回答三个问题：有哪些表、每张表有哪些列、要不要探枚举。
    **产出的必需列见 REQUIRED_*；额外列一律保留**，本层不做归一。

    两条实现路线，必需列相同、额外列各自保留：

    - :class:`~.table_input_sql.SqlTableInput` 及其方言子类 → 物理表
    - :class:`~.table_dataset.DatasetTableInput` → 逻辑视图

    正因形状相同，``TableContext`` 才只有一种，装配时决定接哪个 input，
    此后无人需要区分（DESIGN.md §2.4）。
    """

    #: 本实现认领的 datasource_type 键（小写）。**最多两个：全称 + 简称**
    #: （如 ``("postgresql", "pg")``）。抽象中间层与不走注册分派的实现
    #: （StaticTableInput）留空即可。
    dialect_keys: ClassVar[tuple[str, ...]] = ()

    #: 键 → 实现类。子类定义时由 ``__init_subclass__`` 自动填充 ——
    #: 注册表作为「需要维护的实体」不存在，键与实现同址声明
    #: （与 ``_EMITTING`` 同一原则：同一事实不声明第二遍）。
    _registry: ClassVar[dict[str, type["TableInput"]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for key in cls.dialect_keys:
            claimed = TableInput._registry.get(key)
            if claimed is not None and claimed is not cls:
                raise TypeError(f"方言键 {key!r} 已被 {claimed.__name__} 认领")
            TableInput._registry[key] = cls

    @classmethod
    def create(cls, config: AlgoConfig) -> "TableInput":
        """按 ``config.resolved_table_type()`` 分派构造。**分派的唯一入口** ——
        ``_registry`` 不对外，组合根不摸私有属性。

        未注册的类型 ``KeyError`` 直接抛 —— 表层是所有任务的硬依赖，
        静默降级只会让问题在更下游炸开（与日志层的 ``create`` 刻意不对称）。
        """
        return TableInput._registry[config.resolved_table_type()](config)

    @classmethod
    def fingerprint(cls, config: AlgoConfig) -> dict[str, Any]:
        """本地缓存失效判据 —— 会改变 fetch 结果的全部 config 项。

        判据与消费这些配置的代码同址（与 ``create`` 并排）：新增一个影响
        拉取结果的配置项时，这里同步加一行。键名与 config 字段名一致；
        纯函数、不构造实例 —— ``offline`` 模式不建 Input 但仍要校验缓存。
        """
        return {
            "datasource_type": config.resolved_table_type(),
            "input_table_enums_enabled": config.input_table_enums_enabled,
            "input_table_enums_max_distinct": config.input_table_enums_max_distinct,
            "input_table_exclude_patterns": list(config.input_table_exclude_patterns),
            "input_table_merge_partitions_enabled": config.input_table_merge_partitions_enabled,
        }

    def __init__(self, config: AlgoConfig) -> None:
        self.config = config

    # -- 模板方法：子类通常不重写 -----------------------------------------

    async def fetch(self) -> TableSnapshot:
        """拉表元数据 → 拉列元数据 → 探枚举 → 组装。返回时快照已完整。

        「返回时已完整」是硬约定：对外没有「枚举还没好」这个中间状态，
        因此下游不需要等待、不需要二次拉取（旧版 ``wait_add_enums()``
        那一套由此消失）。

        **本方法不做任何改装。** ``TableSnapshot.build`` 只补齐必需列并
        warning，不改名、不转值、不丢额外列 —— 来源拉到什么就是什么。
        需要转换的来源（外部 schema）自己在 ``fetch_*`` 里转完再交出来。
        """
        table_meta = await self.fetch_table_meta()
        names = sorted(TableSnapshot.build(table_meta).names())
        column_meta = await self.fetch_column_meta(names)
        if self.config.input_table_enums_enabled:
            column_meta = await self.probe_enums(column_meta)
        return TableSnapshot.build(table_meta, column_meta)

    # -- 子类实现 --------------------------------------------------------

    @abstractmethod
    async def fetch_table_meta(self) -> pd.DataFrame:
        """表级元数据。至少含 ``table_name``；额外列一律保留。"""

    @abstractmethod
    async def fetch_column_meta(
        self,
        table_names: Sequence[str],
    ) -> dict[str, pd.DataFrame]:
        """每张表的列级元数据。缺失的表可以不出现在返回值里。"""

    async def probe_enums(
        self,
        column_meta: dict[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        """探查列枚举值。**默认不探** —— 逻辑视图的枚举随列一起继承而来。

        只有能执行 SQL 的来源才需要覆写。
        """
        return column_meta


__all__ = ["TableInput"]
