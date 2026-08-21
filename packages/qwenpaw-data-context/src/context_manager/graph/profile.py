"""Per-dataset topology construction profile（通用化扩展）。

每个 ``DatasetProfile`` 封装了构建图拓扑时所有与数据集相关的"启发式知识"：

- 物理层：表名层前缀 / ``dataset_short`` 截断正则 / 分区键候选列表
- JOINS_ON：join key 白名单 / 维度重叠列名 / 手动 override
- 语义层：默认 semantic provider 栈 / domain 命名函数
- Knowledge / Trace：YAML 路径（None = 跳过该阶段）

内置 profile：``appdata``；
外部数据集通过 :func:`register_profile` 注册，无需修改 runner.py。

``profile_for_dataset`` 根据 ``NEO4J_DATABASE`` 或显式 ``dataset`` 名推断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

# 三元组 (schema.table.column 或 db.schema.table.column, same, via_key)
ManualOverride = Tuple[str, str, str]

REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# 核心数据类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatasetProfile:
    """单个数据集的拓扑构建参数。

    所有字段都有默认值，子定义只需覆盖差异部分。
    """

    name: str
    """唯一标识符，与 ``NEO4J_DATABASE`` / ``--dataset`` 参数对应。"""

    # ---- 物理层 ----
    layer_prefixes: Tuple[str, ...] = ("ads", "dws", "dwd", "dim", "ods", "auto_eval", "eval_task")
    """表名前缀 → ``Table.layer``。匹配第一个前缀，找不到返回 'other'。"""

    partition_key_candidates: Tuple[str, ...] = ("ds", "dt", "stat_date", "data_date", "date_id")
    """分区键候选列名（按优先级排列）。"""

    dataset_short_regex: Optional[str] = None
    """``Formula.dataset_short`` 截断正则（含 domain / rest 命名组）。
    ``None`` 时退化为"取最后一段表名"。"""

    # ---- JOINS_ON ----
    join_key_hints: Tuple[str, ...] = (
        "id", "user_id", "request_id", "item_id", "order_id",
        "session_id", "device_id", "event_id",
    )
    """列名完全匹配 + 在此列表 → confidence 0.7 JOINS_ON。"""

    join_id_pattern: str = r"^.+_(id|key|uuid|code|no|num)$"
    """通用 id-like 列名正则，命中 → confidence 0.6。"""

    join_dim_overlap_hints: Tuple[str, ...] = ()
    """两表都有 ds 列时，这些维度列名也建低权 JOINS_ON（confidence 0.5）。"""

    manual_overrides: Tuple[ManualOverride, ...] = ()
    """手动指定的高置信 JOINS_ON（confidence 1.0）。格式见 ``joins.py``。"""

    # ---- 语义层 ----
    default_semantic_providers: Tuple[str, ...] = ("schema_auto",)
    """默认 semantic provider 栈（前者先跑，后者可以覆盖低 trust 节点）。"""

    domain_namer: Callable[[str], str] = field(
        default=lambda tbl: tbl.split("_")[0] if "_" in tbl else tbl,
        compare=False,
        hash=False,
    )
    """表名 → Domain 名称（schema_auto 阶段调用）。"""

    # ---- knowledge / trace 阶段 ----
    knowledge_path: Optional[Path] = None
    """``external_events.yaml`` 路径；None = 跳过 knowledge 阶段。"""

    trace_path: Optional[Path] = None
    """轨迹主体 YAML（``tasks`` / ``experiences``）；None = 跳过 trace 阶段。"""

    trace_bridges_path: Optional[Path] = None
    """可选：仅含 ``trace_bridge_links`` 的 YAML。
    ``None`` 时若与 ``trace_path`` 同目录存在 ``trace_bridges.yaml`` 则自动加载。
    legacy：桥接也可写在 ``trace_path`` 同一文件内。"""

    # ---- metrics_dict provider ----
    metrics_dict_path: Optional[Path] = None
    """``metrics_dict.yaml`` 路径；仅当 'metrics_dict' 出现在 provider 栈时使用。"""

    ddl_path: Optional[Path] = None
    """物理 DDL 文本路径（如 ``data/test/ddl.txt``）；语义层 post-pass 用它解析
    ``CREATE OR REPLACE VIEW`` 块抽 view 列别名挂到源 ``Column.aliases``。

    None 时本步跳过；
    """

    doc_ingest_sources: tuple[Path, ...] = ()
    """可选：拓扑构建后自动跑 ``context_manager.knowledge`` 的源文件路径列表。"""

    # ---- schema_auto provider（见 :mod:`.semantic_auto`）----
    semantic_auto_all_databases: bool = False
    """True：遍历图中每个 ``:Database``，分别派生语义（多库共用一逻辑库）。"""

    semantic_auto_sqlite_style_tables: bool = False
    """True：``Table`` 节点无 ``schema``（SQLite/DuckDB ingest）；按 ``db`` 匹配，不配 ``schema``。"""

    semantic_auto_qualify_domain_with_db: bool = False
    """True：Domain 名前缀 ``db_id``，避免多 SQLite 库下同表名冲突。"""

    semantic_auto_key_schema: str = ""
    """写入 ``column_key`` / ``table_key`` 时用的逻辑 schema 段；SQLite 图为 ``''``。"""

    schema_auto_bridge_metrics: bool = False
    """True：凡 schema_auto 派生的 Metric（含 SUM/AVG 公式）均写 Formula 桥
    ``Metric-[:HAS_FORMULA]->Formula-[:OF_VIEW]->Dataset-[:CONTAINS_TABLE]->Table``、``-[:USES_COLUMN]->Column``。
    False（默认）：仅 ``layer_prefixes`` 命中的聚合表写桥（Default ODS 明细表不自动生成）。"""

    # ---- schema_auto ANALYZED_BY / DimensionValue ----
    schema_auto_analyzed_by: bool = True
    """True（默认）：schema_auto 在同表的 Metric/Dimension 对间建 ANALYZED_BY 边（confidence=0.8）。
    关闭可用于只想要节点、不想要关系的轻量场景。"""

    schema_auto_analyzed_by_cross_table: bool = True
    """True（默认）：在已有 JOINS_ON 可达的跨表 Metric/Dimension 对建 ANALYZED_BY（confidence=0.5）。
    依赖 JOINS_ON 阶段已先于 schema_auto 执行（runner.py 顺序保证）。"""

    schema_auto_analyzed_by_same_domain_fallback: bool = False
    """False（默认）：同 Domain 内无 JOINS_ON 的 Metric/Dimension 对不建低置信 ANALYZED_BY。
    True：对同 Domain 内尚未关联的任意 Metric/Dimension 对建 confidence=0.3 的 ANALYZED_BY。
    仅在数据集列分布极不均匀（数值列与文本列从不同表出现）时考虑开启。"""

    schema_auto_dimension_values: bool = False
    """False（默认）：不从 Column.sample_values 反填 DimensionValue 节点。
    True：对每个带 MAPS_TO_COLUMN 的 Dimension，从目标列的 sample_values 创建
    DimensionValue 节点及 HAS_VALUE 边。仅在 Column.sample_values 已由物理层写入时有效。"""


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
_PROFILES: dict[str, DatasetProfile] = {}


def register_profile(profile: DatasetProfile) -> None:
    """注册（或覆盖）一个 DatasetProfile。"""
    _PROFILES[profile.name.lower()] = profile


def get_profile(name: str) -> DatasetProfile:
    """按名称查找；不存在则返回 generic profile（name='generic'）。"""
    key = (name or "").strip().lower()
    return _PROFILES.get(key, _PROFILES["generic"])


def registered_profile_names() -> tuple[str, ...]:
    """已注册的全部 profile 名。"""
    return tuple(sorted(_PROFILES.keys()))


# --------------------------------------------------------------------------- #
# 内置 profile：generic
# --------------------------------------------------------------------------- #
def _default_domain_namer(tbl: str) -> str:
    """通用表名 → Domain：取前缀（首段下划线前）；全大写时做 title-case。"""
    seg = tbl.split("_")[0] if "_" in tbl else tbl
    return seg.upper() if seg.islower() and len(seg) <= 4 else seg.title()


_GENERIC = DatasetProfile(
    name="generic",
    domain_namer=_default_domain_namer,
)
register_profile(_GENERIC)


# --------------------------------------------------------------------------- #
# 内置 profile：appdata
# --------------------------------------------------------------------------- #
def _appdata_domain_namer(tbl: str) -> str:
    """``dws_ac_chat_*`` → ``AlphaChat``；``dws_ac_imggen_*`` → ``BetaGen``；等。

    匹配 ``[prefix_]ty_<domain>_...`` 格式，否则退化为通用命名。
    """
    m = re.match(r"^(?:[a-z]+_)?ty_([a-z0-9]+)_", tbl.lower())
    if m:
        seg = m.group(1)
        _MAP = {
            "chatapp": "ChatApp",
            "imagegen": "Wan",
            "damo": "ModelScope",
            "qianwen": "Qianwen",
        }
        return _MAP.get(seg, seg.title())
    return tbl.split("_")[0].title() if "_" in tbl else tbl.title()


register_profile(DatasetProfile(
    name="appdata",
    layer_prefixes=("ads", "dws", "dwd", "dim", "ods", "auto_eval", "eval_task"),
    partition_key_candidates=("ds", "dt", "stat_date", "data_date"),
    dataset_short_regex=r"^public\.dws_ty_(?P<domain>[a-z0-9_]+?)_(?P<rest>.+)$",
    join_key_hints=(
        "user_id",
        "task_id",
        "chat_id",
        "query_id",
        "answer_id",
        "visit_id",
        "request_id",
        "dashscope_request_id",
        "device_id",
        "ds",
    ),
    join_id_pattern=r"^.+_(id|key|uuid|code)$",
    join_dim_overlap_hints=(
        "terminal_type",
        "region",
        "country_name",
        "model_code",
    ),
    manual_overrides=(
        (
            "public.dws_ty_chatapp_overview_1d.terminal_type",
            "public.dws_ty_chatapp_multidim_chatindex_1d.terminal_type",
            "terminal_type",
        ),
        (
            "public.dwd_ty_qwen_chat_msg_fusion_di.user_id",
            "public.dim_ty_qwen_chat_user.user_id",
            "user_id",
        ),
    ),
    default_semantic_providers=("metrics_dict",),
    domain_namer=_appdata_domain_namer,
    knowledge_path=REPO_ROOT / "data" / "test" / "external_events.yaml",
    trace_path=REPO_ROOT / "data" / "test" / "trace_tasks.yaml",
    trace_bridges_path=REPO_ROOT / "data" / "test" / "trace_bridges.yaml",
    metrics_dict_path=REPO_ROOT / "data" / "test" / "metrics_dict.yaml",
    ddl_path=REPO_ROOT / "data" / "test" / "ddl.txt",
    doc_ingest_sources=(REPO_ROOT / "Studio材料" / "merged.txt",),
))


# --------------------------------------------------------------------------- #
# 工具：按 NEO4J_DATABASE 自动推断 profile
# --------------------------------------------------------------------------- #
# 社区版只有 neo4j 默认库；映射到 appdata profile
_NEO4J_DB_TO_PROFILE: dict[str, str] = {
    "appdata": "appdata",
    "neo4j": "appdata",
}


def profile_for_dataset(dataset: Optional[str] = None) -> DatasetProfile:
    """按 ``dataset`` 名（或 ``NEO4J_DATABASE`` 环境变量）推断 profile。

    找不到时返回 'generic' profile（不报错）。
    """
    import os

    name = (dataset or "").strip().lower()
    if not name:
        neo4j_db = (os.environ.get("NEO4J_DATABASE") or "").strip().lower()
        name = _NEO4J_DB_TO_PROFILE.get(neo4j_db, neo4j_db)
    mapped = _NEO4J_DB_TO_PROFILE.get(name, name)
    return get_profile(mapped)


__all__ = [
    "DatasetProfile",
    "ManualOverride",
    "get_profile",
    "profile_for_dataset",
    "register_profile",
    "registered_profile_names",
]
