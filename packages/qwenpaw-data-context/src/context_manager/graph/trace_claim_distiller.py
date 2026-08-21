"""Trace Claim Distiller — 完整 trace 结束后的 LLM 结构化 claim 提取。

设计原则：
  - **不逐 step 提取**：单步 observation 信息碎片化，完整 trace 链路才能蒸馏出有价值的断言
  - **LLM 一次性阅读**：把 Task → Step → ToolCall（含 observation 属性）全链拼成结构化文本，
    让 LLM 选择性提取 3-8 条有价值的 claim
  - **批量写回**：蒸馏出的 claim 挂到产出该信息的 ToolCall 节点上（``PRODUCES`` 边），
    同时连到对应 Step 节点（``DERIVED_FROM_STEP`` 边），可被 search context 向量召回
  - **跨图边**：LLM 可输出 ``resolved_to`` 指向 MG 节点 key，写回时自动建 ``RESOLVED_TO`` 边

两个入口：
  1. :func:`distill_trace_claims` — 独立函数，手动调用
  2. :func:`auto_distill_on_task_end` — ``TraceRecorder.update_task_status`` 自动异步触发
     （受 ``CFG.trace_auto_distill_claims`` 控制）
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

from neo4j import Driver

from ..config import CFG, llm_thinking_enabled
from ..openai_client import complete_json
from ..utils import neo4j_session

log = logging.getLogger("graph.trace_claim_distiller")


# ---------------------------------------------------------------------- #
# Cypher: 拉完整 trace
# ---------------------------------------------------------------------- #

_FETCH_TRACE_CYPHER = """
MATCH (t:Task {key: $task_key})
OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(p:Step)
WITH t, p ORDER BY coalesce(p.step_idx, 0)
OPTIONAL MATCH (p)-[:EXECUTED_BY]->(tc:ToolCall)
WITH t, p, tc ORDER BY coalesce(p.step_idx, 0), coalesce(tc.ts, datetime('1970-01-01'))
RETURN
  t.goal           AS task_goal,
  t.status         AS task_status,
  t.failure_lesson AS failure_lesson,
  p.key            AS plan_key,
  p.step_idx       AS step_idx,
  p.intent         AS intent,
  p.status         AS plan_status,
  tc.key           AS tc_key,
  tc.tool_name     AS tool_name,
  tc.args_json     AS args_json,
  tc.status        AS tc_status,
  tc.key           AS source_tool_call_key,
  coalesce(tc.observation_summary, '') AS obs_summary,
  coalesce(tc.observations_json, '') AS observations_json
ORDER BY coalesce(p.step_idx, 0), coalesce(tc.ts, datetime('1970-01-01'))
"""


def _fetch_trace(driver: Driver, task_key: str) -> Optional[dict]:
    """从 Neo4j 拉出完整 trace，返回结构化 dict。"""
    with neo4j_session(driver) as s:
        rows = s.run(_FETCH_TRACE_CYPHER, task_key=task_key).data()
    if not rows:
        return None
    first = rows[0]
    goal = first.get("task_goal") or ""
    status = first.get("task_status") or "unknown"
    failure_lesson = first.get("failure_lesson") or ""

    steps: list[dict] = []
    current_plan_key: Optional[str] = None
    current_step: Optional[dict] = None
    obs_counter = 0

    for r in rows:
        pk = r.get("plan_key")
        if pk != current_plan_key:
            if current_step:
                steps.append(current_step)
            current_plan_key = pk
            current_step = {
                "step_idx": r.get("step_idx", 0),
                "plan_key": r.get("plan_key") or "",
                "intent": r.get("intent") or "",
                "status": r.get("plan_status") or "",
                "tool_calls": [],
            }
        if current_step and r.get("tc_key"):
            obs_counter += 1
            current_step["tool_calls"].append({
                "tool_name": r.get("tool_name") or "",
                "args_json": r.get("args_json") or "",
                "status": r.get("tc_status") or "",
                "source_tool_call_key": r.get("source_tool_call_key") or "",
                "obs_summary": r.get("obs_summary") or "",
                "observations_json": r.get("observations_json") or "",
                "obs_idx": obs_counter,
            })

    if current_step:
        steps.append(current_step)

    return {
        "task_key": task_key,
        "goal": goal,
        "status": status,
        "failure_lesson": failure_lesson,
        "steps": steps,
        "obs_count": obs_counter,
    }


# ---------------------------------------------------------------------- #
# Prompt 构建
# ---------------------------------------------------------------------- #

_SYSTEM_PROMPT = (
    "你是一位数据分析 trace 审查专家。你的任务是从一条完整的任务执行轨迹中，"
    "提取有价值的结构化断言（Claims）。\n\n"
    "## 核心原则：宁缺毋滥\n"
    "一条 claim 必须是**非显而易见的、未来可复用的经验**。"
    "如果你看一眼字段名/表名就能猜到含义，那就不值得提取。\n\n"
    "## 只提取以下类型的断言\n"
    "- 语义映射：业务术语对应到非直觉的列名（如 DAU → visit_usercnt_1d，"
    "而非 model_visible_cnt → 可见模型数这种字段名即含义的 trivial 映射）\n"
    "- 口径规则：多维表需全维度 filter、率值需 NULLIF、春节需排除\n"
    "- 踩坑经验：近 N 月 ≠ 滚动 N×30 天、漏锁 rollup 行导致笛卡尔、"
    "Hologres 需排除系统 schema 等\n"
    "- 关键决策：选表 A 而非表 B 的原因、语义层为空时的降级策略\n\n"
    "## 不要提取\n"
    "- 字段名即含义的映射（model_visible_cnt = 可见模型数 → 废话）\n"
    "- 表结构描述（表 X 有 A/B/C 列 → 看 schema 就知道）\n"
    "- 中间过程描述（调用了某个工具、读了某个文件）\n"
    "- 对用户意图的复述\n\n"
    "提取 0-5 条 claims。简单查询（如'随便取一点数'）通常 0-2 条即可，"
    "甚至可以为空数组。每条断言的 confidence 应反映你对该断言正确性的确信度（0-1）。\n\n"
    "输出 JSON 对象，包含 claims 数组：\n"
    '```json\n'
    '{\n'
    '  "claims": [\n'
    "    {\n"
    '      "text": "断言文本（简洁，一句话）",\n'
    '      "confidence": 0.9,\n'
    '      "predicate": "maps_to | asserts | avoids | requires",\n'
    '      "subject_type": "metric_mapping | join_path | caliber_rule | time_parsing | general",\n'
    '      "resolved_to": "MG 节点 key（如 met:ChatApp:DAU），不确定则留空",\n'
    '      "source_obs_idx": 产生此断言的 observation 序号（整数，不确定则为 0）\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "```"
)


_FETCH_EXISTING_CLAIMS_CYPHER = """
MATCH (c:Claim) WHERE c.source = 'trace_distill'
RETURN c.text AS text
ORDER BY c.ingest_at DESC
LIMIT 50
"""


def _fetch_existing_claim_texts(driver: Driver) -> list[str]:
    """Return texts of recently distilled claims for cross-task dedup."""
    try:
        with neo4j_session(driver) as s:
            rows = s.run(_FETCH_EXISTING_CLAIMS_CYPHER).data()
        return [r["text"] for r in rows if r.get("text")]
    except Exception:
        return []


def _build_user_prompt(trace: dict, existing_claims: list[str] | None = None) -> str:
    lines = [
        f"Task: {trace['goal']}",
        f"Status: {trace['status']}",
    ]
    if trace.get("failure_lesson"):
        lines.append(f"Failure lesson: {trace['failure_lesson']}")
    if existing_claims:
        lines.append("")
        lines.append("## 已有断言（不要重复提取语义相同的断言）")
        for i, text in enumerate(existing_claims[:20], 1):
            lines.append(f"  {i}. {text[:200]}")
    lines.append("")
    lines.append("Plans & Steps:")

    for step in trace.get("steps", []):
        lines.append(
            f"  Step {step['step_idx']}: {step['intent']}  [{step['status']}]"
        )
        for tc in step.get("tool_calls", []):
            lines.append(f"    Tool: {tc['tool_name']}  [{tc['status']}]")
            args = tc.get("args_json") or ""
            if args:
                # 截断过长的 args
                args_short = args[:300] + "..." if len(args) > 300 else args
                lines.append(f"    Args: {args_short}")
            obs = tc.get("obs_summary") or ""
            if obs:
                obs_short = obs[:500] + "..." if len(obs) > 500 else obs
                lines.append(f"    Observation (#{tc['obs_idx']}): {obs_short}")

    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# LLM 蒸馏
# ---------------------------------------------------------------------- #

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                    "predicate": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "resolved_to": {"type": "string"},
                    "source_obs_idx": {"type": "integer"},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["claims"],
}


def _call_llm(
    trace: dict,
    *,
    model: Optional[str] = None,
    existing_claims: list[str] | None = None,
) -> list[dict]:
    """调用 LLM 蒸馏 claims，返回 claim dict 列表。"""
    user_prompt = _build_user_prompt(trace, existing_claims)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    sem_think = llm_thinking_enabled("trace_claim_distill", context="agent")

    try:
        parsed = complete_json(
            messages,
            json_schema=_JSON_SCHEMA,
            model=model,
            max_retries=2,
            temperature=0.0,
            enable_thinking=sem_think,
        )
    except Exception as exc:
        log.warning("trace_claim_distiller: LLM call failed: %s", exc)
        return []

    if not isinstance(parsed, dict):
        return []

    raw_claims = parsed.get("claims") or []
    claims: list[dict] = []
    if isinstance(raw_claims, list):
        for c in raw_claims:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            claims.append({
                "text": text[:1500],
                "confidence": float(c.get("confidence", 0.7)),
                "predicate": str(c.get("predicate") or "asserts").strip(),
                "subject_type": str(c.get("subject_type") or "general").strip(),
                "resolved_to": str(c.get("resolved_to") or "").strip(),
                "source_obs_idx": int(c.get("source_obs_idx") or 0),
            })

    return claims


# ---------------------------------------------------------------------- #
# 写回 Neo4j
# ---------------------------------------------------------------------- #

def _resolve_obs_to_nodes(
    trace: dict, obs_idx: int
) -> tuple[str, str]:
    """根据 obs_idx（1-based）找到对应的 ToolCall key 和 Step key。

    Returns:
        ``(tool_call_key, plan_key)`` — 任一为空表示未找到。
    """
    for step in trace.get("steps", []):
        for tc in step.get("tool_calls", []):
            if tc.get("obs_idx") == obs_idx:
                return (
                    tc.get("source_tool_call_key") or "",
                    step.get("plan_key") or "",
                )
    return "", ""


def _write_claims_to_neo4j(
    driver: Driver,
    task_key: str,
    trace: dict,
    claims: list[dict],
) -> list[str]:
    """批量写 Claim 节点 + PRODUCES 边 + DERIVED_FROM_STEP 边 + 可选 RESOLVED_TO 边。

    每个 Claim 通过向量索引（``claim_vec``）可被 search context 召回，
    同时连到产生它的 Step 节点（``DERIVED_FROM_STEP`` 边）以便溯源。
    """
    written_keys: list[str] = []

    from .trace import _safe_embed, _claim_emb_text

    with neo4j_session(driver) as s:
        for claim in claims:
            c_key = f"claim:distilled:{uuid.uuid4().hex[:12]}"
            tool_call_key = ""
            plan_key = ""
            if claim["source_obs_idx"]:
                tool_call_key, plan_key = _resolve_obs_to_nodes(
                    trace, claim["source_obs_idx"]
                )

            emb_text = _claim_emb_text(claim)
            vec, emb_hash = _safe_embed(emb_text)

            # 写 Claim 节点
            s.run(
                """
                MERGE (cl:Claim {key: $key})
                  ON CREATE SET
                    cl.text = $text,
                    cl.confidence = $confidence,
                    cl.predicate = $predicate,
                    cl.subject_type = $subject_type,
                    cl.extractor = 'llm_distiller',
                    cl.source = 'trace_distill',
                    cl.source_trust = 0.7,
                    cl.zone = 'trace',
                    cl.valid_at = datetime(),
                    cl.ingest_at = datetime(),
                    cl.task_key = $task_key,
                    cl.content_hash = '',
                    cl.embedding = CASE WHEN $vec IS NOT NULL THEN $vec ELSE cl.embedding END,
                    cl.embedding_hash = CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE cl.embedding_hash END
                  ON MATCH SET
                    cl.text = $text,
                    cl.confidence = $confidence,
                    cl.embedding = CASE WHEN $emb_hash <> coalesce(cl.embedding_hash, '') AND $vec IS NOT NULL
                                        THEN $vec ELSE cl.embedding END,
                    cl.embedding_hash = CASE WHEN $emb_hash <> coalesce(cl.embedding_hash, '') AND $vec IS NOT NULL
                                             THEN $emb_hash ELSE cl.embedding_hash END
                """,
                key=c_key,
                text=claim["text"],
                confidence=claim["confidence"],
                predicate=claim["predicate"],
                subject_type=claim["subject_type"],
                task_key=task_key,
                vec=vec,
                emb_hash=emb_hash,
            )

            # PRODUCES 边（直接挂到产出该信息的 ToolCall 上）
            if tool_call_key:
                s.run(
                    """
                    MATCH (tc:ToolCall {key: $tool_call_key})
                    MATCH (cl:Claim {key: $claim_key})
                    MERGE (tc)-[r:PRODUCES]->(cl)
                      ON CREATE SET r.extractor = 'llm_distiller'
                    SET cl.source_tool_call_key = tc.key
                    """,
                    tool_call_key=tool_call_key,
                    claim_key=c_key,
                )
            else:
                # 找不到具体结果 → 挂到 Task 的首个 ToolCall。
                s.run(
                    """
                    MATCH (t:Task {key: $task_key})-[:DECOMPOSES_INTO]->(p:Step)
                          -[:EXECUTED_BY]->(tc:ToolCall)
                    WITH tc, p LIMIT 1
                    MATCH (cl:Claim {key: $claim_key})
                    MERGE (tc)-[r:PRODUCES]->(cl)
                      ON CREATE SET r.extractor = 'llm_distiller'
                    SET cl.source_tool_call_key = tc.key
                    """,
                    task_key=task_key,
                    claim_key=c_key,
                )

            # DERIVED_FROM_STEP 边 — 连到对应的 Step 节点
            if plan_key:
                s.run(
                    """
                    MATCH (p:Step {key: $plan_key})
                    MATCH (cl:Claim {key: $claim_key})
                    MERGE (cl)-[r:DERIVED_FROM_STEP]->(p)
                      ON CREATE SET r.source = 'llm_distiller', r.created_at = datetime()
                    """,
                    plan_key=plan_key,
                    claim_key=c_key,
                )

            # RESOLVED_TO 跨图边（如果 LLM 给了 MG 节点 key 且该节点存在）
            resolved = claim.get("resolved_to") or ""
            if resolved:
                s.run(
                    """
                    MATCH (cl:Claim {key: $claim_key})
                    OPTIONAL MATCH (meta {key: $meta_key})
                    FOREACH (_ IN CASE WHEN meta IS NULL THEN [] ELSE [meta] END |
                        MERGE (cl)-[r:RESOLVED_TO]->(meta)
                          ON CREATE SET r.resolver = 'llm_distiller', r.confidence = $confidence
                    )
                    """,
                    claim_key=c_key,
                    meta_key=resolved,
                    confidence=claim["confidence"],
                )

            written_keys.append(c_key)

    return written_keys


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #

def distill_trace_claims(
    driver: Driver,
    task_key: str,
    *,
    model: Optional[str] = None,
) -> list[str]:
    """从 Neo4j 拉完整 trace → LLM 蒸馏 → 批量写回 Claim 节点。

    Args:
        driver: Neo4j driver.
        task_key: Task 节点的 key。
        model: LLM 模型名（默认 CFG.llm_model）。

    Returns:
        写入的 claim key 列表。
    """
    trace = _fetch_trace(driver, task_key)
    if not trace:
        log.warning("distill_trace_claims: no trace found for %s", task_key)
        return []

    obs_count = trace.get("obs_count", 0)
    if obs_count == 0:
        log.info("distill_trace_claims: no observations in %s, skipping", task_key)
        return []

    claims = _call_llm(trace, model=model, existing_claims=_fetch_existing_claim_texts(driver))
    if not claims:
        log.info("distill_trace_claims: LLM returned nothing for %s", task_key)
        return []

    written_claims = _write_claims_to_neo4j(driver, task_key, trace, claims)

    log.info(
        "distill_trace_claims: task=%s obs=%d claims=%d/%d",
        task_key, obs_count,
        len(claims), len(written_claims),
    )
    return written_claims


# ---------------------------------------------------------------------- #
# 自动触发入口（async daemon thread）
# ---------------------------------------------------------------------- #

def _run_with_db_ctx(neo4j_database: Optional[str], fn):
    """Execute ``fn()`` inside the optional ``neo4j_database_ctx`` and return its result."""
    from ..utils import neo4j_database_ctx

    db = (neo4j_database or "").strip()
    if db:
        token = neo4j_database_ctx.set(db)
        try:
            return fn()
        finally:
            neo4j_database_ctx.reset(token)
    return fn()


def _async_distill_worker(
    driver: Driver,
    task_key: str,
    neo4j_database: Optional[str] = None,
) -> None:
    """后台线程：拉 trace → 蒸馏 claims → 写回 → 触发 strategy 蒸馏。

    所有异常静默吞掉（不阻塞 agent）。
    """
    def _work() -> None:
        # 1) 拉 trace + LLM 蒸馏（直接调用底层函数，避免 distill_trace_claims
        #    内部重复 fetch + 重复调用 LLM）
        trace = _fetch_trace(driver, task_key)
        if not trace:
            log.warning("auto_distill worker: no trace for %s", task_key)
            return

        if trace.get("obs_count", 0) == 0:
            log.info("auto_distill worker: no observations in %s", task_key)
            return

        claims = _call_llm(trace, existing_claims=_fetch_existing_claim_texts(driver))

        # 2) 写 claims
        if claims:
            _write_claims_to_neo4j(driver, task_key, trace, claims)

        log.info(
            "auto_distill worker: task=%s claims=%d",
            task_key, len(claims),
        )

        # 3) 触发 strategy 蒸馏（lazy import，避免循环依赖 + 允许该模块未就绪）
        if CFG.trace_auto_distill_strategy:
            try:
                from .claim_strategy_synthesizer import distill_strategy_from_trace
            except ImportError as exc:
                log.debug(
                    "auto_distill worker: strategy synthesizer not available yet (%s)",
                    exc,
                )
            else:
                try:
                    distill_strategy_from_trace(
                        driver, task_key, trace, claims,
                    )
                except Exception as exc:
                    log.warning(
                        "auto_distill worker: strategy distill failed for %s: %s",
                        task_key, exc,
                    )

    try:
        _run_with_db_ctx(neo4j_database, _work)
    except Exception as exc:
        log.warning("auto_distill worker failed for %s: %s", task_key, exc)


def auto_distill_on_task_end(
    driver: Driver,
    task_key: str,
    status: str,
    *,
    neo4j_database: Optional[str] = None,
) -> None:
    """task 结束时异步触发 claim 蒸馏（daemon thread）。

    仅对 success / failed 状态触发。受 ``CFG.trace_auto_distill_claims`` 控制。
    """
    if not CFG.trace_auto_distill_claims:
        return
    if status not in ("success", "failed"):
        return
    t = threading.Thread(
        target=_async_distill_worker,
        args=(driver, task_key, neo4j_database),
        daemon=True,
        name="trace_claim_distill",
    )
    t.start()


__all__ = [
    "distill_trace_claims",
    "auto_distill_on_task_end",
]
