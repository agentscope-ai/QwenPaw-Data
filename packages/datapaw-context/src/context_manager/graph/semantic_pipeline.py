"""可插拔的语义层搭建（graph_topology §3.2）。

默认使用 ``metrics_dict`` provider，从 ``data/test/metrics_dict.yaml`` 灌入
Domain / Metric / Dimension 等。其它数据源（按库注释生成、LLM 导出 JSON 等）
通过 :func:`register_semantic_provider` 注册，无需改 :class:`TopologyRunner` 主流程。

v3.1 通用化：
- ``SemanticStageInput`` 新增 ``profile`` 字段，传给各 provider。
- ``run_semantic_stage`` 改为接受逗号分隔的 provider 名列表
  （如 ``"schema_auto,metrics_dict"``），按顺序调用。
- 内置 ``schema_auto`` provider 从 ``semantic_auto`` 模块导入。

编排顺序仍由 :mod:`context_manager.graph.runner` 固定：init → physical（可选）→
JOINS_ON → **本模块** → knowledge → trace。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from neo4j import Driver

from ..utils import get_logger
from .profile import DatasetProfile

log = get_logger("graph.semantic_pipeline")


@dataclass(frozen=True)
class SemanticStageInput:
    """传给各 semantic provider 的上下文。"""

    driver: Driver
    db_id: str
    schema: str
    """YAML/桥接里补全表名时用的逻辑库与 schema（与 :mod:`.physical` 一致）。"""

    metrics_dict_path: Path
    """``metrics_dict`` provider 的 YAML 路径；其它 provider 可忽略或复用为通用 config 路径。"""

    profile: Optional[DatasetProfile] = field(default=None)
    """当前数据集的 ``DatasetProfile``；provider 可按需使用。"""

    datasource_name: str = field(default="")
    """数据源名称；写入 DataSource 顶层节点时使用。"""

    datasource_id: str = field(default="")
    """数据源 id；用于在语义层节点上打 datasource scope。"""


SemanticProviderFn = Callable[[SemanticStageInput], None]

_PROVIDERS: dict[str, SemanticProviderFn] = {}


def register_semantic_provider(name: str, fn: SemanticProviderFn) -> None:
    """注册或覆盖语义层 provider（``name`` 会 lower 后存储）。"""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("provider name must be non-empty")
    _PROVIDERS[key] = fn


def semantic_provider_names() -> tuple[str, ...]:
    """当前已注册的 provider 名（排序）。"""
    return tuple(sorted(_PROVIDERS.keys()))


def run_semantic_stage(provider: str, inp: SemanticStageInput) -> None:
    """执行名为 ``provider`` 的语义层写入逻辑。

    ``provider`` 可以是单个名称或逗号分隔列表（如 ``"schema_auto,metrics_dict"``）。
    多个 provider 按顺序依次执行，前者先写低 trust 节点，后者可升级属性。
    """
    names = [p.strip().lower() for p in (provider or "schema_auto").split(",") if p.strip()]
    for name in names:
        fn = _PROVIDERS.get(name)
        if fn is None:
            raise ValueError(
                f"unknown semantic provider {name!r}; "
                f"available: {', '.join(semantic_provider_names())}"
            )
        log.info("semantic layer: provider=%s", name)
        fn(inp)


def _provider_none(_inp: SemanticStageInput) -> None:
    log.info("semantic layer: provider=none (no-op)")


def _provider_metrics_dict(inp: SemanticStageInput) -> None:
    from .semantic import ingest_semantic

    if not inp.metrics_dict_path.exists():
        log.warning(
            "metrics_dict path not found: %s — semantic stage skipped",
            inp.metrics_dict_path,
        )
        return
    ingest_semantic(
        inp.driver,
        inp.metrics_dict_path,
        db_id=inp.db_id,
        schema=inp.schema,
        profile=inp.profile,
        datasource_name=inp.datasource_name,
        datasource_id=inp.datasource_id,
    )


def _provider_schema_auto(inp: SemanticStageInput) -> None:
    from .semantic_auto import ingest_semantic_auto

    ingest_semantic_auto(
        inp.driver,
        db_id=inp.db_id,
        schema=inp.schema,
        profile=inp.profile,
    )


def _register_builtin_providers() -> None:
    register_semantic_provider("none", _provider_none)
    register_semantic_provider("metrics_dict", _provider_metrics_dict)
    register_semantic_provider("schema_auto", _provider_schema_auto)


_register_builtin_providers()

__all__ = [
    "SemanticStageInput",
    "register_semantic_provider",
    "run_semantic_stage",
    "semantic_provider_names",
]
