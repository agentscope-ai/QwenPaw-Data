"""Profiling 任务相关的全部 LLM prompt（仅通过 build 函数对外暴露）。"""

from __future__ import annotations

import json
from typing import Any

# ===========================================================================
# domain
# ===========================================================================

_DOMAIN_SYSTEM_PROMPT = """你是数仓领域专家。根据历史 SQL 的 JOIN 关系聚类结果，一次性生成：
1) Python 函数 domain_of
2) 各 domain 的元信息解释

只输出 JSON，不要输出 markdown，不要输出额外解释。格式必须为：
{
  "domain_of_code": "def domain_of(table_name: str) -> str | None:\\n    ...",
  "domains": [
    {
      "domain": "英文业务域标识",
      "display_name": "对外展示的规范名称",
      "description": "对该 domain 的详细含义描述",
      "alias": "中文别名，可多个，用、分隔；没有可填空字符串"
    }
  ]
}

对 domain_of_code 的要求：
1. 必须只定义一个函数：domain_of(table_name: str) -> str | None
2. 函数体仅使用 if/elif/in/lower()，禁止 import、exec、网络、文件 IO
3. 每个 domain 名称简洁，与表名语义一致；使用小写英文、数字或下划线；标识彼此独立、无层级命名
4. 同一 cluster 内所有表必须映射到同一 domain（通过函数规则保证）
5. 无法识别时返回 None
6. domain_of_code 必须是纯 Python 源码字符串，不要包含 markdown 代码块标记

对 domains 元信息的要求：
1. domain 必须与 domain_of 可能返回的标识完全一致
2. 覆盖函数中会出现的全部 domain，不允许新增函数不会返回的 domain
3. display_name 适合资产目录/语义层展示；description 说明业务含义；alias 为中文别名
4. 不要输出每个 table 属于哪个 domain（表归属由调用方执行 domain_of 得到）
5. 各 domain 应为同一抽象层级、相互独立的并列概念：同属一个业务类别，不得存在从属/包含关系或明显语义交集（反例：同时产出 commerce 与 order，或 sales 与 ecommerce 重叠）
"""


def build_domain_system_prompt() -> str:
    return _DOMAIN_SYSTEM_PROMPT


def build_domain_user_prompt(request: Any) -> str:
    return (
        "以下是 JOIN 聚类与未聚类表信息。"
        "请一次性生成 domain_of 函数以及各 domain 的元信息。"
        "不要输出每个 table 属于哪个 domain。\n\n"
        f"{json.dumps(request, ensure_ascii=False, indent=2)}"
    )


def build_domain_retry_prompt(
    request: Any,
    unmapped_tables: list[str] | tuple[str, ...],
    previous_response: str,
    *,
    previous_error: str | None = None,
) -> str:
    """构造覆盖率不足或校验失败后的完整重试请求，而不是仅请求增量。"""
    if previous_error:
        reason = (
            "上一次 domain_of / domains 校验失败，请根据报错修正后返回完整 JSON"
            f"（含 domain_of_code 与 domains），不要只返回增量。\n"
            f"上次报错：{previous_error}\n\n"
        )
    else:
        reason = (
            "上一次 domain_of / domains 元信息没有达到 80% 表覆盖率要求。"
            "请返回修正后的完整 JSON（含 domain_of_code 与 domains），不要只返回增量。\n"
            f"未覆盖表：{json.dumps(list(unmapped_tables), ensure_ascii=False)}\n\n"
        )
    return (
        f"{reason}"
        "不要输出每个 table 属于哪个 domain。\n\n"
        f"上一次结果：{previous_response}\n\n"
        f"完整输入证据：{json.dumps(request, ensure_ascii=False, indent=2)}"
    )


# ===========================================================================
# dataset
# ===========================================================================

_DATASET_META_SYSTEM_PROMPT = """你是资深数仓与 BI 语义层建模专家。
请根据给定的历史 SQL 查询模式及其依赖的物理表，为每条 SQL 生成语义视图元信息。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - sql: 输入中的 SQL 原文，必须与输入完全一致
   - parents: 输入中的物理表依赖，必须与输入完全一致
   - name: 视图命名，简洁、可读，适合资产目录展示，必须强制使用英文，命名要完全符合当前的parents命名规范
   - description: 对该 SQL 查询意图、业务含义、典型使用场景的详细描述
4. 不允许新增输入中不存在的 SQL
5. 所有输入 SQL 都必须覆盖，顺序可与输入不同
6. 只输出 JSON， 不要 markdown，所有字符串内部双引号必须转义，不要额外解释文本
"""


def build_dataset_meta_system_prompt() -> str:
    return _DATASET_META_SYSTEM_PROMPT


def build_dataset_meta_user_prompt(batch_payload: list[dict[str, Any]]) -> str:
    return (
        "以下是历史 SQL 中结构等价的重复查询模式。"
        "请为每条记录生成视图命名（name）和描述（description）。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}"
    )


# ===========================================================================
# column role classification (metric_caliber phase-2)
#
# dimension_caliber / metric_caliber 的口径行完全由日志证据与源表 schema 确定，
# 不需要 prompt；这两个任务唯一的模型调用就是下面的列角色判定。
# ===========================================================================

#: 列角色的取值域。
#: ``other``：不属于维度/指标/日期的普通列（主键、技术字段、冗余列等）；
#: ``discarded``：日志里抽出来的派生指标候选，但没有业务保留价值，
#: 例如 ``COUNT(*)``、对日期列或主键做 ``MAX/COUNT``。
COLUMN_ROLES = ("dimension", "metric", "date", "other", "discarded")
#: 指标时间范围的取值域，与 metric_formula_lib.date_range 保持一致。
METRIC_DATE_RANGES = ("1d", "nd", "30d", "1m")

_COLUMN_ROLE_SYSTEM_PROMPT = """你是资深数仓与 BI 语义层建模专家。
请根据一张表的列元数据及其在历史 SQL 中的使用证据，判定每一列的角色。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - column_name: 必须与输入完全一致
   - column_role: 只能是 "dimension" / "metric" / "date" / "other" / "discarded" 之一
   - dimension_name: dimension/date 列的规范中文业务名，其他角色必须为 null
   - metric_name: metric 列的规范中文业务名，其他角色必须为 null
   - date_range: metric 列的时间范围，比如 "1d" / "nd" / "30d" / "1m" 或者其他，
     其他角色必须为 null
   - reason: 一句话说明判定依据，不超过 30 个字
   - confidence: 0 到 1 之间的小数，越高表示越有把握
4. 不允许新增输入中不存在的 column_name；必须覆盖全部输入列
5. column_role 的含义：
   - dimension: 用于分组、筛选、下钻的定性字段（地区、类目、状态、ID 维度等）；不含任何时间类字段
   - metric: 可聚合的定量字段（金额、次数、比率等），以及日志中出现的派生指标
   - date: 日期或时间字段（含 date_id / ds / dt 等分区日期，以及非 date 类型但语义为时间的列）
   - other: 以上都不是，例如技术主键、冗余备注、无业务含义的内部字段
   - discarded: 从历史 SQL 里抽出来的派生指标候选，但作为指标没有保留价值
6. 凡表示时间的列必须判为 date，不得判为 dimension：包括 data_type 含 date/time/timestamp、
   或列名/注释指向日期时间（如 ds、dt、date_id、stat_date、create_time、yyyymmdd 等），
   即使它常出现在 GROUP BY / WHERE 里也一律 date
7. 一列只能有一个 column_role；拿不准时选最主要的用途，并调低 confidence
8. 参考 is_dimension_in_query_logs / is_metric_in_query_logs /
   query_log_metric_name / metric_formula / data_type / comment，
   但不要机械照搬：日志证据缺失不代表该列不是维度或指标；
   时间类列即使 is_dimension_in_query_logs=yes 也必须判为 date
9. 派生指标列（is_derived_metric=true 或 data_type=derived）必须先判断是否值得保留：
   - 有明确业务含义、能独立回答业务问题的，判为 metric
   - 只是技术性聚合、留下来也没人会用的，判为 discarded，并在 reason 里说明原因。
     典型的 discarded 情形：
     * COUNT(*) / COUNT(1) 这类纯行数统计
     * 对日期、分区列做 MAX / MIN / COUNT（如 MAX(ds)、COUNT(date_id)）
     * 对主键、ID、哈希、URL、路径等技术字段做的任何聚合
     * 语义与另一个已有指标完全重复、只是换了写法的
10. dimension_name / metric_name 是面向业务人员的规范中文名称：
   - 通常是 column_name、column_comment 或 column_name_cn 的准确且精简翻译
   - 同义字段必须归一为同一个名称，例如 nation 和 country 都输出“国家”
   - 名称中不要保留表前缀、技术后缀或时间范围；时间范围只写入 date_range
   - 例如 monthly_DAU 可输出 metric_name="日活跃度"、date_range="1m"
11. date_range 的含义：
   - 1d: 单个自然日、日快照或当日值
   - nd: 可变 N 日、累计周期，或无法确定固定窗口
   - 30d: 固定滚动 30 日
   - 1m: 单个自然月或月度值
   或者其他潜在的，可以通过列名或描述推断出来的。
   所有 metric 需要选择一个 date_range；证据不足时选择 nd 并降低 confidence
"""


def build_column_role_system_prompt() -> str:
    return _COLUMN_ROLE_SYSTEM_PROMPT


def build_column_role_user_prompt(
    table_name: str,
    columns: list[dict[str, Any]],
) -> str:
    return (
        f"请为表 `{table_name}` 的下列列判定角色"
        "（dimension / metric / date / other / discarded），"
        "生成规范业务名称；每个 metric 还必须判断 date_range；"
        "没有保留价值的派生指标候选请判为 discarded。"
        "请同时给出一句话理由和置信度。\n\n"
        f"{json.dumps(columns, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# name normalization (dimension / metric 聚合前的跨表归一)
# ===========================================================================

_NAME_NORMALIZATION_SYSTEM_PROMPT = """你是资深数仓与 BI 语义层建模专家。
下面是同一个业务域下、由多张表分别命名得到的一组{kind}名称。
由于命名是逐表进行的，同一个业务概念可能被叫成了不同名字，请把它们归一。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - name: 输入中的原名称，必须与输入完全一致
   - canonical_name: 归一后的规范名称
4. 输入里的每个 name 都必须出现且只出现一次，不允许新增输入中没有的 name
5. 判断是否该合并时，要同时看名称、物理列名和列注释：
   - 指向同一业务概念的必须给同一个 canonical_name，
     例如「分区日期」「日期分区」「统计日期」都归一到「日期」
   - 语义确实不同的保持各自独立，canonical_name 就等于原 name，
     例如「创建时间」和「更新时间」不能合并
6. canonical_name 从输入的名称中挑一个最准确、最通用的；只有当所有候选都不够好时
   才另起一个新名称
7. 宁可不合并，也不要把不同概念强行合并
8. 只输出 JSON，所有字符串内部双引号必须转义
"""


def build_name_normalization_system_prompt(kind: str) -> str:
    """``kind`` 取「维度」或「指标」。"""
    return _NAME_NORMALIZATION_SYSTEM_PROMPT.format(kind=kind)


def build_name_normalization_user_prompt(
    kind: str,
    domain_name: str,
    batch_payload: list[dict[str, Any]],
) -> str:
    return (
        f"业务域 `{domain_name or '(未归属业务域)'}` 下的{kind}名称如下，"
        "请给出每个名称归一后的 canonical_name。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# dimension
# ===========================================================================

_DIMENSION_META_SYSTEM_PROMPT = """你是资深数仓与 BI 语义层建模专家。
维度的业务域（domain_name）与规范名称（dimension_name）已经确定，
你只需要为每个维度补充一句准确的业务含义描述。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - domain_name: 必须与输入完全一致
   - dimension_name: 必须与输入完全一致
   - dimension_description: 该维度的业务含义描述，一到两句话
4. 不允许新增输入中不存在的 (domain_name, dimension_name) 组合，
   且输入中的每个组合都必须覆盖，顺序可与输入不同
5. 描述要综合物理列名、列注释和枚举样例，说明这个维度刻画的是什么、
   典型分析场景是什么
6. 不要复述列名，不要编造枚举样例里没有出现的取值
7. 只输出 JSON，所有字符串内部双引号必须转义，不要额外解释文本
"""


def build_dimension_meta_system_prompt() -> str:
    return _DIMENSION_META_SYSTEM_PROMPT


def build_dimension_meta_user_prompt(batch_payload: list[dict[str, Any]]) -> str:
    return (
        "以下是按 (domain_name, dimension_name) 聚合后的业务维度，"
        "同一个维度可能来自多张表的多个同义物理列。"
        "请为每个维度补充 dimension_description。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# metric
# ===========================================================================

_METRIC_SYSTEM_PROMPT = """你是资深数仓与 BI 指标体系设计专家。
指标的业务域（domain_name）与规范名称（metric_name）已经确定，
你只需要为每个指标补充一句准确的业务含义描述。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - domain_name: 必须与输入完全一致
   - metric_name: 必须与输入完全一致
   - metric_description: 该指标的业务含义描述，一到两句话
4. 不允许新增输入中不存在的 (domain_name, metric_name) 组合，
   且输入中的每个组合都必须覆盖，顺序可与输入不同
5. 描述要综合物理列名、列注释、聚合公式和时间范围，说明这个指标度量的是什么、
   典型分析场景是什么
6. 不要复述公式，也不要编造输入里没有的口径
7. 只输出 JSON，所有字符串内部双引号必须转义，不要额外解释文本
"""


def build_metric_system_prompt() -> str:
    return _METRIC_SYSTEM_PROMPT


def build_metric_user_prompt(batch_payload: list[dict[str, Any]]) -> str:
    return (
        "以下是按 (domain_name, metric_name) 聚合后的业务指标，"
        "同一个指标可能来自多张表的多个同义物理列。"
        "请为每个指标补充 metric_description。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


__all__ = [
    "COLUMN_ROLES",
    "build_name_normalization_system_prompt",
    "build_name_normalization_user_prompt",
    "METRIC_DATE_RANGES",
    "build_column_role_system_prompt",
    "build_column_role_user_prompt",
    "build_dataset_meta_system_prompt",
    "build_dataset_meta_user_prompt",
    "build_dimension_meta_system_prompt",
    "build_dimension_meta_user_prompt",
    "build_domain_system_prompt",
    "build_domain_user_prompt",
    "build_metric_system_prompt",
    "build_metric_user_prompt",
]
