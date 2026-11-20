"""静态表层来源：直接包住给定的 table_meta 与 column_meta。

------------------------------------------------------------------------
它为什么不知道 dataset
------------------------------------------------------------------------
上一版这个类叫 ``DatasetTableInput``，构造参数是 ``ProbeFactsContext``，
内部调 ``facts.confirmed(ProfileTask.DATASET)`` —— 于是一个 Input 同时
依赖了**任务编排词汇**和**Context**，两处越界。

改成只收两个 DataFrame 之后，它对 dataset 一无所知，也因此变得通用：
逻辑视图、单测造数、将来「用户上传 schema」都能用同一个类。

「从 probe 事实取出表层」的职责归 ``ProbeFactsContext``，装配层因此只需
一行（``dataset_snapshot()`` 返回的就是 :class:`TableSnapshot`）::

    snap = facts.dataset_snapshot()
    StaticTableInput(snap.table_meta, snap.column_meta)
"""

from __future__ import annotations

import logging
from typing import Mapping, Sequence, TypeAlias

import pandas as pd

from ..config import AlgoConfig
from .table_input_base import TableInput

logger = logging.getLogger(__name__)


class StaticTableInput(TableInput):
    """把现成的表元数据与列元数据包装成 :class:`TableInput`。

    不做任何 IO，**也不做任何转换** —— 传进来什么就是什么。若数据来自
    外部 schema（如 ``provider.Column``），转换是**调用方**的事：
    ``ProbeFactsContext.dataset_snapshot()`` 就是那个适配器。

    ``probe_enums`` 走基类的默认实现（不探）—— 静态来源的枚举随列一起
    给进来，没有可探的对象。
    """

    def __init__(
        self,
        config: AlgoConfig,
        *,
        table_meta: pd.DataFrame | None = None,
        column_meta: Mapping[str, pd.DataFrame] | None = None,
    ) -> None:
        super().__init__(config)
        self._table_meta = table_meta if table_meta is not None else pd.DataFrame()
        self._column_meta = dict(column_meta or {})

    async def fetch_table_meta(self) -> pd.DataFrame:
        if len(self._table_meta) == 0:
            logger.info("静态表层为空")
        return self._table_meta

    async def fetch_column_meta(
        self,
        table_names: Sequence[str],
    ) -> dict[str, pd.DataFrame]:
        """只返回出现在表元数据里的表 —— 表元数据是主，列元数据是从。"""
        known = {str(n) for n in table_names}
        missing = known - set(self._column_meta)
        if missing:
            logger.warning(
                "%s 张表在表元数据里但没有列元数据：%s",
                len(missing),
                ", ".join(sorted(missing)[:5]),
            )
        return {k: v for k, v in self._column_meta.items() if str(k) in known}


#: 语义别名。装配层给 CALIBER / MANAGE 接的就是它，调用点写
#: ``DatasetTableInput(...)`` 比 ``StaticTableInput(...)`` 更表意 ——
#: 但类本身对 dataset 一无所知，这只是个说明性的名字。
DatasetTableInput: TypeAlias = StaticTableInput


__all__ = ["DatasetTableInput", "StaticTableInput"]
