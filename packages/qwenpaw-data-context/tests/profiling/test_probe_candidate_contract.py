"""Candidate* ↔ staging 表 ↔ 正式表 三层契约守卫（纯静态，零 DB 零 LLM）。

背景：同一实体的列清单在链路上出现多处（DTO / sink dict / staging Table /
promote Create / 正式 Table），v2.1 重构靠人肉同步对齐 —— 本文件把三层间的
**全部差异显式登记**并强制同步：改任何一层而不改登记表，CI 立刻红。
sink 的 insert dict 已由 ``model_dump`` 派生（services/sink.py），promote 的
Create DTO 仍手写，靠本守卫的 staging↔正式表断言间接覆盖。

登记表字段即例外分类学，每一项都必须有理由：

- ``resolved``        DTO 名称引用字段 → sink 解析追加的 staging id 列
- ``nested``          DTO 嵌套字段 → staging JSON 列（None = 展平进子实体表）
- ``parent``          展平子实体时由父级注入的 staging 列
- ``staging_only``    staging 有、正式无（血缘/核验列，如 column_role）
- ``drop_names``      名称冗余列：promote 解析为 id，不进正式表
- ``staging_manual``  staging 默认值或人工维护、算法不产（priority / child_dimensions）
- ``formal_manual``   仅正式表人工档（is_default）
- ``formal_injected`` 正式侧从父级/环境派生（datasource_id / 父 domain_id /
                      按名解析的 dimension_id / metric_id，以及 semantic_config 维护的 scope）
- ``required``        正式表 NOT NULL 业务列对应的 DTO 必填字段

已知缺口（不入登记表）：verified_query 在 tables.py 声明了 profiling 渠道
但 DTO/ProbeSink 无对应 —— 待产品拍板（兑现渠道或修订注释），暂不守卫。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import Table

import profiling.tables as pt
import semantic_config.tables as sct
from profiling.provider import (
    Additivity,
    CaliberColumnRole,
    CandidateBizDomain,
    CandidateDataset,
    CandidateDatasetField,
    CandidateDimension,
    CandidateDimensionCaliber,
    CandidateMemberTable,
    CandidateMetric,
    CandidateMetricCaliber,
    Origin,
    TimeWindow,
)

# ---------------------------------------------------------------------------
# 工程列：sink / repo 注入，DTO 永不携带
# ---------------------------------------------------------------------------

_AUDIT = {
    "tenant_id",
    "workspace_id",
    "created_by",
    "updated_by",
    "is_deleted",
    "created_at",
    "updated_at",
}
STAGING_ENGINEERING = frozenset(
    {"id", "datasource_id", "task_id", "source_type", "probe_status"} | _AUDIT
)
FORMAL_ENGINEERING = frozenset({"id"} | _AUDIT)


@dataclass(frozen=True)
class EntityContract:
    name: str
    candidate: type[BaseModel]
    staging: Table
    formal: Table
    resolved: dict = field(default_factory=dict)
    nested: dict = field(default_factory=dict)
    parent: frozenset = frozenset()
    staging_manual: frozenset = frozenset()
    staging_only: frozenset = frozenset()
    drop_names: frozenset = frozenset()
    formal_manual: frozenset = frozenset()
    formal_injected: frozenset = frozenset()
    required: frozenset = frozenset()


#: origin（create / refine）：七类候选都带，随行落 staging 供前端逐行展示；
#: 不 promote —— 正式表（confirmed 结果）没有「本轮」这个概念
_ORIGIN = frozenset({"origin"})

ENTITIES: tuple[EntityContract, ...] = (
    EntityContract(
        name="biz_domain",
        candidate=CandidateBizDomain,
        staging=pt.staging_biz_domain,
        formal=sct.domain,
        # domain_name 是实体主键不是上游引用，无 id 解析
        nested={"member_tables": "member_tables"},
        # 成员归属在正式面靠 dataset.domain_id 表达，member_tables 不 promote
        staging_only=frozenset({"member_tables"}) | _ORIGIN,
        # scope 不由候选或 Domain complete 提供，由 semantic_config 在数据集写入时维护。
        formal_injected=frozenset({"datasource_scope"}),
        # Excel imports retain their external domain reference separately from id.
        formal_manual=frozenset({"import_id"}),
        required=frozenset({"domain_name"}),
    ),
    EntityContract(
        name="dataset",
        candidate=CandidateDataset,
        staging=pt.staging_dataset,
        formal=sct.dataset,
        resolved={"domain_name": "domain_id"},
        nested={"fields": None},  # 展平进 staging_dataset_field
        staging_only=_ORIGIN,
        drop_names=frozenset({"domain_name"}),
        # 数据集单源列由 promote 从探查身份注入
        formal_injected=frozenset({"datasource_id"}),
        required=frozenset({"dataset_name", "source"}),
    ),
    EntityContract(
        name="dataset_field",
        candidate=CandidateDatasetField,
        staging=pt.staging_dataset_field,
        formal=sct.dataset_field,
        # 展平时父级 CandidateDataset 注入的定位键
        parent=frozenset({"dataset_id", "dataset_name"}),
        staging_only=_ORIGIN,
        drop_names=frozenset({"dataset_name"}),
        # 正式表 domain_id 由 promote 从父 staging_dataset 行派生
        formal_injected=frozenset({"domain_id", "datasource_id"}),
        required=frozenset({"field_name"}),
    ),
    EntityContract(
        name="dimension_caliber",
        candidate=CandidateDimensionCaliber,
        staging=pt.staging_dataset_dimension,
        formal=sct.dataset_dimension,
        resolved={"domain_name": "domain_id", "dataset_name": "dataset_id"},
        # 血缘列，不 promote：column_role 保住时间列身份供 DICT 映射为正式 dimension_type=time；
        # column_name 还原物理列身份供 DICT 聚合/归一（expr 是口径不是列）
        staging_only=frozenset({"column_role", "column_name"}) | _ORIGIN,
        drop_names=frozenset({"domain_name", "dataset_name", "dimension_name"}),
        # dimension_id 由 promote 按 dimension_name 解析
        formal_injected=frozenset({"dimension_id", "datasource_id"}),
        required=frozenset({"expr"}),
    ),
    EntityContract(
        name="dimension",
        candidate=CandidateDimension,
        staging=pt.staging_dimension,
        formal=sct.dimension_dict,
        resolved={"domain_name": "domain_id"},
        staging_only=_ORIGIN,
        drop_names=frozenset({"domain_name"}),
        # 不要求算法生成：staging 默认值/人工编辑后随新实体晋升。
        staging_manual=frozenset({"child_dimensions", "priority"}),
        required=frozenset({"dimension_name"}),
    ),
    EntityContract(
        name="metric_caliber",
        candidate=CandidateMetricCaliber,
        staging=pt.staging_metric_caliber,
        formal=sct.metric_caliber,
        resolved={"domain_name": "domain_id", "dataset_name": "dataset_id"},
        # 血缘列，不 promote：物理列名（派生指标为日志别名）供 DICT synonyms/归一
        staging_only=frozenset({"column_name"}) | _ORIGIN,
        drop_names=frozenset({"domain_name", "dataset_name", "metric_name"}),
        # NULL=未标注，手工档
        formal_manual=frozenset({"is_default"}),
        # metric_id 由 promote 按 metric_name 解析
        formal_injected=frozenset({"metric_id", "datasource_id"}),
        required=frozenset({"expr", "time_window"}),
    ),
    EntityContract(
        name="metric",
        candidate=CandidateMetric,
        staging=pt.staging_metric,
        formal=sct.metric_dict,
        resolved={"domain_name": "domain_id"},
        staging_only=_ORIGIN,
        drop_names=frozenset({"domain_name"}),
        staging_manual=frozenset({"priority"}),
        required=frozenset({"metric_name"}),
    ),
)

_ALL_CANDIDATES = (
    CandidateMemberTable,
    CandidateBizDomain,
    CandidateDatasetField,
    CandidateDataset,
    CandidateDimensionCaliber,
    CandidateMetricCaliber,
    CandidateDimension,
    CandidateMetric,
)


def _staging_business(e: EntityContract) -> set[str]:
    return {c.name for c in e.staging.columns} - STAGING_ENGINEERING


def _formal_business(e: EntityContract) -> set[str]:
    return {c.name for c in e.formal.columns} - FORMAL_ENGINEERING


# ---------------------------------------------------------------------------
# 断言 A：DTO ↔ staging 双向覆盖
# ---------------------------------------------------------------------------


def test_candidate_fields_cover_staging_columns_exactly():
    for e in ENTITIES:
        plain = set(e.candidate.model_fields) - set(e.nested)
        expected = (
            plain
            | {col for col in e.nested.values() if col}
            | set(e.resolved.values())
            | set(e.parent)
            | e.staging_manual
        )
        actual = _staging_business(e)
        assert expected == actual, (
            f"{e.name}: DTO 与 staging 列漂移 —— "
            f"staging 多出 {sorted(actual - expected)}；"
            f"DTO 多出 {sorted(expected - actual)}。"
            "改了一边必须同步另一边（或在登记表登记例外并注明理由）"
        )


# ---------------------------------------------------------------------------
# 断言 B：staging ↔ 正式表
# ---------------------------------------------------------------------------


def test_staging_columns_cover_formal_columns_exactly():
    for e in ENTITIES:
        expected = (
            (_staging_business(e) - e.staging_only - e.drop_names)
            | e.formal_manual
            | e.formal_injected
        )
        actual = _formal_business(e)
        assert expected == actual, (
            f"{e.name}: staging 与正式表列漂移 —— "
            f"正式表多出 {sorted(actual - expected)}；"
            f"staging 侧多出 {sorted(expected - actual)}"
        )


# ---------------------------------------------------------------------------
# 断言 C：可空性下沉 —— 正式表 NOT NULL 的业务列，产出侧必须有人兜底
# ---------------------------------------------------------------------------


def test_formal_not_null_columns_are_required_on_candidate():
    for e in ENTITIES:
        not_null = {
            c.name for c in e.formal.columns if not c.nullable
        } - FORMAL_ENGINEERING
        covered = (
            set(e.required)
            | set(e.resolved.values())
            | set(e.parent)
            | set(e.formal_injected)
        )
        assert not_null <= covered, (
            f"{e.name}: 正式表 NOT NULL 列 {sorted(not_null - covered)} "
            "没有对应的 DTO 必填字段或解析/注入登记 —— 违约会拖到 promote 才炸"
        )
        for field_name in e.required:
            assert e.candidate.model_fields[field_name].is_required(), (
                f"{e.name}.{field_name}: 正式表 NOT NULL，DTO 必须必填 —— "
                "让违约在 emit 构造点爆，不拖到两个阶段后的 promote"
            )


# ---------------------------------------------------------------------------
# 断言 D：值域词表跨层互钉（线上契约 ↔ 算法词表）
# ---------------------------------------------------------------------------


def test_vocab_literals_pinned_to_algo_vocabulary():
    from profiling.algo.handlers.caliber import ADDITIVITY_VALUES
    from profiling.algo.snapshots.caliber import METRIC_TIME_WINDOWS, ColumnRole

    assert set(get_args(TimeWindow)) == set(METRIC_TIME_WINDOWS)
    assert set(get_args(Additivity)) == set(ADDITIVITY_VALUES)
    # L5 血缘列：只有 dimension / date 两个角色产维度绑定
    assert set(get_args(CaliberColumnRole)) == {
        ColumnRole.DIMENSION.value,
        ColumnRole.DATE.value,
    }
    # 探查模式：线上契约（Origin）↔ handler 常量 ↔ AlgoConfig.handler_mode 三方互钉
    from profiling.algo.config import AlgoConfig
    from profiling.algo.handlers.domain import MODE_CREATE, MODE_REFINE

    modes = {MODE_CREATE, MODE_REFINE}
    assert set(get_args(Origin)) == modes
    assert set(get_args(AlgoConfig.model_fields["handler_mode"].annotation)) == modes


# ---------------------------------------------------------------------------
# 断言 E：所有候选 DTO 拒绝多余字段 —— typo 在构造点爆，不落库成静默 NULL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("candidate", _ALL_CANDIDATES)
def test_candidates_reject_unknown_input_fields(candidate):
    with pytest.raises(ValidationError) as exc_info:
        candidate.model_validate({"unexpected_field": "value"})
    assert any(
        error["loc"] == ("unexpected_field",) and error["type"] == "extra_forbidden"
        for error in exc_info.value.errors()
    )


# ---------------------------------------------------------------------------
# 断言 F：origin 三层落位 —— DTO 有、staging 有、正式表无、导出列有
# ---------------------------------------------------------------------------


def test_origin_lands_on_staging_not_on_formal():
    from profiling.stages import STAGE_DEFS

    by_table = {d.table: d for d in STAGE_DEFS.values()}
    for e in ENTITIES:
        assert "origin" in e.candidate.model_fields, f"{e.name}: DTO 少了 origin"
        assert "origin" in _staging_business(e), f"{e.name}: staging 少了 origin 列"
        assert "origin" not in _formal_business(e), (
            f"{e.name}: origin 混进了正式表 —— confirmed 结果不该带「本轮」标记"
        )
        sdef = by_table.get(e.staging.name)
        # dataset_field 尚无独立阶段（挂在 DATASET_FIELDS 上）——有阶段的才校验导出列
        if sdef is not None:
            assert "origin" in sdef.excel_columns, (
                f"{e.name}: origin 不在导出列 —— 导出改上传是覆盖重灌，会被洗成 NULL"
            )
