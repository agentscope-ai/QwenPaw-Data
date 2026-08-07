"""Topology 一键编排器。

使用：

    python -m context_manager.graph.runner

或：

    from context_manager.graph import build_topology
    build_topology()

运行顺序（重要 — 跨图边依赖端点存在）：

1. ``init_all`` 建好所有约束/索引
2. ``ingest_physical`` 反射 PG → Database/Schema/Table/Column
3. ``write_join_inference`` 推断 JOINS_ON
4. **语义层**（可插拔；默认由 profile 决定，如 ``schema_auto`` 或 ``schema_auto,metrics_dict``）
   — Bridge 边的 Column/Table 端点需已存在
5. ``ingest_knowledge`` — 跨图 SURFACE_METRIC 等端点依赖语义层 Metric 已就绪
6. （可选）``doc_ingest`` — ``profile.doc_ingest_sources`` 文档 LLM 抽取（见 ``context_manager.knowledge``）
7. ``ingest_trace``     — 跨图 RESOLVED_TO/EVIDENCED_BY 的 Metric / Event 也都已存在

每个阶段都可单独跳过，方便调试。所有写入都是 idempotent ``MERGE``。

v3.1 通用化：
- 新增 ``--dataset`` 参数，自动从 ``DatasetProfile`` 读取 db_id / providers /
  knowledge_path / trace_path，无需对每个数据集维护 YAML。
- ``metrics_dict_path / external_events_path / trace_tasks_path / trace_bridges_path``
  改为来自 profile 默认；仍可通过 CLI 参数显式覆盖。
- ``semantic_provider`` 支持逗号分隔列表（见 :mod:`.semantic_pipeline`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from neo4j import Driver, GraphDatabase

from ..config import CFG
from ..utils import get_logger
from . import schema_init
from .joins import write_join_inference
from .knowledge import ingest_knowledge
from .keys import DEFAULT_DB_ID, DEFAULT_SCHEMA
from .physical import ingest_physical, list_database_names, migrate_legacy_keys
from .profile import DatasetProfile, profile_for_dataset
from .semantic_pipeline import SemanticStageInput, run_semantic_stage
from .trace import ingest_trace

log = get_logger("graph.runner")

REPO_ROOT = Path(__file__).resolve().parents[2]

# 向后兼容：旧版默认路径常量（优先使用 profile 提供的路径）
DEFAULT_METRICS_DICT = REPO_ROOT / "data" / "test" / "metrics_dict.yaml"
DEFAULT_EXTERNAL_EVENTS = REPO_ROOT / "data" / "test" / "external_events.yaml"
DEFAULT_TRACE_TASKS = REPO_ROOT / "data" / "test" / "trace_tasks.yaml"
DEFAULT_TRACE_BRIDGES = REPO_ROOT / "data" / "test" / "trace_bridges.yaml"
# 旧名兼容（曾单一 synthetic_traces.yaml）
DEFAULT_SYNTHETIC_TRACES = DEFAULT_TRACE_TASKS


@dataclass
class TopologyRunner:
    """Topology 阶段开关 + 路径配置。

    ``dataset`` 参数（或 ``NEO4J_DATABASE`` 环境变量）用于推断 ``DatasetProfile``。
    Profile 决定 db_id / semantic provider / knowledge & trace 路径；
    显式参数（如 ``metrics_dict_path``）优先级高于 profile。
    """

    dataset: Optional[str] = None
    """数据集名（如 appdata）；None 时从环境变量推断。"""

    db_id: Optional[str] = None
    schema: Optional[str] = None

    do_init_schema: bool = True
    do_physical: bool = True
    do_join_inference: bool = True
    do_semantic: bool = True

    semantic_provider: Optional[str] = None
    """逗号分隔 provider 列表（如 ``schema_auto,metrics_dict``）。
    None 时使用 profile.default_semantic_providers。"""

    do_knowledge: bool = True
    do_trace: bool = True
    do_strategy_cards: bool = False
    do_doc_ingest: bool = False
    """在 knowledge YAML 之后跑 ``profile.doc_ingest_sources``（LLM 文档抽取）。"""
    metrics_dict_path: Optional[Path] = None
    external_events_path: Optional[Path] = None
    trace_tasks_path: Optional[Path] = None  # 轨迹主体 YAML（tasks）；覆盖 profile.trace_path
    trace_bridges_path: Optional[Path] = None  # trace_bridge_links；None→profile 或同目录自动探测

    drop_topology_first: bool = False

    def _resolve_profile(self) -> DatasetProfile:
        return profile_for_dataset(self.dataset)

    def run(self, driver: Optional[Driver] = None) -> None:
        own_driver = False
        if driver is None:
            driver = GraphDatabase.driver(
                CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
            )
            own_driver = True
        try:
            self._run_with_driver(driver)
        finally:
            if own_driver:
                driver.close()

    def _run_with_driver(self, driver: Driver) -> None:
        profile = self._resolve_profile()
        log.info("topology runner: dataset=%r profile=%r", self.dataset, profile.name)

        if self.drop_topology_first:
            log.warning(
                "--drop-topology: removing semantic/trace/knowledge nodes "
                "(physical Database/Table/Column kept for legacy ingest)"
            )
            schema_init.drop_topology(driver)

        if self.do_init_schema:
            schema_init.init_all(driver)

        # db_id / schema 优先级：CLI > 默认（连接凭证来自 semantic_config.db）
        db_id = self.db_id or DEFAULT_DB_ID
        schema = self.schema or DEFAULT_SCHEMA

        # --no-physical with multi-db SQLite-style tables:补 key 并推断 JOINS_ON
        multi_db_sqlite = (
            not self.do_physical
            and getattr(profile, "semantic_auto_all_databases", False)
            and getattr(profile, "semantic_auto_sqlite_style_tables", False)
        )
        sqlite_db_ids: list[str] = []

        if self.do_physical:
            db_id, schema = ingest_physical(driver, db_id=db_id, schema=schema, profile=profile)
            log.info("[1/5] physical layer done — db_id=%s schema=%s", db_id, schema)
        elif multi_db_sqlite:
            sqlite_db_ids = list_database_names(driver)
            if not sqlite_db_ids:
                log.warning("[1/5] multi-db sqlite: no :Database nodes — skip legacy migration")
            else:
                sch = profile.semantic_auto_key_schema
                for bid in sqlite_db_ids:
                    migrate_legacy_keys(driver, db_id=bid, schema=sch)
                log.info(
                    "[1/5] legacy key migration done — %d databases (schema=%r)",
                    len(sqlite_db_ids),
                    profile.semantic_auto_key_schema,
                )
        else:
            log.info("[1/5] physical layer skipped")

        if self.do_join_inference:
            if multi_db_sqlite and sqlite_db_ids:
                sch = profile.semantic_auto_key_schema
                for bid in sqlite_db_ids:
                    write_join_inference(driver, db_id=bid, schema=sch, profile=profile)
                log.info("[2/5] JOINS_ON inference done — %d databases", len(sqlite_db_ids))
            else:
                write_join_inference(driver, db_id=db_id, schema=schema, profile=profile)
                log.info("[2/5] JOINS_ON inference done")
        else:
            log.info("[2/5] JOINS_ON inference skipped")

        if self.do_semantic:
            # provider 列表：CLI > profile 默认
            providers = self.semantic_provider
            if not providers:
                providers = ",".join(profile.default_semantic_providers)

            if profile.name == "generic" and "metrics_dict" not in {
                x.strip().lower() for x in providers.split(",") if x.strip()
            }:
                log.warning(
                    "当前 profile=generic，语义层仅跑 %s；不会读取 metrics_dict.yaml。"
                    "若需要 YAML 中的 Metric/Dimension，请使用 --dataset appdata（或相应 profile）并包含 metrics_dict。",
                    providers,
                )

            # metrics_dict 路径：显式 CLI > profile > 全局默认
            md_path = self.metrics_dict_path
            if md_path is None:
                md_path = profile.metrics_dict_path or DEFAULT_METRICS_DICT

            from .datasource_registry import db_id_to_datasource
            _ds = db_id_to_datasource(db_id)
            inp = SemanticStageInput(
                driver=driver,
                db_id=db_id,
                schema=schema,
                metrics_dict_path=md_path,
                profile=profile,
                datasource_id=(_ds.datasource_id if _ds else ""),
            )
            run_semantic_stage(providers, inp)
            log.info("[3/5] semantic layer done (providers=%s)", providers)
        else:
            log.info("[3/5] semantic layer skipped")

        # knowledge 路径：CLI > profile > None（跳过）
        events_path = self.external_events_path or profile.knowledge_path
        if self.do_knowledge:
            if events_path is None:
                log.info("[4/5] knowledge graph skipped (no path in profile)")
            elif not events_path.exists():
                log.warning(
                    "external_events.yaml not found: %s — knowledge stage skipped",
                    events_path,
                )
            else:
                md_path = self.metrics_dict_path
                if md_path is None:
                    md_path = profile.metrics_dict_path or DEFAULT_METRICS_DICT
                ingest_knowledge(
                    driver,
                    events_path,
                    metrics_dict_path=md_path if md_path.exists() else None,
                )
                log.info("[4/5] knowledge graph done")
        else:
            log.info("[4/5] knowledge graph skipped")

        if self.do_doc_ingest:
            from context_manager.knowledge.pipeline import run_doc_ingest

            if not profile.doc_ingest_sources:
                log.info("doc ingest skipped (profile.doc_ingest_sources empty)")
            else:
                for src in profile.doc_ingest_sources:
                    if not src.exists():
                        log.warning("doc ingest: skip missing %s", src)
                        continue
                    log.info("doc ingest: %s", src)
                    run_doc_ingest(
                        driver,
                        source_path=src,
                        dataset=self.dataset,
                        dry_run=False,
                        skip_llm=False,
                    )

        # trace 路径：CLI > profile > None（跳过）
        traces_path = self.trace_tasks_path or profile.trace_path
        bridges_ov = self.trace_bridges_path or profile.trace_bridges_path
        if self.do_trace:
            if traces_path is None:
                log.info("[5/5] trace graph skipped (no path in profile)")
            elif not traces_path.exists():
                log.warning(
                    "trace_tasks.yaml not found: %s — trace stage skipped",
                    traces_path,
                )
            else:
                ingest_trace(driver, traces_path, bridges_path=bridges_ov)
                log.info("[5/5] trace graph done")
        else:
            log.info("[5/5] trace graph skipped")

        log.info("topology build complete.")


def build_topology(**kwargs) -> None:
    """``TopologyRunner`` 的便捷封装；所有 kwargs 透传给数据类构造器。"""
    TopologyRunner(**kwargs).run()


def main() -> int:
    """Module CLI: ``python -m context_manager.graph.runner [--dataset ...] [--no-trace] ...``"""
    import argparse

    p = argparse.ArgumentParser(
        description="Build NL2SQL graph topology",
    )
    p.add_argument(
        "--dataset",
        default=None,
        metavar="NAME",
        help=(
            "数据集名（如 appdata）；"
            "决定 JOINS_ON 白名单 / semantic providers / knowledge & trace 路径。"
            "省略时：先用 NEO4J_DATABASE 映射；若无或未映射则默认为 appdata（会灌 metrics_dict）。"
            "单独跑 build_topology 且不用 appdata 时请显式传入。"
        ),
    )
    p.add_argument("--db-id", default=None, help="覆盖 :Database{name} 的逻辑库 id（默认 app_db 或数据源 dbname）")
    p.add_argument("--schema", default=None, help="覆盖 schema（默认 public）")
    p.add_argument(
        "--metrics-dict",
        type=Path,
        default=None,
        help="覆盖 metrics_dict.yaml 路径（默认由 profile 决定）",
    )
    p.add_argument(
        "--external-events",
        type=Path,
        default=None,
        help="覆盖 external_events.yaml 路径（默认由 profile 决定）",
    )
    p.add_argument(
        "--trace-tasks",
        "--synthetic-traces",
        type=Path,
        default=None,
        dest="trace_tasks_path",
        metavar="PATH",
        help="覆盖轨迹主体 YAML（tasks + experiences；默认 profile.trace_path / trace_tasks.yaml）",
    )
    p.add_argument(
        "--trace-bridges",
        type=Path,
        default=None,
        dest="trace_bridges_path",
        metavar="PATH",
        help="覆盖 trace_bridge_links 专用 YAML（默认 profile.trace_bridges_path 或同目录 trace_bridges.yaml）",
    )
    p.add_argument("--drop-topology", action="store_true", help="先 DETACH DELETE 拓扑节点再灌")
    # 阶段开关
    p.add_argument("--no-init-schema", dest="do_init_schema", action="store_false")
    p.add_argument("--no-physical", dest="do_physical", action="store_false")
    p.add_argument("--no-joins", dest="do_join_inference", action="store_false")
    p.add_argument("--no-semantic", dest="do_semantic", action="store_false")
    p.add_argument(
        "--semantic-provider",
        default=None,
        metavar="NAMES",
        help=(
            "逗号分隔的语义层 provider 列表（如 schema_auto 或 schema_auto,metrics_dict）。"
            "默认由 profile.default_semantic_providers 决定。"
            "内置：schema_auto, metrics_dict, none。"
            "扩展：context_manager.graph.semantic_pipeline.register_semantic_provider"
        ),
    )
    p.add_argument(
        "--list-semantic-providers",
        action="store_true",
        help="列出已注册的语义层 provider 名称并退出",
    )
    p.add_argument(
        "--list-profiles",
        action="store_true",
        help="列出已注册的 DatasetProfile 名称并退出",
    )
    p.add_argument("--no-knowledge", dest="do_knowledge", action="store_false")
    p.add_argument("--no-trace", dest="do_trace", action="store_false")
    p.add_argument(
        "--doc-ingest",
        dest="do_doc_ingest",
        action="store_true",
        help="knowledge 阶段后运行 profile.doc_ingest_sources（LLM 文档灌图）",
    )
    args = p.parse_args()

    if args.list_semantic_providers:
        from .semantic_pipeline import semantic_provider_names

        for name in semantic_provider_names():
            print(name)
        return 0

    if args.list_profiles:
        from .profile import registered_profile_names

        for name in registered_profile_names():
            print(name)
        return 0

    # 省略 --dataset 时：与 NEO4J_DATABASE 对齐；否则默认定到 appdata（避免误用 generic 从而跳过 metrics_dict）
    if args.dataset is None:
        import os

        from .profile import _NEO4J_DB_TO_PROFILE

        nd = (os.environ.get("NEO4J_DATABASE") or "").strip().lower()
        if nd in _NEO4J_DB_TO_PROFILE:
            args.dataset = _NEO4J_DB_TO_PROFILE[nd]
        elif nd in ("", "neo4j"):
            args.dataset = "appdata"
        else:
            args.dataset = nd or "appdata"

    runner = TopologyRunner(
        dataset=args.dataset,
        db_id=args.db_id,
        schema=args.schema,
        metrics_dict_path=args.metrics_dict,
        external_events_path=args.external_events,
        trace_tasks_path=args.trace_tasks_path,
        trace_bridges_path=args.trace_bridges_path,
        drop_topology_first=args.drop_topology,
        do_init_schema=args.do_init_schema,
        do_physical=args.do_physical,
        do_join_inference=args.do_join_inference,
        do_semantic=args.do_semantic,
        semantic_provider=args.semantic_provider,
        do_knowledge=args.do_knowledge,
        do_trace=args.do_trace,
        do_doc_ingest=args.do_doc_ingest,
    )
    runner.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EXTERNAL_EVENTS",
    "DEFAULT_METRICS_DICT",
    "DEFAULT_SYNTHETIC_TRACES",
    "DEFAULT_TRACE_BRIDGES",
    "DEFAULT_TRACE_TASKS",
    "TopologyRunner",
    "build_topology",
]
