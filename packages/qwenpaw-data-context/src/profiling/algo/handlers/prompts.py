"""四个任务的全部 LLM prompt（仅通过 build 函数对外暴露）。

dataset / column-role /
name-normalization / dimension / metric 五段以旧实现为基础；新增 CALIBER
实体语义匹配段，domain 段按 D2
重写输出契约——不再要求模型生成 ``domain_of`` Python 代码（旧版 AST 校验后
``exec``，是实打实的安全隐患），改为直接给出每个 domain 的成员表清单，
由 handler 物化成 ``DomainSnapshot.members`` 数据行。Domain prompt 允许
按名称和描述复用 workspace 目录；只有确无匹配时才生成小写英文新标识。

------------------------------------------------------------------------
语言
------------------------------------------------------------------------
每段 prompt 有 zh / en 两个版本，builder 按 ``language`` 选取（缺省 zh，
未知语言回落 zh）。prompt 文本参与 LLM 响应缓存摘要，规则调整会触发重新
推理。en 版本供 spider2 这类全英文数据集使用：
输入是英文的，产出（domain / 业务名 / 描述）也必须是英文的。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _pick(texts: Mapping[str, str], language: str) -> str:
    """按语言取文本；未知语言回落 zh —— 宁可中文也不要 KeyError。"""
    return texts.get(language) or texts["zh"]


#: 归一 prompt 里 kind 的展示词。handler 内部统一用 zh 词（「维度」/「指标」）
#: 作为 kind 键，这里只做展示层翻译。
_KIND_WORDS: dict[str, dict[str, str]] = {
    "维度": {"zh": "维度", "en": "dimension"},
    "指标": {"zh": "指标", "en": "metric"},
}


def _kind_word(kind: str, language: str) -> str:
    return _pick(_KIND_WORDS.get(kind, {"zh": kind}), language)


# ===========================================================================
# domain（输出契约按 D2 重写：表清单取代 domain_of 代码）
# ===========================================================================

_DOMAIN_SYSTEM_PROMPT = {
    "zh": """你是数仓领域专家。根据历史 SQL 的 JOIN 关系聚类结果，把输入中的表划分到若干业务域（domain），并给出各 domain 的元信息。

只输出 JSON，不要输出 markdown，不要输出额外解释。格式必须为：
{
  "domains": [
    {
      "domain": "英文业务域标识",
      "display_name": "对外展示的规范名称",
      "description": "对该 domain 的详细含义描述",
      "tables": ["表名1", "表名2"]
    }
  ]
}

对划分的要求：
1. 优先使用 reusable_domains 中语义匹配的已有 domain，并原样返回它的 domain 标识；只有新建 domain 才使用小写英文、数字或下划线
2. tables 只能使用输入中出现的表名，每张表最多归属一个 domain
3. 同一 cluster 内的所有表必须划入同一个 domain
4. 尽可能覆盖输入中的全部表；确实无法归类的表可以不出现
5. 每个 domain 的 tables 不得为空；不允许输出没有成员表的 domain

对 domains 元信息的要求：
1. display_name 适合资产目录/语义层展示；description 说明业务含义
2. 新建 domain 应与 reusable_domains 及本次其他 domain 处于同一抽象层级、相互独立；只有没有合适的已有 domain 时才新建
""",
    "en": """You are a data-warehouse domain expert. Based on the JOIN-relationship clusters mined from historical SQL, assign the input tables to several business domains and provide each domain's metadata.

Output JSON only — no markdown, no extra explanation. The format must be:
{
  "domains": [
    {
      "domain": "english_domain_identifier",
      "display_name": "canonical display name",
      "description": "detailed description of what this domain covers",
      "tables": ["table1", "table2"]
    }
  ]
}

Partitioning rules:
1. Prefer a semantically matching domain from reusable_domains and return its domain identifier verbatim; only newly created domain identifiers must use lowercase letters, digits or underscores
2. tables may only contain table names that appear in the input; each table belongs to at most one domain
3. All tables within the same cluster must be assigned to the same domain
4. Cover as many of the input tables as possible; tables that truly cannot be classified may be omitted
5. Every domain's tables must be non-empty; never output a domain without member tables

Metadata rules:
1. display_name should suit an asset catalog / semantic layer; description explains the business meaning
2. Newly created domains must be parallel to and semantically independent from reusable_domains and the other domains in this result; create one only when no existing domain is suitable

All output values (display_name, description) must be written in English.
""",
}


_DOMAIN_REFINE_SYSTEM_PROMPT = {
    "zh": """你是数仓领域专家。输入是若干业务域草稿（draft_domains），以及可选的未分配表（unassigned_tables）。请为每个草稿完善 domain 标识、display_name 与 description；按成员规则处理 tables。

只输出 JSON，不要输出 markdown，不要输出额外解释。格式必须为：
{
  "domains": [
    {
      "draft_id": "输入中的 draft_id，必须原样回带",
      "domain": "英文业务域标识",
      "display_name": "对外展示的规范名称",
      "description": "对该 domain 的详细含义描述",
      "tables": ["表名1", "表名2"]
    }
  ]
}

要求：
1. 每个输入草稿必须且只能对应一条输出，draft_id 原样回带；不得新增草稿之外的 domain，也不得省略任何草稿
2. 若草稿已有成员表（tables 非空）：tables 必须与该草稿成员完全一致，不得增删或移动
3. 若草稿成员为空：可从 unassigned_tables 中为它分配成员；每张未分配表最多归属一个草稿；确实无法归类的表可以不分配；允许输出仍为空成员的草稿
4. 草稿可以合并到 reusable_domains 中语义匹配的已有 domain，此时必须原样返回已有 domain 标识；只有新建 domain 才使用小写英文、数字或下划线
5. display_name 适合资产目录/语义层展示；description 说明业务含义
6. 新建 domain 应与 reusable_domains 处于同一抽象层级，并且只在没有合适的已有 domain 时新建
""",
    "en": """You are a data-warehouse domain expert. The input is a set of business-domain drafts (draft_domains) and optionally unassigned tables (unassigned_tables). Refine each draft's domain identifier, display_name and description; handle tables per the member rules.

Output JSON only — no markdown, no extra explanation. The format must be:
{
  "domains": [
    {
      "draft_id": "the draft_id from the input, echoed verbatim",
      "domain": "english_domain_identifier",
      "display_name": "canonical display name",
      "description": "detailed description of what this domain covers",
      "tables": ["table1", "table2"]
    }
  ]
}

Rules:
1. Every input draft must map to exactly one output entry with its draft_id echoed verbatim; never add domains beyond the drafts and never omit a draft
2. If a draft already has member tables (non-empty tables): tables must be exactly the draft's member tables — no additions, removals or moves
3. If a draft has empty members: you may assign tables from unassigned_tables; each unassigned table may belong to at most one draft; tables that cannot be classified may be left unassigned; empty-member drafts are allowed
4. A draft may merge into a semantically matching domain from reusable_domains, in which case the existing identifier must be returned verbatim; only newly created identifiers must use lowercase letters, digits or underscores
5. display_name should suit an asset catalog / semantic layer; description explains the business meaning
6. Newly created domains must remain at the same abstraction level as reusable_domains and may be created only when no existing domain is suitable

All output values (display_name, description) must be written in English.
""",
}


_DOMAIN_ASSIGN_SYSTEM_PROMPT = {
    "zh": """你是数仓领域专家，为 Dataset 建模补充表的业务域归属。
根据输入的表名、描述、JOIN 聚类和正式 Domain 目录分配归属。
只输出 JSON：{"domains": [{"domain": "正式 canonical 标识", "tables": ["输入表名"]}]}。
1. 只能选择 reusable_domains 中的正式 Domain，必须原样返回其 canonical 标识；禁止新增或改名
2. tables 只能包含本轮输入表；每张表最多属于一个 Domain，同一 cluster 的表必须同域
3. 尽量为每张输入表选择语义合适的 Domain；没有合适选项的表可以不出现，不得强行归类
4. 每条输出必须有有效成员；不得复制历史成员、修改已有 Domain 元信息
""",
    "en": """You are a data-warehouse expert assigning tables to domains for Dataset modeling.
Use table names, descriptions, JOIN clusters, and the formal Domain catalog.
Return JSON only: {"domains": [{"domain": "formal canonical identifier", "tables": ["input table"]}]}.
1. Select only formal Domains from reusable_domains and return their canonical identifiers verbatim; never create or rename a Domain
2. Use only the supplied tables, with at most one Domain per table; tables in the same cluster must share a Domain
3. Try to assign every table to a semantically suitable Domain; omit tables without a suitable option rather than forcing a match
4. Each output needs valid members; never copy historical members or modify formal Domain metadata
""",
}


def build_domain_system_prompt(
    language: str = "zh",
    *,
    mode: str = "create",
    existing_only: bool = False,
) -> str:
    """Domain create/refine 保持原行为；Dataset 补分配只允许正式目录。"""
    if existing_only:
        return _pick(_DOMAIN_ASSIGN_SYSTEM_PROMPT, language)
    texts = _DOMAIN_REFINE_SYSTEM_PROMPT if mode == "refine" else _DOMAIN_SYSTEM_PROMPT
    return _pick(texts, language)


#: Workspace 内可复用 Domain 目录。只有名称与元信息，不携带其他
#: 数据源的成员表；当前数据源已确认的成员锁定由 handler 确定性执行。
_DOMAIN_CONTEXT_RULES = {
    "zh": """以下是当前 workspace 内可复用的业务域（reusable_domains）：
1. 若本次输入表的语义匹配某个已有 domain，优先复用它，并原样返回其 domain 标识
2. 复用时 tables 仍只能列出本次输入中待归属的表，不要补写历史成员
3. 只有没有合适的已有 domain 时才新建；新建 domain 不得与已有 domain 重名或语义重叠

可复用域：
{context}

""",
    "en": """Below are the reusable business domains in the current workspace (reusable_domains):
1. If an input table semantically matches an existing domain, prefer reusing it and return its domain identifier verbatim
2. When reusing a domain, tables may contain only the currently supplied tables awaiting assignment; never reproduce historical members
3. Create a new domain only when none is suitable; a new domain must not duplicate or semantically overlap an existing one

Reusable domains:
{context}

""",
}


_DOMAIN_ASSIGN_CONTEXT_RULES = {
    "zh": """以下 reusable_domains 是当前 workspace 的正式 Domain 目录，也是唯一允许的选项。
只能原样复用 canonical 标识；无合适选项时保留未分配，不得新增 Domain 或强行归类。

可复用域：
{context}

""",
    "en": """The following reusable_domains are the workspace's formal Domain catalog and the only allowed choices.
Reuse canonical identifiers verbatim. Leave unmatched tables unassigned; never create Domains or force a match.

Reusable domains:
{context}

""",
}


def _domain_context_block(
    context: Any, language: str, *, existing_only: bool = False
) -> str:
    if not context:
        return ""
    rules = _DOMAIN_ASSIGN_CONTEXT_RULES if existing_only else _DOMAIN_CONTEXT_RULES
    return _pick(rules, language).format(
        context=json.dumps(context, ensure_ascii=False, indent=2)
    )


def build_domain_user_prompt(
    request: Any,
    language: str = "zh",
    *,
    context: Any | None = None,
    mode: str = "create",
    existing_only: bool = False,
) -> str:
    block = _domain_context_block(context, language, existing_only=existing_only)
    body = json.dumps(request, ensure_ascii=False, indent=2)
    if mode == "refine":
        if language == "en":
            return (
                f"{block}"
                "Below are the business-domain drafts to refine (and optional "
                "unassigned_tables). Refine each draft's domain identifier, "
                "display_name and description. Keep non-empty member tables "
                "unchanged; empty-member drafts may take tables from "
                "unassigned_tables.\n\n"
                f"{body}"
            )
        return (
            f"{block}"
            "以下是待完善的业务域草稿（及可选的 unassigned_tables）。"
            "请为每个草稿完善 domain 标识、display_name 与 description；"
            "已有成员表保持不变，零成员草稿可从 unassigned_tables 分配表。\n\n"
            f"{body}"
        )
    if language == "en":
        return (
            f"{block}"
            "Below are the JOIN clusters and the unclustered tables. "
            "Assign these tables to business domains and provide each "
            "domain's metadata.\n\n"
            f"{body}"
        )
    return (
        f"{block}"
        "以下是 JOIN 聚类与未聚类表信息。"
        "请把这些表划分到若干业务域，并给出各 domain 的元信息。\n\n"
        f"{body}"
    )


def build_domain_retry_prompt(
    request: Any,
    unmapped: list[str] | tuple[str, ...],
    previous_response: str,
    *,
    previous_error: str | None = None,
    language: str = "zh",
    context: Any | None = None,
    mode: str = "create",
    existing_only: bool = False,
) -> str:
    """构造校验失败或覆盖不足后的完整重试请求，而不是仅请求增量。

    ``unmapped`` 在 create 模式是未覆盖的表，在 refine 模式是未回带的 draft_id。
    """
    block = _domain_context_block(context, language, existing_only=existing_only)
    body = json.dumps(request, ensure_ascii=False, indent=2)
    missing = json.dumps(list(unmapped), ensure_ascii=False)
    if language == "en":
        if previous_error:
            reason = (
                "The previous domain output failed validation. Fix it per the "
                "error below and return the complete JSON (all domains with their "
                "tables) — do not return a delta.\n"
                f"Previous error: {previous_error}\n\n"
            )
        elif existing_only:
            reason = (
                "Some tables still lack a formal Domain. Recheck the complete input and return the complete JSON, "
                "not a delta. Do not force a match or invent a Domain if none is suitable.\n"
                f"Uncovered tables: {missing}\n\n"
            )
        elif mode == "refine":
            reason = (
                "The previous refinement omitted some drafts. Return the corrected "
                "complete JSON covering every draft — do not return a delta.\n"
                f"Missing draft_ids: {missing}\n\n"
            )
        else:
            reason = (
                "The previous domain partition did not reach the required 80% "
                "table coverage. Return the corrected complete JSON (all domains "
                "with their tables) — do not return a delta.\n"
                f"Uncovered tables: {missing}\n\n"
            )
        return (
            f"{block}"
            f"{reason}"
            f"Previous response: {previous_response}\n\n"
            f"Full input evidence: {body}"
        )
    if previous_error:
        reason = (
            "上一次 domain 输出校验失败，请根据报错修正后返回完整 JSON"
            "（含全部 domains 及其 tables），不要只返回增量。\n"
            f"上次报错：{previous_error}\n\n"
        )
    elif existing_only:
        reason = (
            "仍有表未能归属到正式 Domain。请核查完整输入并返回完整 JSON，不要只返回增量。"
            "若没有合适的正式域，不得强行归类或新增 Domain。\n"
            f"未覆盖表：{missing}\n\n"
        )
    elif mode == "refine":
        reason = (
            "上一次完善遗漏了部分草稿。请返回覆盖全部草稿的完整 JSON，不要只返回增量。\n"
            f"遗漏的 draft_id：{missing}\n\n"
        )
    else:
        reason = (
            "上一次 domain 划分没有达到 80% 表覆盖率要求。"
            "请返回修正后的完整 JSON（含全部 domains 及其 tables），不要只返回增量。\n"
            f"未覆盖表：{missing}\n\n"
        )
    return f"{block}{reason}上一次结果：{previous_response}\n\n完整输入证据：{body}"


# ===========================================================================
# dataset
# ===========================================================================

_DATASET_META_SYSTEM_PROMPT = {
    "zh": """你是资深数仓与 BI 语义层建模专家。
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
""",
    "en": """You are a senior data-warehouse and BI semantic-layer modeling expert.
Given recurring historical SQL query patterns and the physical tables they depend on, generate semantic-view metadata for each SQL.

Requirements:
1. Output JSON only — no markdown, no extra explanation
2. The JSON top level must be an array
3. Every element must contain the following fields:
   - sql: the SQL text from the input, exactly as given
   - parents: the physical-table dependencies from the input, exactly as given
   - name: the view name — concise, readable, suitable for an asset catalog, in English, following the naming convention of its parents
   - description: a detailed description of the query intent, business meaning and typical usage scenarios
4. Do not invent SQL that is not in the input
5. Every input SQL must be covered; order may differ from the input
6. JSON only, no markdown; double quotes inside strings must be escaped; no extra explanatory text

All output values (name, description) must be written in English.
""",
}


def build_dataset_meta_system_prompt(language: str = "zh") -> str:
    return _pick(_DATASET_META_SYSTEM_PROMPT, language)


def build_dataset_meta_user_prompt(
    batch_payload: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    if language == "en":
        return (
            "Below are structurally equivalent recurring query patterns from "
            "historical SQL. Generate a view name (name) and description "
            "(description) for each record.\n\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}"
        )
    return (
        "以下是历史 SQL 中结构等价的重复查询模式。"
        "请为每条记录生成视图命名（name）和描述（description）。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}"
    )


# ===========================================================================
# column role classification (CALIBER 的本源角色/口径抽取调用)
# ===========================================================================

_COLUMN_ROLE_SYSTEM_PROMPT = {
    "zh": """你是资深数仓与 BI 语义层建模专家。
请根据一张表的列元数据及其在历史 SQL 中的使用证据，判定每一列的角色。

要求：
1. 仅输出 JSON，不要输出 markdown，不要输出额外解释
2. JSON 顶层必须是一个数组
3. 数组中每个元素必须包含以下字段：
   - column_name: 必须与输入完全一致
   - column_role: 只能是 "dimension" / "metric" / "date" / "other" / "discarded" 之一
   - dimension_name: dimension/date 列的规范中文业务名，其他角色必须为 null
   - metric_name: metric 列的规范中文业务名，其他角色必须为 null
   - time_window: metric 列的统计时间窗口，只能是 "1d" / "7d" / "30d" / "mtd" 之一，
     其他角色必须为 null
   - additivity: metric 列的可加性，只能是 "additive" / "semi_additive" / "non_additive" 之一，
     其他角色必须为 null
   - expr: metric 列在本表上的计算 SQL 片段（聚合表达式，如 "SUM(amount)"、
     "COUNT(DISTINCT user_id)"、"SUM(a) / SUM(b)"），只能引用本表的列名；
     输入里带 metric_formula 的列直接沿用 metric_formula；其他角色必须为 null
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
   - 与已有指标语义相同不是 discarded：仍应判为 metric，后续会复用已有指标的规范名称
10. dimension_name / metric_name 是面向业务人员的规范中文名称：
   - 通常是 column_name、column_comment 或 column_name_cn 的准确且精简翻译
   - 同义字段必须归一为同一个名称，例如 nation 和 country 都输出“国家”
   - 名称中不要保留表前缀、技术后缀或时间范围；时间范围只写入 time_window
   - 例如 monthly_DAU 可输出 metric_name="日活跃度"、time_window="mtd"
11. time_window 的含义（必须四选一，不允许其他取值）：
   - 1d: 单个自然日、日快照或当日值
   - 7d: 固定滚动 7 日 / 周度值
   - 30d: 固定滚动 30 日
   - mtd: 月初至今（month-to-date）、自然月或月度值
   所有 metric 必须从这四个值里选择最接近的一个；证据不足时按列的业务语义
   选最可能的窗口（日粒度事实通常是 1d）并降低 confidence
12. expr 的写法：与 additivity 自洽 —— additive 通常是 SUM(列)，non_additive 的
   比率/均值写成 AVG(列) 或 SUM(a) / SUM(b)，去重计数写 COUNT(DISTINCT 列)；
   明细级数值字段（单价、时长等）按其最常见的分析口径给聚合（如 SUM / AVG），
   不要输出裸列名；不要引用输入里不存在的列
13. additivity 的含义（metric 必填，三选一）：
   - additive: 可以跨任意维度求和（金额、次数、销量等流量型指标）
   - semi_additive: 只能在部分维度上求和，跨时间只能取端点或平均
     （余额、库存、在手订单数等存量型指标）
   - non_additive: 不能直接求和（比率、占比、平均值、去重计数等）
""",
    "en": """You are a senior data-warehouse and BI semantic-layer modeling expert.
Given the column metadata of a table and its usage evidence from historical SQL, determine the role of every column.

Requirements:
1. Output JSON only — no markdown, no extra explanation
2. The JSON top level must be an array
3. Every element must contain the following fields:
   - column_name: exactly as given in the input
   - column_role: one of "dimension" / "metric" / "date" / "other" / "discarded"
   - dimension_name: the canonical English business name for dimension/date columns; must be null for other roles
   - metric_name: the canonical English business name for metric columns; must be null for other roles
   - time_window: the statistical time window of a metric column, strictly one of "1d" / "7d" / "30d" / "mtd";
     must be null for other roles
   - additivity: the additivity of a metric column, strictly one of "additive" / "semi_additive" / "non_additive";
     must be null for other roles
   - expr: the SQL fragment computing this metric on this table (an aggregate expression such as
     "SUM(amount)", "COUNT(DISTINCT user_id)", "SUM(a) / SUM(b)"), referencing only columns of this table;
     when the input carries metric_formula, reuse it verbatim; must be null for other roles
   - reason: a one-sentence justification, at most 20 words
   - confidence: a decimal between 0 and 1; higher means more certain
4. Do not invent column_name values that are not in the input; every input column must be covered
5. Meaning of column_role:
   - dimension: qualitative fields used for grouping, filtering, drill-down (region, category, status, ID dimensions, ...); never any time-related field
   - metric: aggregatable quantitative fields (amounts, counts, ratios, ...), plus derived metrics observed in query logs
   - date: date or time fields (including partition dates such as date_id / ds / dt, and columns whose semantics are temporal even if the data type is not date)
   - other: none of the above, e.g. technical primary keys, redundant remarks, internal fields without business meaning
   - discarded: derived-metric candidates extracted from historical SQL that are not worth keeping as metrics
6. Any column that represents time must be classified as date, never dimension: this includes data_type containing date/time/timestamp, or names/comments indicating datetime (ds, dt, date_id, stat_date, create_time, yyyymmdd, ...), even if it frequently appears in GROUP BY / WHERE
7. Each column has exactly one column_role; when unsure, pick the dominant usage and lower the confidence
8. Consider is_dimension_in_query_logs / is_metric_in_query_logs /
   query_log_metric_name / metric_formula / data_type / comment,
   but do not follow them mechanically: missing log evidence does not mean a column is not a dimension or metric;
   time-related columns must be date even when is_dimension_in_query_logs=yes
9. Derived-metric columns (is_derived_metric=true or data_type=derived) must first be judged for retention value:
   - if it has clear business meaning and can answer a business question on its own, classify as metric
   - if it is merely a technical aggregation nobody would use, classify as discarded and explain why in reason.
     Typical discarded cases:
     * pure row counts such as COUNT(*) / COUNT(1)
     * MAX / MIN / COUNT over dates or partition columns (e.g. MAX(ds), COUNT(date_id))
     * any aggregation over primary keys, IDs, hashes, URLs, paths and other technical fields
   - Semantic equivalence to an existing metric is not a reason to discard it: classify it as
     metric; a later step will reuse the existing metric's canonical name
10. dimension_name / metric_name are canonical English names for business users:
   - usually an accurate and concise rendering of column_name, column_comment or column_name_cn
   - synonymous fields must be normalized to the same name, e.g. nation and country both output "country"
   - do not keep table prefixes, technical suffixes or time windows in the name; the time window goes only into time_window
   - e.g. monthly_DAU may output metric_name="daily active users", time_window="mtd"
11. Meaning of time_window (strictly one of the four values, nothing else):
   - 1d: a single calendar day, daily snapshot or same-day value
   - 7d: fixed rolling 7 days / weekly value
   - 30d: fixed rolling 30 days
   - mtd: month-to-date, a calendar month or monthly value
   Every metric must pick the closest of these four; when evidence is insufficient
   pick the most plausible window for the column's business semantics
   (daily-grain facts are usually 1d) and lower the confidence
12. How to write expr: consistent with additivity — additive is usually SUM(col); ratios/averages that are
   non_additive are AVG(col) or SUM(a) / SUM(b); distinct counts are COUNT(DISTINCT col);
   detail-level numeric fields (unit price, duration, ...) get their most common analytical aggregate
   (SUM / AVG); never output a bare column name; never reference columns absent from the input
13. Meaning of additivity (required for every metric, one of three):
   - additive: can be summed across any dimension (amounts, counts, sales volumes and other flow metrics)
   - semi_additive: can be summed only across some dimensions; across time take endpoints or averages
     (balances, inventory, open orders and other stock metrics)
   - non_additive: cannot be summed directly (ratios, percentages, averages, distinct counts)
""",
}


def build_column_role_system_prompt(language: str = "zh") -> str:
    return _pick(_COLUMN_ROLE_SYSTEM_PROMPT, language)


def build_column_role_user_prompt(
    table_name: str,
    columns: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    if language == "en":
        return (
            f"Determine the role of each column of table `{table_name}` "
            "(dimension / metric / date / other / discarded) and produce the "
            "canonical business names; every metric also needs a time_window "
            "(1d/7d/30d/mtd), an additivity and an expr (aggregate SQL fragment); "
            "derived-metric candidates without retention value must be "
            "discarded. Give a one-sentence reason and a confidence for each.\n\n"
            f"{json.dumps(columns, ensure_ascii=False, indent=2, default=str)}"
        )
    return (
        f"请为表 `{table_name}` 的下列列判定角色"
        "（dimension / metric / date / other / discarded），"
        "生成规范业务名称；每个 metric 还必须判断 time_window（1d/7d/30d/mtd 四选一）、"
        "additivity（additive/semi_additive/non_additive 三选一）与 expr（聚合 SQL 片段）；"
        "没有保留价值的派生指标候选请判为 discarded。"
        "请同时给出一句话理由和置信度。\n\n"
        f"{json.dumps(columns, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# semantic entity matching (CALIBER 跨表 / 跨数据源 canonicalization)
# ===========================================================================

_SEMANTIC_ENTITY_MATCH_SYSTEM_PROMPT = {
    "zh": """你是数仓语义层建模专家。请在同一个业务域和同一种实体类型内，将本轮抽取的候选名称对齐为最终规范名称。

仅输出 JSON，不要输出 markdown 或额外解释。JSON 顶层必须是数组：
[
  {"name": "输入候选名称，必须原样返回", "canonical_name": "最终规范名称"}
]

规则：
1. 每个输入候选名称必须且只能出现一次，不得新增或遗漏 name
2. 优先复用 reusable_entities 中语义匹配的已有实体；命中时 canonical_name 必须原样返回其 name
3. local_pinned_names 是当前数据源已确认但尚未正式落库的名称；语义命中时必须原样复用
4. 多个候选表示同一业务概念时，必须输出同一 canonical_name
5. 只有无匹配的已有或 pinned 实体时才保留或产生新名称；新名称应简洁、准确且面向业务人员
6. 不得跨业务域或跨实体类型匹配；本请求已限定为单一业务域和单一类型
7. 未复用既有标识时，新的 canonical_name 必须使用中文业务名
""",
    "en": """You are a data-warehouse semantic-layer modeling expert. Within one business domain and one entity kind, align the names extracted in this run to their final canonical names.

Output JSON only, with no markdown or extra explanation. The JSON top level must be an array:
[
  {"name": "input candidate name, echoed verbatim", "canonical_name": "final canonical name"}
]

Rules:
1. Every input candidate name must appear exactly once; do not invent or omit name values
2. Prefer a semantically matching entity from reusable_entities; when one matches, canonical_name must reproduce its name verbatim
3. local_pinned_names are confirmed names from the current data source that have not yet been persisted as formal entities; reuse a matching pinned name verbatim
4. Candidates representing the same business concept must receive the same canonical_name
5. Retain or create a new concise business-facing name only when no reusable or pinned entity matches
6. Never match across domains or entity kinds; this request is already scoped to exactly one domain and one kind
7. When no existing identifier is reused, the new canonical_name must be written in English
""",
}


def build_semantic_entity_match_system_prompt(
    kind: str,
    language: str = "zh",
) -> str:
    """候选名称与已有 Metric / Dimension 实体的语义对齐 prompt。"""
    kind_word = _kind_word(kind, language)
    base = _pick(_SEMANTIC_ENTITY_MATCH_SYSTEM_PROMPT, language)
    if language == "en":
        return f"{base}\nEntity kind: {kind_word}."
    return f"{base}\n实体类型：{kind_word}。"


def build_semantic_entity_match_user_prompt(
    kind: str,
    domain_name: str,
    candidates: list[dict[str, Any]],
    reusable_entities: list[dict[str, Any]],
    local_pinned_names: list[str],
    language: str = "zh",
) -> str:
    """用显式分区的 JSON 载荷区分本源候选、全局目录与本源 pinned 名称。"""
    payload = {
        "domain_name": domain_name,
        "entity_kind": _kind_word(kind, language),
        "candidates": candidates,
        "reusable_entities": reusable_entities,
        "local_pinned_names": local_pinned_names,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if language == "en":
        return (
            "Align every candidate name below. Reuse an existing or pinned "
            "canonical name whenever its business meaning matches.\n\n"
            f"{body}"
        )
    return (
        "请对齐以下每个候选名称。语义匹配时优先复用已有或 pinned 的"
        f"规范名称。\n\n{body}"
    )


# ===========================================================================
# name normalization (dimension / metric 聚合前的跨表归一)
# ===========================================================================

_NAME_NORMALIZATION_SYSTEM_PROMPT = {
    "zh": """你是资深数仓与 BI 语义层建模专家。
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
""",
    "en": """You are a senior data-warehouse and BI semantic-layer modeling expert.
Below is a set of {kind} names under the same business domain, produced table by table.
Because naming was done per table, the same business concept may have received different names. Normalize them.

Requirements:
1. Output JSON only — no markdown, no extra explanation
2. The JSON top level must be an array
3. Every element must contain the following fields:
   - name: the original name from the input, exactly as given
   - canonical_name: the normalized canonical name
4. Every input name must appear exactly once; do not invent names that are not in the input
5. When deciding whether to merge, consider the name, the physical column names and the column comments together:
   - names pointing to the same business concept must share one canonical_name,
     e.g. "partition date", "date partition" and "stat date" all normalize to "date"
   - names with genuinely different semantics stay separate, canonical_name equals the original name,
     e.g. "created time" and "updated time" must not be merged
6. Pick the most accurate and most common candidate from the input names as canonical_name; only coin a new name when none of the candidates is good enough
7. Prefer not merging over force-merging different concepts
8. JSON only; double quotes inside strings must be escaped

All canonical_name values must be written in English.
""",
}


def build_name_normalization_system_prompt(kind: str, language: str = "zh") -> str:
    """``kind`` 取「维度」或「指标」（内部键恒为中文词，展示按语言翻译）。"""
    return _pick(_NAME_NORMALIZATION_SYSTEM_PROMPT, language).format(
        kind=_kind_word(kind, language)
    )


def build_name_normalization_user_prompt(
    kind: str,
    domain_name: str,
    batch_payload: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    if language == "en":
        return (
            f"Below are the {_kind_word(kind, language)} names under business "
            f"domain `{domain_name or '(no domain)'}`. Give the canonical_name "
            "for each name.\n\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
        )
    return (
        f"业务域 `{domain_name or '(未归属业务域)'}` 下的{kind}名称如下，"
        "请给出每个名称归一后的 canonical_name。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# dimension
# ===========================================================================

_DIMENSION_META_SYSTEM_PROMPT = {
    "zh": """你是资深数仓与 BI 语义层建模专家。
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
""",
    "en": """You are a senior data-warehouse and BI semantic-layer modeling expert.
The business domain (domain_name) and canonical name (dimension_name) of each dimension are already fixed;
your only job is to add an accurate business-meaning description for each dimension.

Requirements:
1. Output JSON only — no markdown, no extra explanation
2. The JSON top level must be an array
3. Every element must contain the following fields:
   - domain_name: exactly as given in the input
   - dimension_name: exactly as given in the input
   - dimension_description: one or two sentences describing the business meaning of the dimension
4. Do not invent (domain_name, dimension_name) pairs that are not in the input, and every input pair must be covered; order may differ from the input
5. The description should synthesize the physical column names, column comments and enum samples, explaining what the dimension characterizes and its typical analysis scenarios
6. Do not merely restate column names; do not fabricate values absent from the enum samples
7. JSON only; double quotes inside strings must be escaped; no extra explanatory text

All dimension_description values must be written in English.
""",
}


def build_dimension_meta_system_prompt(language: str = "zh") -> str:
    return _pick(_DIMENSION_META_SYSTEM_PROMPT, language)


def build_dimension_meta_user_prompt(
    batch_payload: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    if language == "en":
        return (
            "Below are business dimensions aggregated by (domain_name, "
            "dimension_name); one dimension may come from several synonymous "
            "physical columns across tables. Add a dimension_description for "
            "each.\n\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
        )
    return (
        "以下是按 (domain_name, dimension_name) 聚合后的业务维度，"
        "同一个维度可能来自多张表的多个同义物理列。"
        "请为每个维度补充 dimension_description。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


# ===========================================================================
# metric
# ===========================================================================

_METRIC_SYSTEM_PROMPT = {
    "zh": """你是资深数仓与 BI 指标体系设计专家。
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
""",
    "en": """You are a senior data-warehouse and BI metric-system design expert.
The business domain (domain_name) and canonical name (metric_name) of each metric are already fixed;
your only job is to add an accurate business-meaning description for each metric.

Requirements:
1. Output JSON only — no markdown, no extra explanation
2. The JSON top level must be an array
3. Every element must contain the following fields:
   - domain_name: exactly as given in the input
   - metric_name: exactly as given in the input
   - metric_description: one or two sentences describing the business meaning of the metric
4. Do not invent (domain_name, metric_name) pairs that are not in the input, and every input pair must be covered; order may differ from the input
5. The description should synthesize the physical column names, column comments, aggregation formulas and time windows, explaining what the metric measures and its typical analysis scenarios
6. Do not merely restate the formula; do not fabricate calibers absent from the input
7. JSON only; double quotes inside strings must be escaped; no extra explanatory text

All metric_description values must be written in English.
""",
}


def build_metric_system_prompt(language: str = "zh") -> str:
    return _pick(_METRIC_SYSTEM_PROMPT, language)


def build_metric_user_prompt(
    batch_payload: list[dict[str, Any]],
    language: str = "zh",
) -> str:
    if language == "en":
        return (
            "Below are business metrics aggregated by (domain_name, "
            "metric_name); one metric may come from several synonymous "
            "physical columns across tables. Add a metric_description for "
            "each.\n\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
        )
    return (
        "以下是按 (domain_name, metric_name) 聚合后的业务指标，"
        "同一个指标可能来自多张表的多个同义物理列。"
        "请为每个指标补充 metric_description。\n\n"
        f"{json.dumps(batch_payload, ensure_ascii=False, indent=2, default=str)}"
    )


__all__ = [
    "build_column_role_system_prompt",
    "build_column_role_user_prompt",
    "build_dataset_meta_system_prompt",
    "build_dataset_meta_user_prompt",
    "build_dimension_meta_system_prompt",
    "build_dimension_meta_user_prompt",
    "build_domain_retry_prompt",
    "build_domain_system_prompt",
    "build_domain_user_prompt",
    "build_metric_system_prompt",
    "build_metric_user_prompt",
    "build_name_normalization_system_prompt",
    "build_name_normalization_user_prompt",
    "build_semantic_entity_match_system_prompt",
    "build_semantic_entity_match_user_prompt",
]
