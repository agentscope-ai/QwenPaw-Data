"""AlgoContexts：11 个 context 的容器与组合根。

3 个来源 + 4 对产出（staged / confirmed），外加装出它们的那份 ``AlgoConfig``。

产出成对而不是一个 context 挂两个槽 —— 两者只差「读事实的哪一侧」和「挂不挂
``EmitStore``」，把同一个类实例化两次即可，不必把 store/缓存/current 那套机制
在类里再写一遍。

本文件只剩组合根自己的知识：

- store 挂载规则：溶解在 ``build`` 的 ``stores()`` 闭包里 —— 挂什么 store
  只有 build 一个消费者，且「会 emit 的 context」可从 ``OUTPUT_CONTEXT``
  一行派生（见 ``_EMITTING``），不值得为它维护四个顶层名字。
- 方言分派：键写在各方言类的 ``dialect_keys`` 上（与实现同址，同一事实
  不声明第二遍），``__init_subclass__`` 在类定义时自动收进 ``_registry``。
  触发导入与门面同住 ``inputs/__init__`` —— 本文件从门面拿两个基类，
  门面执行完注册**必然已完成**（结构保证，不靠记性）。这里只剩 build
  里的两行 ``create(config)``。
- 日志解析管线住 ``parser/pipeline.py``，类壳 ``ParsedQueryLogInput``
  与另两个来源块同构（``create`` / ``fingerprint``）。这里只注入它的
  两个依赖：上游 ``query_log_raw.load`` 与进度钩子。

新增一种数据库 = 写方言实现（带 ``dialect_keys``）+ 在
``inputs/__init__`` 的触发导入里加一行；
新增一种证据 = 写 parser + 在 ``parser/pipeline.py`` 的
``default_parsers`` 里加一行。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Callable, Iterator

from .config import AlgoConfig
from .contexts import (
    CaliberContext,
    DatasetContext,
    DictContext,
    DomainContext,
    Lifecycle,
    QueryLogContext,
    RawQueryLogContext,
    SnapshotContext,
    SourceTableContext,
)
from .hooks import RunHooks
from .inputs import (
    ProbeFactsInput,
    QueryLogInput,
    StaticProbeFactsInput,
    TableInput,
)
from .parser import ParsedQueryLogInput
from .stores import EmitStore, ExcelStore, ParquetStore
from .tasks import OUTPUT_CONTEXT, ProfileTask


#: 会把产出吐给编排层的 context —— **恰是任务产出写入的那四个**（``*_staged``）。
#: 「会 emit 的 = 任务会写的」不是巧合而是定义：emit 的用途就是把任务新产出
#: 回传给人核验；confirmed 是后端已落定的内容，再 emit 回去就是回环。
#: 从 ``OUTPUT_CONTEXT`` 派生，同一个事实不声明第二遍。
_EMITTING: frozenset[str] = frozenset(OUTPUT_CONTEXT.values())


@dataclass(frozen=True)
class AlgoContexts:
    """11 个 context + 装出它们的那份 config。"""

    #: 这批 context 是按哪份配置装出来的。绑在一起，下游不必再单独传
    config: AlgoConfig

    source_table: SourceTableContext
    query_log_raw: RawQueryLogContext
    query_log: QueryLogContext

    domain_staged: DomainContext
    domain_confirmed: DomainContext
    dataset_staged: DatasetContext
    dataset_confirmed: DatasetContext
    columns_caliber_staged: CaliberContext
    columns_caliber_confirmed: CaliberContext
    columns_dict_staged: DictContext
    columns_dict_confirmed: DictContext

    # -- 组合根 ----------------------------------------------------------

    @classmethod
    def build(
        cls,
        config: AlgoConfig,
        *,
        offline: bool = False,
        facts: Any | None = None,
        emit: Callable[[str, str, Any], Any] | None = None,
        hooks: RunHooks = RunHooks(),
    ) -> "AlgoContexts":
        """装配 11 个 context：3 个来源 + 4 对产出（staged / confirmed）。

        context 只拿到「一个能产出 snapshot 的协程」，**不认识任何 Input 类型**。

        执行器由 ``AlgoConfig.load_table_execute_sql`` /
        ``load_query_log_execute_sql`` 装配（测试桩走
        ``config.execute_sql_func``）。``offline=True``
        完全不建输入来源 —— 快照只能来自本地缓存，**保证不发起任何远程
        查询**（录制回放测试用）。
        """
        # facts 可以是「已取好的原始行」（编排层交进来的 {键: DataFrame}）——
        # 在这里包成 ProbeFactsInput，编排层因此不必认识算法库的内部类型。
        # runtime_checkable 的结构检查恰好验证 fetch / facts_of 都在。
        if facts is not None and not isinstance(facts, ProbeFactsInput):
            facts = StaticProbeFactsInput(facts)

        out_dir = config.datasource_out_dir

        def stores(name: str) -> list:
            # store_include_contexts 非空时只给名单内的 context 挂 store；
            # 其余 context 仍在内存里正常链接，只是不落盘、无跨运行缓存
            included = config.store_include_contexts
            if included and name not in included:
                return []
            # parquet 必挂（唯一回读源）；xlsx 是纯交付物，关掉省一半写盘
            # 时间且不影响回读；EmitStore 只挂 staged 产物 —— 这正是 staged
            # 与 confirmed 两个实例**唯一**的 store 差别
            mounted: list = [
                ParquetStore(
                    out_dir / name, compression=config.store_parquet_compression
                )
            ]
            if config.store_excel_enabled:
                mounted.append(
                    ExcelStore(out_dir / name, max_rows=config.store_excel_max_rows)
                )
            if emit is not None and name in _EMITTING:
                mounted.append(EmitStore(emit, context_name=name))
            return mounted

        def fetch_with_start(fetch, message: str):
            """远程拉取开始时打一条进度。不改 Input 内部循环。"""

            async def started():
                # prepare() 已进入 5% 的加载阶段；并发来源开始时不能把任务
                # 进度覆盖回 0%，否则任一来源失败都会在前端表现为从未启动。
                hooks.report_progress(5, message)
                return await fetch()

            return started

        def report_parse_progress(pct: int, message: str) -> None:
            """日志解析的局部 0..100 映射到加载阶段 5..9。"""
            bounded = max(0, min(100, int(pct)))
            hooks.report_progress(5 + bounded * 4 // 100, message)

        # -- 物理表 --
        # 分派、失败策略、失效判据都在 Input 类上（create / fingerprint
        # 同址）：表层硬失败 / 日志层降级 Null
        source_table = SourceTableContext(
            stores=stores("source_table"),
            fetch=None
            if offline
            else fetch_with_start(TableInput.create(config).fetch, "正在拉取表结构…"),
            fingerprint=TableInput.fingerprint(config),
        )

        # -- 原始日志 --
        query_log_raw = RawQueryLogContext(
            stores=stores("query_log_raw"),
            fetch=None
            if offline
            else fetch_with_start(
                QueryLogInput.create(config).fetch, "正在拉取查询日志…"
            ),
            fingerprint=QueryLogInput.fingerprint(config),
        )

        # -- 解析后的证据：派生来源，读上游 context 再转换 --
        # 不挂 offline 闸 —— 解析是纯本地计算，offline 只承诺「不发远程
        # 查询」；禁掉反而毁了指纹漂移时的本地重解析自愈（离线回放靠它）
        query_log = QueryLogContext(
            stores=stores("query_log"),
            fetch=ParsedQueryLogInput.create(
                config,
                load_raw=query_log_raw.load,
                on_progress=report_parse_progress,
            ).fetch,
            fingerprint=ParsedQueryLogInput.fingerprint(config),
        )

        # -- 四对产出 --
        # 每种产出两个实例，只差「读事实的哪一侧」和「挂不挂 EmitStore」。
        # 事实读取器由 facts_of 预绑定 lifecycle（键控查表，无反射），
        # 算法层因此不认识 ProbeFactsInput 的形状，也碰不到租户与权限。
        # 字段名 == context 名 == f"{类.name}_{lifecycle.value}"（同时也是
        # 落盘目录名与 facts_of 的键）：前缀读各类自己的 ``name``，同一事实
        # 不声明第二遍。规则与字段声明一旦漂移，cls(**artifacts) 立刻
        # TypeError —— 列举只在字段声明处存在一次。
        artifacts: dict[str, SnapshotContext] = {}
        for ctx_cls in (DomainContext, DatasetContext, CaliberContext, DictContext):
            for lc in Lifecycle:
                name = f"{ctx_cls.name}_{lc.value}"
                artifacts[name] = ctx_cls(
                    stores=stores(name),
                    name=name,
                    lifecycle=lc,
                    fact=None
                    if facts is None
                    else facts.facts_of(ctx_cls.name, lc.value),
                )

        contexts = cls(
            config=config,
            source_table=source_table,
            query_log_raw=query_log_raw,
            query_log=query_log,
            **artifacts,
        )

        # -- store_include_contexts 校验 ------------------------------------
        # 名单里写错一个名字，那个 context 就悄悄不挂 store —— 不报错，只是产物
        # 全空。这类静默失败必须早死：改 context 名时忘了改名单正是它的典型来源。
        known = {c.name for c in contexts.all()}
        unknown = [n for n in config.store_include_contexts if n not in known]
        if unknown:
            raise ValueError(
                f"store_include_contexts 含未知 context {unknown}；"
                f"可选: {', '.join(sorted(known))}"
            )

        return contexts

    # -- 查找 ------------------------------------------------------------

    def all(self) -> tuple[SnapshotContext, ...]:
        """11 个 context，顺序即字段声明顺序。列举不第二遍。"""
        return tuple(getattr(self, f.name) for f in fields(self) if f.name != "config")

    def by_name(self, name: str) -> SnapshotContext:
        for context in self.all():
            if context.name == name:
                return context
        raise KeyError(f"未知 context: {name!r}")

    def output_for(self, task: ProfileTask) -> SnapshotContext:
        """该任务的产出 context。

        **一律是 staged 侧** —— handler 的产出按定义是待人核验的草稿；
        confirmed 是后端 promote 之后的结果，算法永远不往那边写。

        ``OUTPUT_CONTEXT`` 的读取点因此收敛在这里一处：调用方（profiler 的
        第 ⑤ 步、provider 的投影）只说「这个任务的产出」，不必知道它叫什么名字。
        """
        return self.by_name(OUTPUT_CONTEXT[task])

    def providers_for(self, task: ProfileTask) -> tuple[SnapshotContext, ...]:
        """支持该任务的 context。**依赖图在这里被读出来。**"""
        return tuple(c for c in self.all() if c.supports(task))

    def __iter__(self) -> Iterator[SnapshotContext]:
        return iter(self.all())


__all__ = ["AlgoContexts"]
