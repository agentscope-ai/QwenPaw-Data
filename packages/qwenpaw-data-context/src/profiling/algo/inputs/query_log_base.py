"""查询日志来源：抽象基类 + 空实现。

``NullQueryLogInput`` 与基类同文件，因为它不是一个「变体」，而是**接口
定义的一部分** —— 它回答的是「没有日志时这个抽象长什么样」。你会去找
``PGQueryLogInput``，但不会去找 Null 那个，它是你什么都不配时拿到的东西。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar

from ..snapshots.query_log import RawQueryLogSnapshot
from ..config import AlgoConfig

logger = logging.getLogger(__name__)


class QueryLogInput(ABC):
    """查询日志来源。``fetch()`` 是模板：判可用 → 拼 SQL → 执行 → 组装快照。

    方言差异只在**日志表的位置与过滤条件**。

    **日志永远是增强项。** :meth:`available` 返回 False 是正常路径而非错误
    ——没有库名、该数据源类型没有日志表、离线评测，都会走到这里。硬依赖在
    上游确认事实上（``tasks.REQUIRES``），不在日志上。
    """

    #: 日志文本所在列
    query_column: str = "query"

    #: 本实现认领的 datasource_type 键（小写）。最多两个：全称 + 简称。
    #: Null 实现不认领 —— 它是降级落点，不走分派。
    dialect_keys: ClassVar[tuple[str, ...]] = ()

    #: 是否经执行器跑 SQL 取日志。``False`` 的实现（文件来源）不建执行器，
    #: 也不受「方言键必有自建执行器」的注册守卫约束 —— 那条守卫防的是
    #: 「SQL 系方言丢键」，对按定义不执行 SQL 的来源没有意义。
    executes_sql: ClassVar[bool] = True

    #: 键 → 实现类。子类定义时由 ``__init_subclass__`` 自动填充。
    _registry: ClassVar[dict[str, type["QueryLogInput"]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for key in cls.dialect_keys:
            claimed = QueryLogInput._registry.get(key)
            if claimed is not None and claimed is not cls:
                raise TypeError(f"方言键 {key!r} 已被 {claimed.__name__} 认领")
            QueryLogInput._registry[key] = cls

    @classmethod
    def create(cls, config: AlgoConfig) -> "QueryLogInput":
        """按 ``config.resolved_query_log_type()`` 分派构造。**分派的唯一入口。**

        未认领的类型回落 :class:`NullQueryLogInput` —— 日志永远是增强项，
        降级不阻断（与表层的 ``create`` 刻意不对称）。

        兜底策略与 Null 实现**同文件同址**：分派丢兜底这类回归在这里
        一眼可见，不会散落在组合根里被顺手改掉。
        """
        impl = QueryLogInput._registry.get(config.resolved_query_log_type())
        return (impl or NullQueryLogInput)(config)

    @classmethod
    def fingerprint(cls, config: AlgoConfig) -> dict[str, Any]:
        """本地缓存失效判据 —— 会改变 fetch 结果的全部 config 项。

        键名与 config 字段名一致；纯函数、不构造实例（``offline`` 模式
        不建 Input 但仍要校验缓存）。**不含解析器版本** —— 那是下游
        ``ParsedQueryLogInput.fingerprint`` 的事，原始日志与解析各自失效。

        **在基类上调用即分派**：``QueryLogInput.fingerprint(config)`` 按
        ``resolved_query_log_type()`` 转给认领该键的实现（与 ``create``
        同一张注册表），实现类可覆写为自己的判据（文件来源要看文件清单
        而不是时间窗）；未认领的类型落到下面的 SQL 系默认判据。
        """
        if cls is QueryLogInput:
            impl = QueryLogInput._registry.get(config.resolved_query_log_type())
            if impl is not None:
                return impl.fingerprint(config)
        start_inclusive, end_inclusive, _ = config.resolved_query_log_window()
        return {
            "datasource_type": config.resolved_query_log_type(),
            "input_query_log_dbname": config.input_query_log_dbname,
            # 记本次实际窗口而不是原始 None：默认滚动到新的一天时自动失效；
            # 键名与显式配置保持一致，固定日期的既有录制缓存仍可回放。
            "input_query_log_start_ts": start_inclusive,
            "input_query_log_end_ts": end_inclusive,
            "input_query_log_max_count": config.input_query_log_max_count,
        }

    def __init__(self, config: AlgoConfig) -> None:
        """**只收 config。** 方言与执行器都从它来 —— 两处配置早晚会不一致。

        执行器经 ``config.load_query_log_execute_sql()`` 装配；测试桩走
        ``config.execute_sql_func``，不再有第二个入口。
        """
        self.config = config
        # 经方法分派而不是直接内联 —— NullQueryLogInput 覆写为 None：
        # 它按定义不执行 SQL，构造必须永不失败（降级路径的可靠落点）。
        self._execute_sql = self._build_execute_sql()

    def _build_execute_sql(self) -> Callable[..., Any] | None:
        """默认：按日志层类型装配执行器。Null 实现覆写为 None。"""
        return self.config.load_query_log_execute_sql()

    @abstractmethod
    def available(self) -> bool:
        """是否有日志可拉。"""

    @abstractmethod
    def build_sql(self) -> str:
        """日志抽取 SQL。至少产出 :attr:`query_column` 列。"""

    async def fetch(self) -> RawQueryLogSnapshot:
        """原始日志快照。**不解析** —— 解析是 parser 的事。

        不可用或空结果时返回空快照，调用方无需先判断。
        核验（``ensure_required``）在 ``RawQueryLogSnapshot.build`` 内完成。
        """
        if not self.available() or self._execute_sql is None:
            return RawQueryLogSnapshot.build()
        frame = await self._execute_sql(self.build_sql())
        if frame is None or len(frame) == 0:
            return RawQueryLogSnapshot.build()
        return RawQueryLogSnapshot.build(frame)


class NullQueryLogInput(QueryLogInput):
    """没有查询日志。

    首次探查、无日志表的数据源类型、离线评测都走这个。
    **不是错误路径** —— 全部任务在无日志时降级运行而非跳过。
    """

    def _build_execute_sql(self) -> None:
        """不建执行器 —— 本实现按定义不执行任何 SQL。

        也因此构造**永不失败** —— 组合根的降级分派
        （``(impl or NullQueryLogInput)(config)``）才有一个可靠的落点。
        """
        return None

    def available(self) -> bool:
        return False

    def build_sql(self) -> str:  # pragma: no cover - available() 恒 False
        raise NotImplementedError("NullQueryLogInput 没有 SQL")

    async def fetch(self) -> RawQueryLogSnapshot:
        return RawQueryLogSnapshot.build()


__all__ = ["NullQueryLogInput", "QueryLogInput"]
