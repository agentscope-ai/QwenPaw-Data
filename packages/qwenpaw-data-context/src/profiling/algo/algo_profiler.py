"""编排器：五步流水线 + handler 层组合根。

------------------------------------------------------------------------
五步流水线，对四个任务完全一致
------------------------------------------------------------------------
::

    ① 加载支持本任务的 context（并发）
    ② 各自投影 evidence_for_task
    ③ 装配成具名 TaskEvidence + 门禁
    ④ handler 产出 snapshot            ← handlers/ 的 LLM 层
    ⑤ 存回本任务的 output context

上一个任务 ⑤ 存的，就是下一个任务 ① 读到的本地快照——链条自然接上，
不需要显式传递。

context 的装配不在这里 —— 见 :meth:`.algo_contexts.AlgoContexts.build`。
handler 层的默认装配（:func:`default_handlers`）住本文件：它唯一的消费者
就是下面的 ``AlgoProfiler.build``（严格流派下从 ``handlers/__init__`` 收编）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .algo_contexts import AlgoContexts
from .evidence import TaskEvidence, Unavailable, assemble
from .handlers.base import TaskHandler
from .hooks import RunHooks
from .tasks import TASK_ORDER, ProfileTask

logger = logging.getLogger(__name__)


def default_handlers(
    llm: Any | None,
    *,
    raise_if_cancelled: Callable[[], None] = lambda: None,
    report_progress: Callable[[int, str], None] = lambda _pct, _message: None,
    temperature: float = 0.2,
    concurrency: int = 4,
    dataset_batch_size: int = 20,
    dataset_derived_from_tables_enabled: bool = True,
    dataset_derived_from_tables_include_patterns: tuple[str, ...] = (
        "view%",
        "dataset%",
    ),
    dataset_parse_from_logs_enabled: bool = True,
    dict_batch_size: int = 40,
    language: str = "zh",
    parser_dialect: str = "postgres",
    handler_mode: str = "create",
) -> dict[ProfileTask, "TaskHandler"]:
    """四个 handler 的默认装配（原 ``handlers/__init__``，随严格流派收编到
    它唯一的消费者 —— handler 层组合根）。``llm=None`` 时按 D4 降级注册。

    handler 类在函数内 import：handlers 包依赖 evidence/snapshots，
    顶层互不成环。
    """
    from .handlers.caliber import CaliberHandler
    from .handlers.dataset import DatasetHandler
    from .handlers.dictionary import DictHandler
    from .handlers.domain import DomainHandler
    from .handlers.runner import LlmRunner

    llm_progress_high_water = 10

    def report_llm_progress(pct: int, message: str) -> None:
        """LLM runner 的局部 0..100 映射到算法阶段 10..80。"""
        nonlocal llm_progress_high_water
        bounded = max(0, min(100, int(pct)))
        mapped = 10 + bounded * 70 // 100
        llm_progress_high_water = max(llm_progress_high_water, mapped)
        report_progress(llm_progress_high_water, message)

    runner = (
        LlmRunner(
            llm,
            temperature=temperature,
            concurrency=concurrency,
            raise_if_cancelled=raise_if_cancelled,
            report_progress=report_llm_progress,
        )
        if llm is not None
        else None
    )
    return {
        ProfileTask.DOMAIN: DomainHandler(
            llm, temperature=temperature, language=language, mode=handler_mode
        ),
        ProfileTask.DATASET: DatasetHandler(
            llm,
            runner=runner,
            batch_size=dataset_batch_size,
            derived_from_tables_enabled=dataset_derived_from_tables_enabled,
            derived_from_tables_include_patterns=dataset_derived_from_tables_include_patterns,
            parse_from_logs_enabled=dataset_parse_from_logs_enabled,
            language=language,
            mode=handler_mode,
        ),
        ProfileTask.CALIBER: CaliberHandler(
            llm,
            runner=runner,
            language=language,
            dialect=parser_dialect,
            mode=handler_mode,
        ),
        ProfileTask.DICT: DictHandler(
            llm,
            runner=runner,
            batch_size=dict_batch_size,
            language=language,
            mode=handler_mode,
        ),
    }


@dataclass(frozen=True)
class Outcome:
    """一个任务的运行结果，供编排层做状态机与日志。"""

    task: ProfileTask
    executed: bool
    reason: str | None = None


class AlgoProfiler:
    """对四个任务执行统一的五步流水线。

    不是 ABC —— 唯一可能的抽象点是 handler 查表，那不该是抽象方法。
    """

    def __init__(
        self,
        contexts: AlgoContexts,
        handlers: Mapping[ProfileTask, TaskHandler] | None = None,
        *,
        hooks: RunHooks = RunHooks(),
    ) -> None:
        self.contexts = contexts
        self.handlers = dict(handlers or {})
        self.hooks = hooks

    # -- 单任务 ----------------------------------------------------------

    async def prepare(self, task: ProfileTask) -> TaskEvidence | Unavailable:
        """① 加载 → ② 投影 → ③ 装配。**不执行任务**，供单测与预检使用。"""
        self.hooks.raise_if_cancelled()

        providers = self.contexts.providers_for(task)
        await asyncio.gather(*(c.load() for c in providers))

        parts = {c.name: c.evidence_for_task(task) for c in providers}
        evidence = assemble(task, parts)
        if isinstance(evidence, Unavailable):
            logger.info("%s: 跳过 —— %s", task.value, evidence.reason)
        return evidence

    async def run(self, task: ProfileTask) -> Outcome:
        self.hooks.report_progress(5, "正在加载表结构与查询日志…")
        evidence = await self.prepare(task)
        if isinstance(evidence, Unavailable):
            return Outcome(task, executed=False, reason=evidence.reason)

        handler = self.handlers.get(task)
        if handler is None:
            return Outcome(
                task, executed=False, reason="未注册 handler（LLM 层未实现）"
            )

        self.hooks.report_progress(10, f"正在运行 {task.value} 算法…")
        snapshot = await handler.handle(evidence)
        await self.contexts.output_for(task).save(snapshot)
        return Outcome(task, executed=True)

    # -- 后续需要支持一键全部执行的操作 ----------------------------------------------------------
    # -- 组合根 ----------------------------------------------------------

    @classmethod
    def build(
        cls,
        contexts: AlgoContexts,
        *,
        handlers: Mapping[ProfileTask, TaskHandler] | None = None,
        llm: Any | None = None,
        hooks: RunHooks = RunHooks(),
    ) -> "AlgoProfiler":
        """context 已在别处装好时的组合根 —— 只补 handler 层。

        编排层要先按租户/归属把 ``ProbeFactsInput`` 装进 context（那需要 db
        会话），因此 context 在那边装；这里只接手，不重复装配。
        config 从 ``contexts.config`` 取，不再单独传一份可能对不上的。
        """
        config = contexts.config
        if handlers is None:
            # 函数内 import：handlers 包依赖 evidence/snapshots，顶层互不成环
            from .handlers.llm import OpenAiCompatClient

            if llm is None and config.llm_enabled:
                # 落盘去向由组合根决定，llm.py 不认识路径
                from .stores import TextStore

                llm = OpenAiCompatClient(
                    model=config.llm_model,
                    api_key=config.llm_api_key,
                    base_url=config.llm_base_url,
                    store=TextStore(config.datasource_out_dir / "llm"),
                    max_attempts=config.llm_max_attempts,
                )
            if llm is None:
                logger.warning("build: 未配置 LLM api key，handler 按 D4 降级注册")
            handlers = default_handlers(
                llm,
                raise_if_cancelled=hooks.raise_if_cancelled,
                report_progress=hooks.report_progress,
                temperature=config.llm_temperature,
                concurrency=config.llm_concurrency,
                language=config.llm_output_language,
                dataset_batch_size=config.dataset_batch_size,
                dataset_derived_from_tables_enabled=config.dataset_derived_from_tables_enabled,
                dataset_derived_from_tables_include_patterns=(
                    config.dataset_derived_from_tables_include_patterns
                ),
                dataset_parse_from_logs_enabled=config.dataset_parse_from_logs_enabled,
                dict_batch_size=config.dict_batch_size,
                parser_dialect=config.parser_dialect,
                handler_mode=config.handler_mode,
            )
        # return 必须在 if 之外 —— 显式传入 handlers 的调用同样要拿到实例
        return cls(contexts, handlers, hooks=hooks)

    async def run_all(
        self,
        tasks: tuple[ProfileTask, ...] = TASK_ORDER,
    ) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for index, task in enumerate(tasks, start=1):
            outcome = await self.run(task)
            outcomes.append(outcome)
            self.hooks.report_progress(
                int(index / len(tasks) * 100),
                f"{task.value}: {'done' if outcome.executed else outcome.reason}",
            )
        return outcomes


__all__ = ["AlgoProfiler", "Outcome"]
