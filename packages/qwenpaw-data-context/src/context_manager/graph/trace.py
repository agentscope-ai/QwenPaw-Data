"""Trace Graph ingester（``graph_topology_v4.md`` 轨迹区）。

离线 ``ingest_trace`` 读 ``trace_tasks.yaml``（可选 ``trace_bridge_links``），写入：

- ``Task -[:DECOMPOSES_INTO {order}]-> Step``、``Step -[:NEXT]-> Step``、``Step -[:EXECUTED_BY]-> ToolCall``、
  ``ToolCall -[:PRODUCES]-> Claim``；observation 作为 ToolCall 数组属性保存
- 可选 ``Turn`` 链：``Turn -[:NEXT]-> Turn``、``Turn -[:SPAWNS]-> Task``（YAML ``turns``）
- 可选 ``Experience``：``Experience -[:DERIVED_FROM]-> Task``，及 ``DISCOVERED`` → 已有 MG 结点 key

跨图（端点须已存在）：

- ``Claim -[:RESOLVED_TO]-> Metric / Column / …``
- ``Observation|Claim -[:EVIDENCED_BY]-> Event | Entity``（``evidenced_by_kg`` 为 key）

``trace_bridge_links`` 在 v4 不建 ``BRIDGES_TO``，仅日志。

在线 API：:class:`TraceRecorder`。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import Driver

from ..config import CFG
from ..utils import get_logger, neo4j_session
from .keys import experience_key, turn_key
from .knowledge import merge_topology_bridge_links

log = get_logger("graph.trace")


# ---------------------------------------------------------------------- #
# Embedding helpers (spec: embedding-ingest §3.2-A)
# ---------------------------------------------------------------------- #

def _embedding_hash(text: str) -> str:
    """Compute idempotent hash = sha1(model_name + SEP + text)."""
    import hashlib
    h = hashlib.sha1()
    h.update((CFG.embed_model or "unknown").encode("utf-8"))
    h.update(b"\x1f")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _safe_embed(text: str) -> tuple[list[float] | None, str]:
    """Embed text with error tolerance. Returns (vec_or_None, hash)."""
    if not text:
        return None, ""
    h = _embedding_hash(text)
    try:
        from ..embedder import embed_one
        vec = embed_one(text)
        return vec, h
    except Exception as exc:
        log.warning("trace embedding failed (text=%.80s…): %s", text, exc)
        return None, ""


def _task_emb_text(task: dict) -> str:
    goal = (task.get("goal") or "").strip()
    sig = (task.get("task_signature") or "").strip()[:32]
    return f"Task: {goal}" + (f" [{sig}]" if sig else "")


def _plan_emb_text(plan: dict) -> str:
    intent = (plan.get("intent") or "").strip()
    hint = (plan.get("tool_hint") or "").strip()
    text = f"Step: {intent}"
    if hint:
        text += f" (tool: {hint})"
    return text


def _turn_emb_text(row: dict) -> str:
    role = (row.get("role") or "").strip()
    content = (row.get("content") or "").strip()[:500]
    return f"{role}: {content}" if content else f"{role}:"


def _experience_emb_text(exp: dict) -> str:
    outcome = (exp.get("outcome") or "").strip()
    insight = (exp.get("key_insight") or exp.get("summary") or "").strip()
    return f"Experience [{outcome}]: {insight}"


def _tc_emb_text(tc: dict) -> str:
    name = (tc.get("tool_name") or "").strip()
    args = (tc.get("args_json") or "").strip()[:200]
    status = (tc.get("status") or "").strip()
    return f"ToolCall: {name}({args}) [{status}]"


def _obs_emb_text(obs: dict) -> str:
    summary = (obs.get("summary") or "").strip()[:500]
    return f"Observation: {summary}"


def _claim_emb_text(claim: dict) -> str:
    text = (claim.get("text") or "").strip()
    predicate = (claim.get("predicate") or "").strip()
    return f"Claim [{predicate}]: {text}"


def _iso(value: Any) -> str:
    """把 ``datetime`` / ``date`` / 字符串统一成 Neo4j ``datetime()`` 能解析的 ISO 8601。

    PyYAML 会把 ISO 字符串自动解析成 ``datetime``；``str()`` 会得到 ``"YYYY-MM-DD HH:MM:SS+TZ"``
    （空格分隔），Neo4j 的 ``datetime()`` 不接受空格——必须是 ``T``。这里统一走 ``.isoformat()``。
    返回 ``""`` 表示「无值」，由调用方决定是否塞进图。
    """
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return ""
    # `2026-03-05 14:22:00+08:00` → `2026-03-05T14:22:00+08:00`
    if len(s) >= 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    return s


# ---------------------------------------------------------------------- #
# 数据加载
# ---------------------------------------------------------------------- #
def load_trace_yaml(path: Path) -> dict[str, Any]:
    """读轨迹相关 YAML（tasks / trace_bridge_links 等）。"""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load trace YAML. Add `pyyaml>=6.0` to requirements.txt."
        ) from exc
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"trace YAML top-level must be a mapping, got {type(data).__name__}: {path}"
        )
    return data


def load_synthetic_traces(path: Path) -> dict[str, Any]:
    """向后兼容别名：:func:`load_trace_yaml`。"""
    return load_trace_yaml(path)


def _resolve_trace_bridges_path(
    traces_path: Path,
    bridges_path: Optional[Path],
) -> Optional[Path]:
    """确定桥接 YAML：显式路径优先；否则同目录 ``trace_bridges.yaml`` 若存在则用。"""
    if bridges_path is not None:
        if bridges_path.exists():
            return bridges_path
        log.warning("trace bridges file missing (skipped): %s", bridges_path)
        return None
    cand = traces_path.parent / "trace_bridges.yaml"
    return cand if cand.exists() else None


# ---------------------------------------------------------------------- #
# 写入入口
# ---------------------------------------------------------------------- #
def ingest_trace(
    driver: Driver,
    traces_path: Path,
    *,
    bridges_path: Optional[Path] = None,
) -> None:
    """读 YAML → Task / Step / … / 可选 Turn、Experience 写入 Neo4j（v4）。"""
    data = load_trace_yaml(traces_path)
    bridges_file = _resolve_trace_bridges_path(traces_path, bridges_path)
    primary_bridges = list(data.get("trace_bridge_links") or [])
    if bridges_file:
        extra = load_trace_yaml(bridges_file)
        primary_bridges = primary_bridges + list(extra.get("trace_bridge_links") or [])
    data["trace_bridge_links"] = primary_bridges

    tasks = list(data.get("tasks") or [])
    experiences_top = list(data.get("experiences") or [])

    log.info(
        "trace graph: tasks=%d, top_level_experiences=%d",
        len(tasks),
        len(experiences_top),
    )

    with neo4j_session(driver) as s:
        for entry in tasks:
            _write_task_block(s, entry)
        for exp in experiences_top:
            if not isinstance(exp, dict):
                continue
            tk = str(exp.get("task_key") or "").strip()
            if not tk:
                log.warning("trace graph: top-level experience skipped (missing task_key)")
                continue
            tsig = str(
                exp.get("task_signature")
                or exp.get("signature_hash")
                or ""
            ).strip() or tk
            s.execute_write(_write_experience_node, task_key=tk, task_signature=tsig, exp=exp)

    bridges = list(data.get("trace_bridge_links") or [])
    if bridges:
        _, n_skipped = merge_topology_bridge_links(driver, bridges)
        log.info("trace_bridge_links: v4 skipped %d link(s) (no BRIDGES_TO)", n_skipped)


# ---------------------------------------------------------------------- #
# Task block（每条 trace = 一个 task + plans + tool_calls + observations + claims）
# ---------------------------------------------------------------------- #
def _write_task_block(session, entry: dict) -> None:
    if not isinstance(entry, dict):
        return
    task = entry.get("task") or {}
    plans = list(entry.get("plans") or [])
    failure_lesson = str(entry.get("failure_lesson") or "").strip()
    if not isinstance(task, dict) or not task.get("key"):
        return

    task_key = str(task["key"])

    # 1) Task 节点
    session.execute_write(
        _write_task_node,
        task={
            "key": task_key,
            "goal": str(task.get("goal") or ""),
            "status": str(task.get("status") or "pending"),
            "owner_agent": str(task.get("owner_agent") or ""),
            "task_signature": str(task.get("task_signature") or task.get("signature_hash") or ""),
            "as_of_date": str(task.get("as_of_date") or ""),
            "created_at": _iso(task.get("created_at")),
            "failure_lesson": failure_lesson,
        },
    )

    # 2) Plans + 链路
    plan_rows: list[dict] = []
    for plan in plans:
        if not isinstance(plan, dict) or not plan.get("key"):
            continue
        plan_rows.append(
            {
                "key": str(plan["key"]),
                "step_idx": int(plan.get("step_idx") or 0),
                "intent": str(plan.get("intent") or ""),
                "status": str(plan.get("status") or "pending"),
                "tool_hint": str(plan.get("tool_hint") or ""),
            }
        )
    if plan_rows:
        session.execute_write(_write_plans, task_key=task_key, plans=plan_rows)
        # NEXT 时间链
        if len(plan_rows) >= 2:
            ordered = sorted(plan_rows, key=lambda r: r["step_idx"])
            edges = [
                {"src": ordered[i]["key"], "dst": ordered[i + 1]["key"]}
                for i in range(len(ordered) - 1)
            ]
            session.execute_write(_write_plan_next, edges=edges)

    # 3) ToolCalls + Observations + Claims (+ 跨图)
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        plan_key = str(plan.get("key") or "").strip()
        if not plan_key:
            continue
        for tc in plan.get("tool_calls") or []:
            _write_tool_call_block(session, plan_key=plan_key, tc=tc)

    task_sig = str(task.get("task_signature") or task.get("signature_hash") or "")

    turns = list(entry.get("turns") or [])
    if turns:
        session.execute_write(_write_turns_block, task_key=task_key, task=task, turns=turns)

    for exp in list(entry.get("experiences") or []):
        if isinstance(exp, dict):
            session.execute_write(
                _write_experience_node,
                task_key=task_key,
                task_signature=task_sig or task_key,
                exp=exp,
            )


def _write_task_node(tx, *, task: dict) -> None:
    # Compute embedding for Task node (idempotent via hash check)
    emb_text = _task_emb_text(task)
    vec, emb_hash = _safe_embed(emb_text)
    tx.run(
        """
        MERGE (t:Task {key: $task.key})
          ON CREATE SET t.goal = $task.goal, t.status = $task.status,
                        t.owner_agent = $task.owner_agent,
                        t.task_signature = $task.task_signature,
                        t.as_of_date = $task.as_of_date,
                        t.failure_lesson = $task.failure_lesson,
                        t.created_at = CASE WHEN $task.created_at = '' THEN datetime() ELSE datetime($task.created_at) END,
                        t.zone = 'trace',
                        t.embedding = CASE WHEN $vec IS NOT NULL THEN $vec ELSE t.embedding END,
                        t.embedding_hash = CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE t.embedding_hash END
          ON MATCH  SET t.goal = $task.goal, t.status = $task.status,
                        t.owner_agent = $task.owner_agent,
                        t.task_signature = $task.task_signature,
                        t.as_of_date = $task.as_of_date,
                        t.failure_lesson = $task.failure_lesson,
                        t.zone = 'trace',
                        t.embedding = CASE WHEN $emb_hash <> coalesce(t.embedding_hash, '') AND $vec IS NOT NULL
                                           THEN $vec ELSE t.embedding END,
                        t.embedding_hash = CASE WHEN $emb_hash <> coalesce(t.embedding_hash, '') AND $vec IS NOT NULL
                                                THEN $emb_hash ELSE t.embedding_hash END
        """,
        task=task,
        vec=vec,
        emb_hash=emb_hash,
    )


def _write_plans(tx, *, task_key: str, plans: list[dict]) -> None:
    for p in plans:
        emb_text = _plan_emb_text(p)
        vec, emb_hash = _safe_embed(emb_text)
        p["_emb_vec"] = vec
        p["_emb_hash"] = emb_hash
    tx.run(
        """
        MATCH (task:Task {key: $task_key})
        UNWIND $plans AS p
        MERGE (plan:Step {key: p.key})
          ON CREATE SET plan.task_key = task.key, plan.step_idx = p.step_idx,
                        plan.intent = p.intent, plan.status = p.status,
                        plan.tool_hint = p.tool_hint, plan.zone = 'trace',
                        plan.embedding = CASE WHEN p._emb_vec IS NOT NULL THEN p._emb_vec ELSE plan.embedding END,
                        plan.embedding_hash = CASE WHEN p._emb_vec IS NOT NULL THEN p._emb_hash ELSE plan.embedding_hash END
          ON MATCH  SET plan.step_idx = p.step_idx, plan.intent = p.intent,
                        plan.status = p.status, plan.tool_hint = p.tool_hint,
                        plan.zone = 'trace',
                        plan.embedding = CASE WHEN p._emb_hash <> coalesce(plan.embedding_hash, '') AND p._emb_vec IS NOT NULL
                                              THEN p._emb_vec ELSE plan.embedding END,
                        plan.embedding_hash = CASE WHEN p._emb_hash <> coalesce(plan.embedding_hash, '') AND p._emb_vec IS NOT NULL
                                                   THEN p._emb_hash ELSE plan.embedding_hash END
        MERGE (task)-[r:DECOMPOSES_INTO]->(plan)
          ON CREATE SET r.order = p.step_idx
          ON MATCH  SET r.order = p.step_idx
        """,
        task_key=task_key,
        plans=plans,
    )


def _write_plan_next(tx, *, edges: list[dict]) -> None:
    tx.run(
        """
        UNWIND $edges AS e
        MATCH (a:Step {key: e.src})
        MATCH (b:Step {key: e.dst})
        MERGE (a)-[:NEXT]->(b)
        """,
        edges=edges,
    )


def _write_turns_block(tx, *, task_key: str, task: dict, turns: list[dict]) -> None:
    """``Turn`` 链 + 首轮 ``SPAWNS`` → ``Task``（v4）。"""
    session_id = str(task.get("session_id") or "").strip() or task_key
    rows: list[dict] = []
    for idx, raw in enumerate(
        sorted(
            [t for t in turns if isinstance(t, dict)],
            key=lambda t: int(t.get("ordinal") or t.get("order") or t.get("idx") or 0),
        )
    ):
        ordn = int(raw.get("ordinal") or raw.get("order") or raw.get("idx") or idx)
        tkey = str(raw.get("key") or "").strip() or turn_key(session_id, ordn)
        emb_text = _turn_emb_text({"role": str(raw.get("role") or "user"), "content": str(raw.get("content") or raw.get("text") or "")})
        vec, emb_hash = _safe_embed(emb_text)
        rows.append(
            {
                "key": tkey,
                "session_id": session_id,
                "ordinal": ordn,
                "role": str(raw.get("role") or "user"),
                "content": str(raw.get("content") or raw.get("text") or "")[:32000],
                "ts": _iso(raw.get("ts")),
                "_emb_vec": vec,
                "_emb_hash": emb_hash,
            }
        )
    if not rows:
        return
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (tn:Turn {key: r.key})
          ON CREATE SET tn.session_id = r.session_id, tn.role = r.role, tn.content = r.content,
                        tn.ordinal = r.ordinal,
                        tn.ts = CASE WHEN r.ts = '' THEN datetime() ELSE datetime(r.ts) END,
                        tn.zone = 'trace',
                        tn.embedding = CASE WHEN r._emb_vec IS NOT NULL THEN r._emb_vec ELSE tn.embedding END,
                        tn.embedding_hash = CASE WHEN r._emb_vec IS NOT NULL THEN r._emb_hash ELSE tn.embedding_hash END
          ON MATCH  SET tn.session_id = r.session_id, tn.role = r.role, tn.content = r.content,
                        tn.ordinal = r.ordinal,
                        tn.ts = CASE WHEN r.ts = '' THEN tn.ts ELSE datetime(r.ts) END,
                        tn.zone = 'trace',
                        tn.embedding = CASE WHEN r._emb_hash <> coalesce(tn.embedding_hash, '') AND r._emb_vec IS NOT NULL
                                            THEN r._emb_vec ELSE tn.embedding END,
                        tn.embedding_hash = CASE WHEN r._emb_hash <> coalesce(tn.embedding_hash, '') AND r._emb_vec IS NOT NULL
                                                 THEN r._emb_hash ELSE tn.embedding_hash END
        """,
        rows=rows,
    )
    for i in range(len(rows) - 1):
        tx.run(
            """
            MATCH (a:Turn {key: $a})
            MATCH (b:Turn {key: $b})
            MERGE (a)-[:NEXT]->(b)
            """,
            a=rows[i]["key"],
            b=rows[i + 1]["key"],
        )
    tx.run(
        """
        MATCH (tn:Turn {key: $first})
        MATCH (task:Task {key: $task_key})
        MERGE (tn)-[:SPAWNS]->(task)
        """,
        first=rows[0]["key"],
        task_key=task_key,
    )


def _write_experience_node(tx, *, task_key: str, task_signature: str, exp: dict) -> None:
    ek = str(exp.get("key") or "").strip()
    if not ek:
        ek = experience_key(f"{task_key}|{exp.get('outcome')}|{exp.get('key_insight')}")
    out = str(exp.get("outcome") or "").strip() or "unknown"
    insight = str(exp.get("key_insight") or exp.get("summary") or "").strip()
    scope = str(exp.get("applicable_scope") or "").strip()
    emb_text = _experience_emb_text(exp)
    vec, emb_hash = _safe_embed(emb_text)
    tx.run(
        """
        MERGE (e:Experience {key: $ek})
          ON CREATE SET e.task_signature = $tsig, e.outcome = $out,
                        e.key_insight = $insight, e.applicable_scope = $scope,
                        e.zone = 'trace',
                        e.embedding = CASE WHEN $vec IS NOT NULL THEN $vec ELSE e.embedding END,
                        e.embedding_hash = CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE e.embedding_hash END
          ON MATCH  SET e.task_signature = $tsig, e.outcome = $out,
                        e.key_insight = $insight, e.applicable_scope = $scope,
                        e.zone = 'trace',
                        e.embedding = CASE WHEN $emb_hash <> coalesce(e.embedding_hash, '') AND $vec IS NOT NULL
                                           THEN $vec ELSE e.embedding END,
                        e.embedding_hash = CASE WHEN $emb_hash <> coalesce(e.embedding_hash, '') AND $vec IS NOT NULL
                                                THEN $emb_hash ELSE e.embedding_hash END
        WITH e
        MATCH (t:Task {key: $task_key})
        MERGE (e)-[:DERIVED_FROM]->(t)
        """,
        ek=ek,
        tsig=task_signature,
        out=out,
        insight=insight[:8000],
        scope=scope[:2000],
        task_key=task_key,
        vec=vec,
        emb_hash=emb_hash,
    )
    for dk in list(exp.get("discovered_keys") or []):
        dk = str(dk).strip()
        if not dk:
            continue
        tx.run(
            """
            MATCH (e:Experience {key: $ek})
            OPTIONAL MATCH (n {key: $dk})
            FOREACH (_ IN CASE WHEN n IS NULL THEN [] ELSE [n] END |
                MERGE (e)-[:DISCOVERED]->(n)
            )
            """,
            ek=ek,
            dk=dk,
        )


def _write_tool_call_block(session, *, plan_key: str, tc: dict) -> None:
    if not isinstance(tc, dict) or not tc.get("key"):
        return
    tc_key = str(tc["key"])
    obs = tc.get("observation") or {}
    claims = list(tc.get("claims") or [])

    session.execute_write(
        _write_tool_call,
        plan_key=plan_key,
        tool_call={
            "key": tc_key,
            "tool_name": str(tc.get("tool_name") or ""),
            "args_json": str(tc.get("args_json") or ""),
            "status": str(tc.get("status") or "pending"),
            "latency_ms": int(tc.get("latency_ms") or 0),
            "retry_count": int(tc.get("retry_count") or 0),
            "returncode": int(tc.get("returncode") or 0),
        },
    )

    if isinstance(obs, dict) and obs.get("key"):
        session.execute_write(
            _write_observation,
            tc_key=tc_key,
            obs={
                "key": str(obs["key"]),
                "summary": str(obs.get("summary") or ""),
                "raw_blob_ref": str(obs.get("raw_blob_ref") or ""),
                "schema_hash": str(obs.get("schema_hash") or ""),
            },
        )

        # 跨图：Observation 触发的 EVIDENCED_BY → Event / Entity
        evidenced_by = str(obs.get("evidenced_by_kg") or "").strip()
        if evidenced_by:
            # Claims 也会从 obs 继承一条 EVIDENCED_BY；此处先在 obs 上落
            session.execute_write(
                _write_obs_evidence,
                obs_key=str(obs["key"]),
                kg_key=evidenced_by,
            )

    # Claims
    for claim in claims:
        if not isinstance(claim, dict) or not claim.get("key"):
            continue
        c_key = str(claim["key"])
        obs_key = str(obs.get("key") or "") if isinstance(obs, dict) else ""
        session.execute_write(
            _write_claim,
            obs_key=obs_key,
            claim={
                "key": c_key,
                "text": str(claim.get("text") or ""),
                "confidence": float(claim.get("confidence") or 0.0),
                "extractor": str(claim.get("extractor") or "rule"),
                "subject_type": str(claim.get("subject_type") or "literal"),
                "predicate": str(claim.get("predicate") or "asserts"),
                "object": str(claim.get("object") or claim.get("text") or "")[:2000],
            },
        )

        # 跨图：Claim → Metadata via resolved_to_metadata
        resolved_to = str(claim.get("resolved_to_metadata") or "").strip()
        if resolved_to:
            session.execute_write(
                _write_claim_resolved,
                claim_key=c_key,
                meta_key=resolved_to,
                resolver=str(claim.get("resolver") or "agent"),
                confidence=float(claim.get("confidence") or 0.0),
            )

        # 跨图：Claim → Event / Entity via evidenced_by_kg（claim 上独立标）
        evidenced = str(claim.get("evidenced_by_kg") or "").strip()
        if evidenced:
            session.execute_write(
                _write_claim_evidenced,
                claim_key=c_key,
                kg_key=evidenced,
                quote=str(claim.get("quote") or ""),
            )


def _write_tool_call(tx, *, plan_key: str, tool_call: dict) -> None:
    emb_text = _tc_emb_text(tool_call)
    vec, emb_hash = _safe_embed(emb_text)
    tx.run(
        """
        MATCH (plan:Step {key: $plan_key})
        MERGE (tc:ToolCall {key: $tc.key})
          ON CREATE SET tc.plan_key = plan.key, tc.tool_name = $tc.tool_name,
                        tc.args_json = $tc.args_json, tc.status = $tc.status,
                        tc.latency_ms = $tc.latency_ms, tc.retry_count = $tc.retry_count,
                        tc.ts = datetime(), tc.zone = 'trace',
                        tc.embedding = CASE WHEN $vec IS NOT NULL THEN $vec ELSE tc.embedding END,
                        tc.embedding_hash = CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE tc.embedding_hash END
          ON MATCH  SET tc.tool_name = $tc.tool_name, tc.args_json = $tc.args_json,
                        tc.status = $tc.status, tc.latency_ms = $tc.latency_ms,
                        tc.retry_count = $tc.retry_count, tc.zone = 'trace',
                        tc.embedding = CASE WHEN $emb_hash <> coalesce(tc.embedding_hash, '') AND $vec IS NOT NULL
                                            THEN $vec ELSE tc.embedding END,
                        tc.embedding_hash = CASE WHEN $emb_hash <> coalesce(tc.embedding_hash, '') AND $vec IS NOT NULL
                                                 THEN $emb_hash ELSE tc.embedding_hash END
        MERGE (plan)-[:EXECUTED_BY]->(tc)
        """,
        plan_key=plan_key,
        tc=tool_call,
        vec=vec,
        emb_hash=emb_hash,
    )


def _write_observation(tx, *, tc_key: str, obs: dict) -> None:
    emb_text = _obs_emb_text(obs)
    vec, emb_hash = _safe_embed(emb_text)
    obs_json = json.dumps(obs, ensure_ascii=False, default=str)
    tx.run(
        """
        MATCH (tc:ToolCall {key: $tc_key})
        WITH tc, $obs.key IN coalesce(tc.observation_keys, []) AS exists
        SET tc.observation_keys =
              CASE WHEN exists
                   THEN tc.observation_keys
                   ELSE coalesce(tc.observation_keys, []) + [$obs.key] END,
            tc.observations_json =
              CASE WHEN exists
                   THEN tc.observations_json
                   ELSE coalesce(tc.observations_json, []) + [$obs_json] END,
            tc.observation_summary = $obs.summary,
            tc.observation_status = coalesce($obs.status, tc.status),
            tc.observation_error = coalesce($obs.error, ''),
            tc.observation_embedding =
              CASE WHEN $vec IS NOT NULL THEN $vec ELSE tc.observation_embedding END,
            tc.observation_embedding_hash =
              CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE tc.observation_embedding_hash END,
            tc.zone = 'trace'
        """,
        tc_key=tc_key,
        obs=obs,
        obs_json=obs_json,
        vec=vec,
        emb_hash=emb_hash,
    )


def _write_claim(tx, *, obs_key: str, claim: dict) -> None:
    """v3: Claim 写入时补 valid_at / ingest_at / source_id / source_trust / content_hash。"""
    emb_text = _claim_emb_text(claim)
    vec, emb_hash = _safe_embed(emb_text)
    tx.run(
        """
        MERGE (cl:Claim {key: $claim.key})
          ON CREATE SET cl.text = $claim.text, cl.confidence = $claim.confidence,
                        cl.subject_type = $claim.subject_type,
                        cl.predicate = $claim.predicate,
                        cl.object = $claim.object,
                        cl.source_obs_key = $obs_key, cl.zone = 'trace',
                        cl.valid_at = datetime(),
                        cl.ingest_at = datetime(),
                        cl.source_id = coalesce($claim.source_id, 'agent'),
                        cl.source_trust = coalesce($claim.source_trust, 0.7),
                        cl.content_hash = coalesce($claim.content_hash, ''),
                        cl.ingest_method = coalesce($claim.ingest_method, 'agent'),
                        cl.embedding = CASE WHEN $vec IS NOT NULL THEN $vec ELSE cl.embedding END,
                        cl.embedding_hash = CASE WHEN $vec IS NOT NULL THEN $emb_hash ELSE cl.embedding_hash END
          ON MATCH  SET cl.text = $claim.text, cl.confidence = $claim.confidence,
                        cl.subject_type = $claim.subject_type,
                        cl.predicate = $claim.predicate,
                        cl.object = $claim.object,
                        cl.source_obs_key = $obs_key, cl.zone = 'trace',
                        cl.embedding = CASE WHEN $emb_hash <> coalesce(cl.embedding_hash, '') AND $vec IS NOT NULL
                                            THEN $vec ELSE cl.embedding END,
                        cl.embedding_hash = CASE WHEN $emb_hash <> coalesce(cl.embedding_hash, '') AND $vec IS NOT NULL
                                                 THEN $emb_hash ELSE cl.embedding_hash END
        WITH cl
        MATCH (tc:ToolCall)
        WHERE $obs_key IN coalesce(tc.observation_keys, [])
        MERGE (tc)-[r:PRODUCES]->(cl)
          ON CREATE SET r.extractor = $claim.extractor
          ON MATCH  SET r.extractor = $claim.extractor
        SET cl.source_tool_call_key = tc.key
        """,
        obs_key=obs_key,
        claim=claim,
        vec=vec,
        emb_hash=emb_hash,
    )


def _write_obs_evidence(tx, *, obs_key: str, kg_key: str) -> None:
    tx.run(
        """
        MATCH (tc:ToolCall)
        WHERE $obs_key IN coalesce(tc.observation_keys, [])
        OPTIONAL MATCH (kg {key: $kg_key})
        FOREACH (_ IN CASE WHEN kg IS NULL THEN [] ELSE [kg] END |
            MERGE (tc)-[:EVIDENCED_BY]->(kg)
        )
        """,
        obs_key=obs_key,
        kg_key=kg_key,
    )


def _write_claim_resolved(
    tx, *, claim_key: str, meta_key: str, resolver: str, confidence: float
) -> None:
    """Claim → Metric/Column 的跨图边 RESOLVED_TO（§12.1）。

    端点不存在时跳过（不允许跨 zone 建 ghost，§12.2）。
    """
    tx.run(
        """
        MATCH (cl:Claim {key: $claim_key})
        OPTIONAL MATCH (meta {key: $meta_key})
        FOREACH (_ IN CASE WHEN meta IS NULL THEN [] ELSE [meta] END |
            MERGE (cl)-[r:RESOLVED_TO]->(meta)
              ON CREATE SET r.resolver = $resolver, r.confidence = $confidence
              ON MATCH  SET r.resolver = $resolver, r.confidence = $confidence
        )
        """,
        claim_key=claim_key,
        meta_key=meta_key,
        resolver=resolver,
        confidence=confidence,
    )


def _write_claim_evidenced(tx, *, claim_key: str, kg_key: str, quote: str) -> None:
    tx.run(
        """
        MATCH (cl:Claim {key: $claim_key})
        OPTIONAL MATCH (kg {key: $kg_key})
        FOREACH (_ IN CASE WHEN kg IS NULL THEN [] ELSE [kg] END |
            MERGE (cl)-[r:EVIDENCED_BY]->(kg)
              ON CREATE SET r.quote = $quote
              ON MATCH  SET r.quote = $quote
        )
        """,
        claim_key=claim_key,
        kg_key=kg_key,
        quote=quote,
    )


# ---------------------------------------------------------------------- #
# 完整对话一次性灌入 + Claim 蒸馏
# ---------------------------------------------------------------------- #

def _parse_openai_messages(messages: List[Dict[str, Any]]) -> dict:
    """解析 OpenAI messages 格式的完整对话。

    返回：
        {
            "goal": str,              # 首条 user 消息
            "final_answer": str,      # 最后一条无 tool_calls 的 assistant 内容
            "steps": [                # assistant→tool 配对列表
                {
                    "tool_name": str,
                    "args_json": str,
                    "result": str,    # tool role content
                    "assistant_content": str,  # 同轮 assistant 文本（可能为 None）
                    "success": bool,
                }
            ],
        }
    """
    goal = ""
    final_answer = ""
    steps: list[dict] = []

    # 工具结果：tool_call_id → content
    tool_results: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            tool_results[call_id] = str(msg.get("content") or "")

    # 遍历 assistant 消息，提取 tool_calls 并配对结果
    for msg in messages:
        if msg.get("role") == "user" and not goal:
            goal = str(msg.get("content") or "")[:2000]
        elif msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function") or {}
                    call_id = str(tc.get("id") or "")
                    result = tool_results.get(call_id, "")
                    steps.append({
                        "tool_name": str(func.get("name") or ""),
                        "args_json": str(func.get("arguments") or ""),
                        "result": result,
                        "assistant_content": str(content or "") if content else None,
                        "success": bool(result.strip()),
                    })
            elif content:
                final_answer = str(content)[:3000]

    return {"goal": goal, "final_answer": final_answer, "steps": steps}


def _parse_qwenpaw_data_events(events: list[dict], *, goal: str = "") -> dict:
    """解析 JSONL 事件流。

    事件格式：每行 ``{"event": "thinking|thought|tool_call|tool_result", "step": int, ...}``，
    按 ``step`` 分组，一组 = thinking → thought → tool_call → tool_result。

    返回与 :func:`_parse_openai_messages` 相同的结构以复用下游写入逻辑。
    """
    from collections import defaultdict

    by_step: dict[int, list[dict]] = defaultdict(list)
    for ev in events:
        if not isinstance(ev, dict):
            continue
        step = ev.get("step")
        if step is None:
            continue
        by_step[int(step)].append(ev)

    steps: list[dict] = []
    final_answer = ""
    detected_goal = goal

    for step_idx in sorted(by_step):
        group = by_step[step_idx]
        thought_content = ""
        tool_name = ""
        tool_args: dict = {}
        tool_result = ""
        duration_ms = 0.0

        for ev in group:
            evt = ev.get("event", "")
            if evt == "thought":
                thought_content = str(ev.get("content") or "")
                if not detected_goal and thought_content:
                    detected_goal = thought_content[:2000]
            elif evt == "tool_call":
                tool_name = str(ev.get("tool") or "")
                tool_args = ev.get("args") or {}
            elif evt == "tool_result":
                tool_result = str(ev.get("result") or "")
                duration_ms = float(ev.get("duration_ms") or 0)

        if tool_name:
            try:
                args_json = json.dumps(tool_args, ensure_ascii=False, default=str)
            except Exception:
                args_json = str(tool_args)
            steps.append({
                "tool_name": tool_name,
                "args_json": args_json,
                "result": tool_result,
                "thought": thought_content,
                "duration_ms": duration_ms,
                "success": bool(tool_result.strip()),
            })
        elif thought_content:
            final_answer = thought_content[:3000]

    qid = None
    ts_min = ts_max = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if qid is None and "qid" in ev:
            qid = ev["qid"]
        ts = ev.get("ts")
        if ts is not None:
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or ts > ts_max:
                ts_max = ts

    total_ms = ((ts_max - ts_min) * 1000) if ts_min is not None and ts_max is not None else 0

    return {
        "goal": detected_goal,
        "final_answer": final_answer,
        "steps": steps,
        "metadata": {
            "qid": qid,
            "total_duration_ms": total_ms,
            "step_count": len(steps),
        },
    }



def ingest_dialog(
    driver: Driver,
    *,
    messages: List[Dict[str, Any]],
    signature_hash: str = "",
    as_of_date: str = "",
    status: str = "success",
    failure_lesson: str = "",
    task_key: Optional[str] = None,
    owner_agent: str = "context_manager",
    auto_distill: bool = True,
) -> dict:
    """完整对话（OpenAI messages 格式）→ 一次性写入 Trace Graph + 自动 Claim 蒸馏。

    与 :class:`TraceRecorder` 的逐步写入不同，此函数在 dialog 结束后一次性调用：
    解析 messages 中的 ``assistant.tool_calls`` / ``tool`` 配对，生成
    ``Task → Step → ToolCall`` 链路，结果作为 ToolCall 属性，然后触发 LLM 蒸馏 Claim。

    Args:
        driver: Neo4j driver。
        messages: OpenAI messages 格式的完整对话。
        signature_hash: Task signature；空则从 user question + as_of_date 派生。
        as_of_date: ``YYYYMMDD`` 或 ISO 8601 日期字符串。
        status: Task 状态（``success`` / ``failed``）。
        failure_lesson: 失败时的经验总结（挂到 Task.failure_lesson）。
        task_key: 显式 Task key；空则自动生成。
        owner_agent: 执行此 dialog 的 agent 名。
        auto_distill: 是否自动触发 LLM Claim 蒸馏（受 ``CFG.trace_auto_distill_claims`` 控制）。

    Returns:
        ``{"task_key": str, "claim_keys": list[str], "step_count": int}``
    """
    from .trace_claim_distiller import distill_trace_claims

    parsed = _parse_openai_messages(messages)
    steps = parsed["steps"]
    goal = parsed["goal"]
    final_answer = parsed["final_answer"]

    if not goal:
        log.warning("ingest_dialog: no user message found, skipping")
        return {"task_key": "", "claim_keys": [], "step_count": 0}

    if not signature_hash:
        payload = f"{goal.strip()}|{(as_of_date or '').strip()}"
        signature_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    if not task_key:
        task_key = f"task:{signature_hash}:{uuid.uuid4().hex[:8]}"

    log.info(
        "ingest_dialog: task=%s goal=%.60s steps=%d status=%s",
        task_key, goal, len(steps), status,
    )

    with neo4j_session(driver) as s:
        # 1) Task
        s.execute_write(
            _write_task_node,
            task={
                "key": task_key,
                "goal": goal[:1000],
                "status": str(status or "success"),
                "owner_agent": str(owner_agent or "context_manager"),
                "task_signature": signature_hash,
                "as_of_date": as_of_date or "",
                "created_at": _iso(_dt.datetime.now(_dt.timezone.utc)),
                "failure_lesson": str(failure_lesson or "")[:1000],
            },
        )

        # 2) Plans + ToolCalls + Observations
        if steps:
            plan_rows: list[dict] = []
            for i, step in enumerate(steps):
                intent = step.get("assistant_content") or f"call: {step['tool_name']}"
                plan_status = "success" if step.get("success") else "failed"
                plan_rows.append({
                    "key": f"{task_key}:step:{i}",
                    "step_idx": i,
                    "intent": str(intent)[:500],
                    "status": plan_status,
                    "tool_hint": str(step["tool_name"] or "")[:200],
                })

            s.execute_write(_write_plans, task_key=task_key, plans=plan_rows)

            # Step NEXT chain
            if len(plan_rows) >= 2:
                edges = [
                    {"src": plan_rows[i]["key"], "dst": plan_rows[i + 1]["key"]}
                    for i in range(len(plan_rows) - 1)
                ]
                s.execute_write(_write_plan_next, edges=edges)

            # ToolCalls + Observations
            for i, step in enumerate(steps):
                plan_key = plan_rows[i]["key"]
                tc_key = f"tc:{task_key}:{i}:{uuid.uuid4().hex[:8]}"
                obs_key = f"obs:{task_key}:{i}:{uuid.uuid4().hex[:8]}" if step["result"] else ""

                tc_status = "success" if step.get("success") else "failed"
                s.execute_write(
                    _write_tool_call,
                    plan_key=plan_key,
                    tool_call={
                        "key": tc_key,
                        "tool_name": str(step["tool_name"] or ""),
                        "args_json": str(step["args_json"] or "")[:2000],
                        "status": tc_status,
                        "latency_ms": 0,
                        "retry_count": 0,
                        "returncode": 0 if step.get("success") else 1,
                    },
                )

                if obs_key:
                    s.execute_write(
                        _write_observation,
                        tc_key=tc_key,
                        obs={
                            "key": obs_key,
                            "summary": str(step["result"] or "")[:3000],
                            "raw_blob_ref": "",
                            "schema_hash": "",
                        },
                    )

        # 3) final_answer → 额外 Observation 挂在最后一个 ToolCall 下
        if final_answer and steps:
            last_plan_key = f"{task_key}:step:{len(steps) - 1}"
            fa_tc_key = f"tc:{task_key}:final:{uuid.uuid4().hex[:8]}"
            fa_obs_key = f"obs:{task_key}:final:{uuid.uuid4().hex[:8]}"
            s.execute_write(
                _write_tool_call,
                plan_key=last_plan_key,
                tool_call={
                    "key": fa_tc_key,
                    "tool_name": "final_answer",
                    "args_json": "",
                    "status": "success",
                    "latency_ms": 0,
                    "retry_count": 0,
                    "returncode": 0,
                },
            )
            s.execute_write(
                _write_observation,
                tc_key=fa_tc_key,
                obs={
                    "key": fa_obs_key,
                    "summary": final_answer[:3000],
                    "raw_blob_ref": "",
                    "schema_hash": "",
                },
            )

    # 4) Claim 蒸馏
    claim_keys: list[str] = []
    if auto_distill and status in ("success", "failed"):
        try:
            claim_keys = distill_trace_claims(driver, task_key)
        except Exception as exc:
            log.warning("ingest_dialog: claim distill failed for %s: %s", task_key, exc)

    return {"task_key": task_key, "claim_keys": claim_keys, "step_count": len(steps)}


def ingest_qwenpaw_data_trace(
    driver: Driver,
    *,
    events: list[dict],
    goal: str = "",
    signature_hash: str = "",
    as_of_date: str = "",
    status: str = "success",
    failure_lesson: str = "",
    task_key: Optional[str] = None,
    owner_agent: str = "qwenpaw-data",
    auto_distill: bool = True,
    oss_raw_key: str = "",
) -> dict:
    """JSONL 事件流 → 一次性写入 Trace Graph + 自动 Claim 蒸馏。

    与 :func:`ingest_dialog` 平行，处理
    ``thinking / thought / tool_call / tool_result`` 事件格式。

    Args:
        driver: Neo4j driver。
        events: JSONL 事件列表（每项为 dict）。
        goal: 用户问题；空则从首条 thought 推断。
        signature_hash: Task signature；空则从 goal + as_of_date 派生。
        as_of_date: 业务日期。
        status: Task 状态（success / failed）。
        failure_lesson: 失败原因。
        task_key: 显式 Task key；空则自动生成。
        owner_agent: agent 标识。
        auto_distill: 是否自动触发 Claim 蒸馏。
        oss_raw_key: 原始 JSONL 在 OSS 上的 key（写入 Task 节点供溯源）。

    Returns:
        ``{"task_key": str, "claim_keys": list[str], "step_count": int, "oss_raw_key": str}``
    """
    from .trace_claim_distiller import distill_trace_claims

    parsed = _parse_qwenpaw_data_events(events, goal=goal)
    steps = parsed["steps"]
    goal = parsed["goal"]
    final_answer = parsed["final_answer"]

    if not goal:
        log.warning("ingest_qwenpaw_data_trace: no goal found, skipping")
        return {"task_key": "", "claim_keys": [], "step_count": 0, "oss_raw_key": oss_raw_key}

    if not signature_hash:
        payload = f"{goal.strip()}|{(as_of_date or '').strip()}"
        signature_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    if not task_key:
        task_key = f"task:{signature_hash}:{uuid.uuid4().hex[:8]}"

    log.info(
        "ingest_qwenpaw_data_trace: task=%s goal=%.60s steps=%d status=%s",
        task_key, goal, len(steps), status,
    )

    with neo4j_session(driver) as s:
        # 1) Task 节点
        task_dict = {
            "key": task_key,
            "goal": goal[:1000],
            "status": str(status or "success"),
            "owner_agent": str(owner_agent or "qwenpaw-data"),
            "task_signature": signature_hash,
            "as_of_date": as_of_date or "",
            "created_at": _iso(_dt.datetime.now(_dt.timezone.utc)),
            "failure_lesson": str(failure_lesson or "")[:1000],
        }
        s.execute_write(_write_task_node, task=task_dict)

        if oss_raw_key:
            s.run(
                "MATCH (t:Task {key: $k}) SET t.oss_raw_key = $oss",
                k=task_key, oss=oss_raw_key,
            )

        # 2) Plans + ToolCalls + Observations
        if steps:
            plan_rows: list[dict] = []
            for i, step in enumerate(steps):
                intent = step.get("thought") or f"call: {step['tool_name']}"
                plan_status = "success" if step.get("success") else "failed"
                plan_rows.append({
                    "key": f"{task_key}:step:{i}",
                    "step_idx": i,
                    "intent": str(intent)[:500],
                    "status": plan_status,
                    "tool_hint": str(step["tool_name"] or "")[:200],
                })

            s.execute_write(_write_plans, task_key=task_key, plans=plan_rows)

            if len(plan_rows) >= 2:
                edges = [
                    {"src": plan_rows[i]["key"], "dst": plan_rows[i + 1]["key"]}
                    for i in range(len(plan_rows) - 1)
                ]
                s.execute_write(_write_plan_next, edges=edges)

            for i, step in enumerate(steps):
                plan_key = plan_rows[i]["key"]
                tc_key = f"tc:{task_key}:{i}:{uuid.uuid4().hex[:8]}"
                obs_key = f"obs:{task_key}:{i}:{uuid.uuid4().hex[:8]}" if step["result"] else ""

                tc_status = "success" if step.get("success") else "failed"
                s.execute_write(
                    _write_tool_call,
                    plan_key=plan_key,
                    tool_call={
                        "key": tc_key,
                        "tool_name": str(step["tool_name"] or ""),
                        "args_json": str(step["args_json"] or "")[:2000],
                        "status": tc_status,
                        "latency_ms": int(step.get("duration_ms") or 0),
                        "retry_count": 0,
                        "returncode": 0 if step.get("success") else 1,
                    },
                )

                if obs_key:
                    s.execute_write(
                        _write_observation,
                        tc_key=tc_key,
                        obs={
                            "key": obs_key,
                            "summary": str(step["result"] or "")[:3000],
                            "raw_blob_ref": "",
                            "schema_hash": "",
                        },
                    )

        # 3) final_answer
        if final_answer and steps:
            last_plan_key = f"{task_key}:step:{len(steps) - 1}"
            fa_tc_key = f"tc:{task_key}:final:{uuid.uuid4().hex[:8]}"
            fa_obs_key = f"obs:{task_key}:final:{uuid.uuid4().hex[:8]}"
            s.execute_write(
                _write_tool_call,
                plan_key=last_plan_key,
                tool_call={
                    "key": fa_tc_key,
                    "tool_name": "final_answer",
                    "args_json": "",
                    "status": "success",
                    "latency_ms": 0,
                    "retry_count": 0,
                    "returncode": 0,
                },
            )
            s.execute_write(
                _write_observation,
                tc_key=fa_tc_key,
                obs={
                    "key": fa_obs_key,
                    "summary": final_answer[:3000],
                    "raw_blob_ref": "",
                    "schema_hash": "",
                },
            )

    # 4) Claim 蒸馏
    claim_keys: list[str] = []
    if auto_distill and status in ("success", "failed"):
        try:
            claim_keys = distill_trace_claims(driver, task_key)
        except Exception as exc:
            log.warning("ingest_qwenpaw_data_trace: claim distill failed for %s: %s", task_key, exc)

    return {
        "task_key": task_key,
        "claim_keys": claim_keys,
        "step_count": len(steps),
        "oss_raw_key": oss_raw_key,
    }


# ---------------------------------------------------------------------- #
# 在线增量写入：TraceRecorder（agent 用）
# ---------------------------------------------------------------------- #
class TraceRecorder:
    """ReAct agent 在线写 Trace Graph 的薄封装。

    与离线 ``ingest_trace`` 共用底层 ``_write_*`` 事务函数，仅暴露按事件粒度的
    高层方法：``start_task → write_plans → record_step → update_task_status``。

    召回模板：

    - ``recall_success_traces``  → T1 trace_recall
    - ``recall_failed_plans``    → T2 failure_avoidance

    所有写入 idempotent；同 ``signature_hash`` 同 ``task_key`` 反复跑只会更新属性，
    不会重复建点。
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    # -- helpers -------------------------------------------------------- #
    @staticmethod
    def signature_hash(question: str, as_of_date: str = "") -> str:
        """与 ``§10.3`` 一致：``sha256(question + as_of_date)[:16]``。"""
        payload = f"{(question or '').strip()}|{(as_of_date or '').strip()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def make_task_key(signature_hash: str, suffix: Optional[str] = None) -> str:
        suffix = suffix or uuid.uuid4().hex[:8]
        return f"task:{signature_hash}:{suffix}"

    @staticmethod
    def _make_plan_key(task_key: str, step_idx: int) -> str:
        # task:<sig>:<suffix> → plan:<sig>:<suffix>:<step_idx>
        body = task_key.split(":", 1)[1] if ":" in task_key else task_key
        return f"step:{body}:{step_idx}"

    # -- task ----------------------------------------------------------- #
    def start_task(
        self,
        *,
        goal: str,
        signature_hash: str,
        owner_agent: str = "context_manager",
        as_of_date: str = "",
        task_key: Optional[str] = None,
    ) -> str:
        key = task_key or self.make_task_key(signature_hash)
        task = {
            "key": key,
            "goal": str(goal or "")[:1000],
            "status": "running",
            "owner_agent": str(owner_agent or "context_manager"),
            "task_signature": signature_hash,
            "as_of_date": as_of_date or "",
            "created_at": _iso(_dt.datetime.now(_dt.timezone.utc)),
            "failure_lesson": "",
        }
        with neo4j_session(self.driver) as s:
            s.execute_write(_write_task_node, task=task)
        return key

    def update_task_status(
        self,
        *,
        task_key: str,
        status: str,
        failure_lesson: str = "",
    ) -> None:
        with neo4j_session(self.driver) as s:
            s.run(
                """
                MATCH (t:Task {key: $k})
                SET t.status = $s,
                    t.failure_lesson = $lesson
                """,
                k=task_key,
                s=status,
                lesson=str(failure_lesson or "")[:1000],
            )
        # task 结束后异步触发 LLM claim 蒸馏（daemon thread，不阻塞 agent）
        from .trace_claim_distiller import auto_distill_on_task_end
        auto_distill_on_task_end(self.driver, task_key, status)

    # -- plans ---------------------------------------------------------- #
    def write_plans(self, *, task_key: str, plans: List[Dict[str, Any]]) -> List[str]:
        """plans: ``[{"step_idx": int, "intent": str, "tool_hint": str}, ...]``，返回 plan_key 列表。"""
        rows: List[Dict[str, Any]] = []
        for p in plans:
            if not isinstance(p, dict):
                continue
            step_idx = int(p.get("step_idx") or 0)
            rows.append(
                {
                    "key": self._make_plan_key(task_key, step_idx),
                    "step_idx": step_idx,
                    "intent": str(p.get("intent") or "")[:500],
                    "status": str(p.get("status") or "pending"),
                    "tool_hint": str(p.get("tool_hint") or "")[:200],
                }
            )
        if not rows:
            return []
        with neo4j_session(self.driver) as s:
            s.execute_write(_write_plans, task_key=task_key, plans=rows)
            if len(rows) >= 2:
                ordered = sorted(rows, key=lambda r: r["step_idx"])
                edges = [
                    {"src": ordered[i]["key"], "dst": ordered[i + 1]["key"]}
                    for i in range(len(ordered) - 1)
                ]
                s.execute_write(_write_plan_next, edges=edges)
        return [r["key"] for r in rows]

    def update_plan_status(self, *, plan_key: str, status: str) -> None:
        with neo4j_session(self.driver) as s:
            s.run(
                "MATCH (p:Step {key: $k}) SET p.status = $s",
                k=plan_key,
                s=str(status or "pending"),
            )

    # -- step (ToolCall + Observation + Claims) ------------------------ #
    def record_step(
        self,
        *,
        plan_key: str,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        status: str = "success",
        observation_summary: str = "",
        claims: Optional[List[Dict[str, Any]]] = None,
        latency_ms: int = 0,
        retry_count: int = 0,
        raw_blob_ref: str = "",
        schema_hash: str = "",
        returncode: int = 0,
    ) -> Dict[str, Any]:
        """一步 = 1 ToolCall (+ 可选 Observation) (+ 可选 Claims)。

        Claim 可带 ``resolved_to``、``evidenced_by_kg``（Event/Entity key）等跨图字段。
        """
        tc_key = f"tc:{uuid.uuid4().hex}"
        obs_key = f"obs:{uuid.uuid4().hex}" if observation_summary else ""
        claim_keys: List[str] = []

        try:
            args_json = json.dumps(args or {}, ensure_ascii=False, default=str)[:2000]
        except Exception:
            args_json = str(args)[:2000]

        with neo4j_session(self.driver) as s:
            s.execute_write(
                _write_tool_call,
                plan_key=plan_key,
                tool_call={
                    "key": tc_key,
                    "tool_name": str(tool_name or ""),
                    "args_json": args_json,
                    "status": str(status or "success"),
                    "latency_ms": int(latency_ms or 0),
                    "retry_count": int(retry_count or 0),
                    "returncode": int(returncode or 0),
                },
            )
            if obs_key:
                s.execute_write(
                    _write_observation,
                    tc_key=tc_key,
                    obs={
                        "key": obs_key,
                        "summary": str(observation_summary or "")[:2000],
                        "raw_blob_ref": str(raw_blob_ref or ""),
                        "schema_hash": str(schema_hash or ""),
                    },
                )
            for cl in claims or []:
                if not isinstance(cl, dict):
                    continue
                c_key = f"claim:{uuid.uuid4().hex}"
                claim_keys.append(c_key)
                s.execute_write(
                    _write_claim,
                    obs_key=obs_key,
                    claim={
                        "key": c_key,
                        "text": str(cl.get("text") or "")[:1500],
                        "confidence": float(cl.get("confidence") or 0.5),
                        "extractor": str(cl.get("extractor") or "agent"),
                        "subject_type": str(cl.get("subject_type") or "literal"),
                        "predicate": str(cl.get("predicate") or "asserts"),
                        "object": str(cl.get("object") or cl.get("text") or "")[:2000],
                    },
                )
                resolved = str(cl.get("resolved_to") or "").strip()
                if resolved:
                    s.execute_write(
                        _write_claim_resolved,
                        claim_key=c_key,
                        meta_key=resolved,
                        resolver=str(cl.get("resolver") or "agent"),
                        confidence=float(cl.get("confidence") or 0.5),
                    )
                evid = str(cl.get("evidenced_by_kg") or "").strip()
                if evid:
                    s.execute_write(
                        _write_claim_evidenced,
                        claim_key=c_key,
                        kg_key=evid,
                        quote=str(cl.get("quote") or "")[:500],
                    )

        return {"tc_key": tc_key, "obs_key": obs_key, "claim_keys": claim_keys}

    # -- recall (T1 / T2) ---------------------------------------------- #
    def recall_success_traces(
        self,
        *,
        signature_hash: str,
        k: int = 3,
        exclude_task_key: str = "",
    ) -> List[Dict[str, Any]]:
        """T1 trace_recall：同 signature 历史成功 Task + plan 概要（按时间倒序）。

        v3：Claim 过滤 ``valid_to IS NULL``（未失效的事实）。
        """
        cypher = """
        MATCH (t:Task)
        WHERE t.task_signature = $sig AND t.status = 'success'
          AND ($excl = '' OR t.key <> $excl)
        OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(p:Step)
        WITH t, p ORDER BY coalesce(p.step_idx, 0)
        WITH t, collect({
          step_idx: coalesce(p.step_idx, 0),
          intent: coalesce(p.intent, ''),
          tool_hint: coalesce(p.tool_hint, '')
        }) AS plans
        RETURN t.key AS task_key, t.goal AS goal,
               toString(t.created_at) AS created_at, plans
        ORDER BY t.created_at DESC
        LIMIT $k
        """
        with neo4j_session(self.driver) as s:
            rows = s.run(
                cypher, sig=signature_hash, excl=exclude_task_key, k=int(k)
            ).data()
        return rows

    def recall_failed_plans(
        self,
        *,
        signature_hash: str,
        k: int = 3,
    ) -> List[Dict[str, Any]]:
        """T2 failure_avoidance：同 signature 历史失败 Step + Task.failure_lesson。

        v3：仅召回 valid_to IS NULL 的 Claim（非失效事实），任务仍按全量失败记录。
        """
        cypher = """
        MATCH (t:Task {task_signature: $sig, status: 'failed'})
              -[:DECOMPOSES_INTO]->(p:Step {status: 'failed'})
        RETURN p.intent AS intent, p.step_idx AS step_idx, p.key AS plan_key,
               coalesce(t.failure_lesson, '') AS lesson, t.key AS task_key
        ORDER BY t.created_at DESC, p.step_idx
        LIMIT $k
        """
        with neo4j_session(self.driver) as s:
            rows = s.run(cypher, sig=signature_hash, k=int(k)).data()
        return rows

    def invalidate_claim(self, *, claim_key: str, reason: str = "") -> None:
        """v3：标记 Claim 失效（设置 valid_to）。不物理删除，保持 append-only。"""
        with neo4j_session(self.driver) as s:
            s.run(
                """
                MATCH (cl:Claim {key: $k})
                WHERE cl.valid_to IS NULL
                SET cl.valid_to = datetime(),
                    cl.invalidation_reason = $reason
                """,
                k=claim_key,
                reason=str(reason or "")[:500],
            )


# ---------------------------------------------------------------------- #
# QwenPaw Data Host snapshot ingestion
# ---------------------------------------------------------------------- #

def _write_host_trace_tx(tx, *, model: dict[str, Any]) -> None:
    """Merge a parsed QwenPaw Data host trace into the TG."""
    session = model["session"]
    tx.run(
        """
        MERGE (s:Session {key: $row.key})
        SET s.session_id = $row.session_id,
            s.user_id = $row.user_id,
            s.agent_name = $row.agent_name,
            s.datasource_id = $row.datasource_id,
            s.metadata_json = $row.metadata_json,
            s.message_count = $row.message_count,
            s.trace_hash = $row.trace_hash,
            s.zone = 'trace',
            s.created_at = coalesce(s.created_at, datetime())
        """,
        row=session,
    )
    main_task_keys = [
        row["key"] for row in model["tasks"] if row["task_kind"] == "main"
    ]
    # Preserve append-only Claim provenance while replacing execution nodes.
    tx.run(
        """
        MATCH (root:Task {session_key: $session_key, task_kind: 'main'})
        MATCH (root)-[:DECOMPOSES_INTO|EXECUTED_BY|SPAWNS*1..64]
              ->(tc:ToolCall)-[:PRODUCES]->(cl:Claim)
        SET cl.source_tool_call_key = tc.key
        """,
        session_key=session["key"],
    )
    # Remove main Tasks that belonged to an older version of this same
    # session snapshot. Claim nodes remain append-only; DETACH only removes
    # their edge from an execution node that no longer exists.
    tx.run(
        """
        MATCH (old:Task {session_key: $session_key, task_kind: 'main'})
        WHERE NOT old.key IN $task_keys
        OPTIONAL MATCH (old)-[
            :DECOMPOSES_INTO|EXECUTED_BY|SPAWNS*1..64
        ]->(descendant)
        WITH collect(DISTINCT old) AS olds,
             collect(DISTINCT descendant) AS descendants
        WITH olds + descendants AS stale_nodes
        UNWIND stale_nodes AS stale_node
        DETACH DELETE stale_node
        """,
        session_key=session["key"],
        task_keys=main_task_keys,
    )
    # A host payload is a complete snapshot. Remove the previous execution
    # subtree first, then recreate it in this same transaction. This prevents
    # revised DAGs and shortened traces from leaving stale steps behind.
    tx.run(
        """
        UNWIND $task_keys AS task_key
        MATCH (root:Task {key: task_key})
        OPTIONAL MATCH (root)-[
            :DECOMPOSES_INTO|EXECUTED_BY|SPAWNS*1..64
        ]->(descendant)
        WITH collect(DISTINCT descendant) AS descendants
        UNWIND descendants AS descendant
        DETACH DELETE descendant
        """,
        task_keys=main_task_keys,
    )
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (t:Task {key: r.key})
        SET t.goal = r.goal,
            t.status = r.status,
            t.owner_agent = r.owner_agent,
            t.task_signature = r.task_signature,
            t.created_at = CASE WHEN r.created_at = '' THEN coalesce(t.created_at, datetime())
                                ELSE datetime(replace(r.created_at, ' ', 'T')) END,
            t.task_kind = r.task_kind,
            t.parent_task_key = r.parent_task_key,
            t.parent_tool_call_key = r.parent_tool_call_key,
            t.graph_id = r.graph_id,
            t.session_key = r.session_key,
            t.source_message_id = r.source_message_id,
            t.source_message_role = r.source_message_role,
            t.source_message_timestamp = r.source_message_timestamp,
            t.user_input = r.user_input,
            t.expected_outcome = r.expected_outcome,
            t.datasource_id = r.datasource_id,
            t.zone = 'trace'
        """,
        rows=model["tasks"],
    )
    tx.run(
        """
        MATCH (s:Session {key: $session_key})-[old:HAS_TASK]->(:Task)
        DELETE old
        """,
        session_key=session["key"],
    )
    tx.run(
        """
        UNWIND $task_keys AS task_key
        MATCH (s:Session {key: $session_key})
        MATCH (t:Task {key: task_key})
        MERGE (s)-[:HAS_TASK]->(t)
        """,
        session_key=session["key"],
        task_keys=main_task_keys,
    )
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (t:Task {key: r.task_key})
        MERGE (p:Step {key: r.key})
        SET p.task_key = r.task_key,
            p.step_idx = r.step_idx,
            p.source_node_id = r.source_node_id,
            p.intent = r.intent,
            p.status = r.status,
            p.tool_hint = r.tool_hint,
            p.source_entry_index = r.source_entry_index,
            p.deps_json = r.deps_json,
            p.source_message_id = r.source_message_id,
            p.source_message_role = r.source_message_role,
            p.source_message_timestamp = r.source_message_timestamp,
            p.reasoning_json = r.reasoning_json,
            p.datasource_id = r.datasource_id,
            p.zone = 'trace'
        MERGE (t)-[rel:DECOMPOSES_INTO]->(p)
        SET rel.order = r.step_idx
        """,
        rows=model["plans"],
    )
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (p:Step {key: r.plan_key})
        MERGE (tc:ToolCall {key: r.key})
        SET tc.plan_key = r.plan_key,
            tc.tool_name = r.tool_name,
            tc.args_json = r.args_json,
            tc.status = r.status,
            tc.error = r.error,
            tc.agent_name = r.agent_name,
            tc.source_message_id = r.source_message_id,
            tc.source_message_role = r.source_message_role,
            tc.source_message_timestamp = r.source_message_timestamp,
            tc.source_entry_index = r.source_entry_index,
            tc.parent_tool_call_key = r.parent_tool_call_key,
            tc.synthetic = r.synthetic,
            tc.observations_json = r.observations_json,
            tc.observation_keys = r.observation_keys,
            tc.observation_summary = r.observation_summary,
            tc.observation_status = r.observation_status,
            tc.observation_error = r.observation_error,
            tc.datasource_id = r.datasource_id,
            tc.zone = 'trace'
        MERGE (p)-[:EXECUTED_BY]->(tc)
        """,
        rows=model["tool_calls"],
    )
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (tc:ToolCall {key: r.tool_call_key})
        MATCH (t:Task {key: r.task_key})
        MERGE (tc)-[:SPAWNS]->(t)
        """,
        rows=model["spawns"],
    )
    # Reattach append-only Claims directly to their stable ToolCall.
    tx.run(
        """
        UNWIND $tool_call_keys AS tool_call_key
        MATCH (tc:ToolCall {key: tool_call_key})
        MATCH (cl:Claim {source_tool_call_key: tool_call_key})
        MERGE (tc)-[:PRODUCES]->(cl)
        """,
        tool_call_keys=[row["key"] for row in model["tool_calls"]],
    )


def ingest_host_trace(
    driver: Driver,
    payload: dict[str, Any],
    *,
    auto_distill: bool = True,
    neo4j_database: Optional[str] = None,
) -> dict[str, Any]:
    """Parse and atomically persist a complete QwenPaw Data host session snapshot.

    After the graph write, asynchronously triggers claim + strategy distillation
    for each completed main Task (status success/failed), reusing the same
    ``auto_distill_on_task_end`` daemon-thread path that ``TraceRecorder`` uses.
    """
    from .host_trace_parser import parse_host_trace
    from .trace_claim_distiller import auto_distill_on_task_end

    model = parse_host_trace(payload)
    datasource_id = str(
        model.get("session", {}).get("datasource_id", "")
    ).strip()
    for rows in (model["tasks"], model["plans"], model["tool_calls"]):
        for row in rows:
            row["datasource_id"] = datasource_id
    observation_count = 0
    for tool_call in model["tool_calls"]:
        observations = list(tool_call.get("observations") or [])
        observation_count += len(observations)
        latest = observations[-1] if observations else {}
        tool_call["observations_json"] = [
            json.dumps(item, ensure_ascii=False, default=str)
            for item in observations
        ]
        tool_call["observation_keys"] = [
            str(item.get("key") or "") for item in observations
        ]
        tool_call["observation_summary"] = str(latest.get("summary") or "")
        tool_call["observation_status"] = str(latest.get("status") or "")
        tool_call["observation_error"] = str(latest.get("error") or "")
    with neo4j_session(driver) as session:
        session.execute_write(_write_host_trace_tx, model=model)

    if auto_distill:
        for task in model["tasks"]:
            if task.get("task_kind") != "main":
                continue
            task_key = str(task.get("key") or "")
            status = str(task.get("status") or "")
            if not task_key or status not in ("success", "failed"):
                continue
            try:
                auto_distill_on_task_end(
                    driver, task_key, status,
                    neo4j_database=neo4j_database,
                )
            except Exception as exc:
                log.warning(
                    "ingest_host_trace: distill trigger failed for %s: %s",
                    task_key, exc,
                )

    return {
        "task_keys": [row["key"] for row in model["tasks"]],
        "counts": {
            "tasks": len(model["tasks"]),
            "plans": len(model["plans"]),
            "tool_calls": len(model["tool_calls"]),
            "observations": observation_count,
        },
    }


# ═══════════════════════════════════════════════════════════════════════ #
#  TG admin helpers — lifecycle management
# ═══════════════════════════════════════════════════════════════════════ #

def archive_task(driver: Driver, task_key: str, *, reason: str = "") -> dict[str, Any]:
    """Transition a completed/failed task to archived status."""
    with neo4j_session(driver) as s:
        rec = s.run(
            """
            MATCH (t:Task {key: $key})
            WHERE t.status IN ['success', 'failed']
            SET t.status = 'archived',
                t.status_reason = $reason,
                t.status_updated_at = datetime()
            RETURN t.key AS key, t.status AS status
            """,
            key=task_key, reason=reason,
        ).single()
    if not rec:
        raise ValueError(f"Task {task_key} 不存在或状态不允许归档")
    return {"ok": True, "key": rec["key"], "status": rec["status"]}


def delete_task_cascade(driver: Driver, task_key: str) -> dict[str, Any]:
    """Delete a task and all its downstream nodes (Step, ToolCall, etc.)."""
    with neo4j_session(driver) as s:
        rec = s.run("MATCH (t:Task {key: $key}) RETURN t.key AS key", key=task_key).single()
        if not rec:
            raise ValueError(f"Task not found: {task_key}")
        result = s.run(
            """
            MATCH (t:Task {key: $key})
            OPTIONAL MATCH (t)-[*]->(child)
            DETACH DELETE child, t
            """,
            key=task_key,
        )
        summary = result.consume()
        nodes_deleted = summary.counters.nodes_deleted
    log.info("trace: cascade delete Task %s (nodes=%s)", task_key, nodes_deleted)
    return {"ok": True, "key": task_key, "nodes_deleted": nodes_deleted}


def batch_archive_tasks(driver: Driver, task_keys: list[str], *, reason: str = "") -> dict[str, Any]:
    """Archive up to 50 tasks in one call, skipping ineligible ones."""
    if len(task_keys) > 50:
        raise ValueError("单次最多归档 50 个 Task")
    archived = 0
    for key in task_keys:
        try:
            archive_task(driver, key, reason=reason)
            archived += 1
        except ValueError:
            pass
    return {"ok": True, "requested": len(task_keys), "archived": archived}


def batch_delete_tasks(driver: Driver, task_keys: list[str]) -> dict[str, Any]:
    """Cascade-delete up to 50 tasks in one call."""
    if len(task_keys) > 50:
        raise ValueError("单次最多删除 50 个 Task")
    deleted = 0
    for key in task_keys:
        try:
            delete_task_cascade(driver, key)
            deleted += 1
        except ValueError:
            pass
    return {"ok": True, "requested": len(task_keys), "deleted": deleted}


def invalidate_claim(driver: Driver, claim_key: str, *, reason: str = "") -> dict[str, Any]:
    """Expire a claim by setting valid_to to now."""
    with neo4j_session(driver) as s:
        rec = s.run(
            """
            MATCH (c:Claim {key: $key})
            SET c.valid_to = datetime(),
                c.invalidation_reason = $reason
            RETURN c.key AS key
            """,
            key=claim_key, reason=reason,
        ).single()
    if not rec:
        raise ValueError(f"Claim not found: {claim_key}")
    return {"ok": True, "key": rec["key"]}


def list_claims_global(
    driver: Driver,
    *,
    subject_type: str | None = None,
    q: str | None = None,
    valid: bool | None = None,
    task_key: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """Paginated global claim query with subject_type/keyword/validity filters."""
    conditions = ["1=1"]
    params: dict[str, Any] = {}
    if subject_type:
        conditions.append("c.subject_type = $subject_type")
        params["subject_type"] = subject_type
    if q:
        conditions.append("(toLower(c.text) CONTAINS $q OR toLower(coalesce(c.object, '')) CONTAINS $q)")
        params["q"] = q.lower()
    if valid is True:
        conditions.append("c.valid_to IS NULL")
    elif valid is False:
        conditions.append("c.valid_to IS NOT NULL")
    if task_key:
        conditions.append("c.task_key = $task_key")
        params["task_key"] = task_key
    where = " AND ".join(conditions)
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size

    count_cypher = f"MATCH (c:Claim) WHERE {where} RETURN count(c) AS total"
    data_cypher = f"""
    MATCH (c:Claim)
    WHERE {where}
    RETURN c.key AS key, c.text AS text, c.confidence AS confidence,
           c.subject_type AS subject_type, c.predicate AS predicate,
           c.object AS object, c.task_key AS task_key,
           toString(c.valid_at) AS valid_at,
           toString(c.valid_to) AS valid_to
    ORDER BY c.valid_at DESC
    SKIP $skip LIMIT $limit
    """
    with neo4j_session(driver) as s:
        total_rec = s.run(count_cypher, **{k: v for k, v in params.items() if k not in ("skip", "limit")}).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()
    return rows, total


__all__ = [
    "ingest_trace",
    "ingest_dialog",
    "ingest_qwenpaw_data_trace",
    "load_trace_yaml",
    "load_synthetic_traces",
    "ingest_host_trace",
    "TraceRecorder",
    "archive_task",
    "delete_task_cascade",
    "batch_archive_tasks",
    "batch_delete_tasks",
    "invalidate_claim",
    "list_claims_global",
]

