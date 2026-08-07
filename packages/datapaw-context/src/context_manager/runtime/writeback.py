"""Step 6: Async write-back for the topology pipeline.

Called after SQL generation & execution result is known.
Runs asynchronously (off critical path via threading or background task).

.. note::
    ``threading.Thread`` does **not** inherit :class:`contextvars.ContextVar` from the
    parent thread. Topology UI sets ``neo4j_database_ctx`` in middleware; async writeback
    must receive ``neo4j_database`` explicitly and re-bind it inside the worker thread,
    otherwise :func:`neo4j_session` falls back to default / wrong logical DB.

Episodic layer (append-only):
  - Task / Step / ToolCall / Claim (handled by TraceRecorder elsewhere)

Procedural layer (this module):
  - Card hit (success/fail) -> record_hit
  - Strategy card creation moved to claim_strategy_synthesizer.py (trace distillation)

All failures are logged, not raised (writeback must not block user response).
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from neo4j import Driver

from ..graph.strategy_card import StrategyCardWriter
from ..graph.trace import TraceRecorder
from ..utils import neo4j_database_ctx
from .anchors import AnchorSet
from .decision_llm import DecisionOutput
from .traversal import TraversalSubgraph

log = logging.getLogger("runtime.writeback")


@contextmanager
def _neo4j_logical_db(neo4j_database: Optional[str]) -> Iterator[None]:
    """Bind Neo4j logical DB inside the current thread (required for worker threads)."""
    db = (neo4j_database or "").strip()
    if not db:
        yield
        return
    token = neo4j_database_ctx.set(db)
    try:
        yield
    finally:
        neo4j_database_ctx.reset(token)


# ---------------------------------------------------------------------- #
# Sync helpers (called inside background thread)
# ---------------------------------------------------------------------- #

def _writeback_success_sync(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    sql: str,
    task_key: str = "",
    plan_key: str = "",
    neo4j_database: Optional[str] = None,
    observable_path_plan: Optional[list[str]] = None,
    thinking_context: str = "agent",
) -> None:
    """Handle procedural write-back on SQL execution success."""
    with _neo4j_logical_db(neo4j_database):
        _writeback_success_sync_inner(
            driver,
            decision,
            anchors,
            subgraph,
            sql,
            task_key,
            plan_key,
            observable_path_plan=observable_path_plan,
            thinking_context=thinking_context,
        )


def _writeback_success_sync_inner(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    sql: str,
    task_key: str,
    plan_key: str,
    *,
    observable_path_plan: Optional[list[str]] = None,
    thinking_context: str = "agent",
) -> None:
    """Record hit on reused card success. Strategy card creation moved to trace claim distillation."""
    if not decision.reuse_key:
        return
    writer = StrategyCardWriter(driver)
    try:
        writer.record_hit(
            decision.reuse_key,
            outcome="success",
            plan_key=plan_key,
        )
        log.info("writeback_success: hit recorded for card %s", decision.reuse_key)
    except Exception as exc:
        log.warning("writeback_success: record_hit failed: %s", exc)

    # Layer 2: Update strategy confidence from execution feedback
    if task_key:
        try:
            from ..graph.claim_strategy_synthesizer import (
                update_strategy_confidence_from_execution,
            )
            update_strategy_confidence_from_execution(
                driver,
                strategy_key=decision.reuse_key,
                task_key=task_key,
                execution_success=True,
            )
        except Exception as exc:
            log.warning(
                "writeback_success: confidence update failed for card %s: %s",
                decision.reuse_key, exc,
            )


def _writeback_failure_sync(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    error: str,
    task_key: str = "",
    llm_summarizer: Optional[Callable[[str, str, str], str]] = None,
    neo4j_database: Optional[str] = None,
    thinking_context: str = "agent",
) -> None:
    """Handle procedural write-back on SQL execution failure."""
    with _neo4j_logical_db(neo4j_database):
        _writeback_failure_sync_inner(
            driver,
            decision,
            anchors,
            subgraph,
            error,
            task_key,
            llm_summarizer,
            thinking_context=thinking_context,
        )


def _writeback_failure_sync_inner(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    error: str,
    task_key: str,
    llm_summarizer: Optional[Callable[[str, str, str], str]],
    *,
    thinking_context: str = "agent",
) -> None:
    """Record hit on reused card failure. Strategy card creation moved to trace claim distillation."""
    if not decision.reuse_key:
        return
    writer = StrategyCardWriter(driver)
    try:
        writer.record_hit(decision.reuse_key, outcome="fail")
        log.info("writeback_failure: fail recorded for card %s", decision.reuse_key)
    except Exception as exc:
        log.warning("writeback_failure: record_hit failed: %s", exc)

    # Layer 2: Update strategy confidence from execution feedback
    if task_key:
        try:
            from ..graph.claim_strategy_synthesizer import (
                update_strategy_confidence_from_execution,
            )
            update_strategy_confidence_from_execution(
                driver,
                strategy_key=decision.reuse_key,
                task_key=task_key,
                execution_success=False,
            )
        except Exception as exc:
            log.warning(
                "writeback_failure: confidence update failed for card %s: %s",
                decision.reuse_key, exc,
            )


# ---------------------------------------------------------------------- #
# Public async entry points
# ---------------------------------------------------------------------- #

def writeback_success(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    sql: str,
    *,
    task_key: str = "",
    plan_key: str = "",
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
    observable_path_plan: Optional[list[str]] = None,
    thinking_context: str = "agent",
) -> None:
    """Async-friendly writeback on success. Spawns daemon thread if async_mode=True."""
    if async_mode:
        t = threading.Thread(
            target=_writeback_success_sync,
            args=(
                driver,
                decision,
                anchors,
                subgraph,
                sql,
                task_key,
                plan_key,
                neo4j_database,
                observable_path_plan,
                thinking_context,
            ),
            daemon=True,
            name="wb_success",
        )
        t.start()
    else:
        _writeback_success_sync(
            driver,
            decision,
            anchors,
            subgraph,
            sql,
            task_key,
            plan_key,
            neo4j_database,
            observable_path_plan,
            thinking_context,
        )


def writeback_failure(
    driver: Driver,
    decision: DecisionOutput,
    anchors: AnchorSet,
    subgraph: TraversalSubgraph,
    error: str,
    *,
    task_key: str = "",
    llm_summarizer: Optional[Callable[[str, str, str], str]] = None,
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
    thinking_context: str = "agent",
) -> None:
    """Async-friendly writeback on failure."""
    if async_mode:
        t = threading.Thread(
            target=_writeback_failure_sync,
            args=(
                driver,
                decision,
                anchors,
                subgraph,
                error,
                task_key,
                llm_summarizer,
                neo4j_database,
                thinking_context,
            ),
            daemon=True,
            name="wb_failure",
        )
        t.start()
    else:
        _writeback_failure_sync(
            driver,
            decision,
            anchors,
            subgraph,
            error,
            task_key,
            llm_summarizer,
            neo4j_database,
            thinking_context,
        )


def _writeback_user_feedback_sync(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    corrected_sql: str = "",
    neo4j_database: Optional[str] = None,
    thinking_context: str = "agent",
) -> None:
    """Handle user semantic feedback (SQL ran but was semantically wrong).

    Record hit on reused card only. Strategy card creation moved to
    claim_strategy_synthesizer.py (trace distillation).
    """
    with _neo4j_logical_db(neo4j_database):
        _writeback_user_feedback_sync_inner(
            driver,
            question,
            db_id,
            task_type,
            reuse_card_key,
            pred_sql,
            node_keys,
            reason,
            corrected_sql,
            thinking_context=thinking_context,
        )


def _writeback_user_feedback_sync_inner(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    corrected_sql: str,
    *,
    thinking_context: str = "agent",
) -> None:
    """Record hit on reused card from user feedback. Strategy card creation moved to trace claim distillation."""
    if not reuse_card_key:
        return
    writer = StrategyCardWriter(driver)
    try:
        writer.record_hit(reuse_card_key, outcome="fail")
        log.info("writeback_feedback: degraded card %s (user marked wrong)", reuse_card_key)
    except Exception as exc:
        log.warning("writeback_feedback: record_hit failed: %s", exc)


def _writeback_memory_confirm_sync(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    *,
    polarity: str = "confirm",
    neo4j_database: Optional[str] = None,
    thinking_context: str = "agent",
) -> None:
    """User-confirmed prior SQL: reinforce reuse card via record_hit."""
    with _neo4j_logical_db(neo4j_database):
        _writeback_memory_confirm_sync_inner(
            driver,
            question,
            db_id,
            task_type,
            reuse_card_key,
            pred_sql,
            node_keys,
            reason,
            polarity=polarity,
            thinking_context=thinking_context,
        )


def _writeback_memory_confirm_sync_inner(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    *,
    polarity: str = "confirm",
    thinking_context: str = "agent",
) -> None:
    """Record hit on reused card from memory confirm. Strategy card creation moved to trace claim distillation."""
    if not reuse_card_key:
        return
    writer = StrategyCardWriter(driver)
    pol = (polarity or "confirm").strip().lower()
    try:
        writer.record_hit(reuse_card_key, outcome="success")
        log.info(
            "writeback_memory_confirm: reinforced card %s (polarity=%s)",
            reuse_card_key,
            pol,
        )
    except Exception as exc:
        log.warning("writeback_memory_confirm: record_hit failed: %s", exc)


def writeback_memory_confirm(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    *,
    polarity: str = "confirm",
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
    thinking_context: str = "agent",
) -> None:
    """memory_only confirm/caveat: record hit on reused card."""
    if async_mode:
        t = threading.Thread(
            target=_writeback_memory_confirm_sync,
            args=(
                driver,
                question,
                db_id,
                task_type,
                reuse_card_key,
                pred_sql,
                node_keys,
                reason,
            ),
            kwargs=dict(
                polarity=polarity,
                neo4j_database=neo4j_database,
                thinking_context=thinking_context,
            ),
            daemon=True,
            name="wb_memory_confirm",
        )
        t.start()
    else:
        _writeback_memory_confirm_sync(
            driver,
            question,
            db_id,
            task_type,
            reuse_card_key,
            pred_sql,
            node_keys,
            reason,
            polarity=polarity,
            neo4j_database=neo4j_database,
            thinking_context=thinking_context,
        )


def _lesson_for_exec_signal(signal: str, detail: str, elapsed_ms: float) -> str:
    """Deterministic lesson text for machine-generated avoid cards."""
    d = (detail or "").strip()
    if signal == "sql_error":
        return f"[exec] PostgreSQL error: {d[:500]}"
    if signal == "empty_result":
        return (
            "[exec] Query returned 0 rows — possible wrong filters, grain, or partition scope "
            "(machine signal after successful parse)."
        )
    return (
        f"[exec] Slow query ({elapsed_ms:.0f} ms) — review predicates, joins, or heavy scans "
        f"(threshold exceeded; machine signal)."
    )


def _writeback_exec_signal_sync(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    node_keys: list[str],
    signal: str,
    detail: str,
    elapsed_ms: float,
    neo4j_database: Optional[str] = None,
) -> None:
    """Record hit on reused card after PG execution anomalies."""
    with _neo4j_logical_db(neo4j_database):
        _writeback_exec_signal_sync_inner(
            driver,
            question=question,
            db_id=db_id,
            task_type=task_type,
            reuse_card_key=reuse_card_key,
            node_keys=node_keys,
            signal=signal,
            detail=detail,
            elapsed_ms=elapsed_ms,
        )


def _writeback_exec_signal_sync_inner(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    node_keys: list[str],
    signal: str,
    detail: str,
    elapsed_ms: float,
) -> None:
    """Record hit on reused card from exec signal. Strategy card creation moved to trace claim distillation."""
    rk = (reuse_card_key or "").strip()
    if not rk:
        return
    writer = StrategyCardWriter(driver)
    outcome = "fail" if signal == "sql_error" else "partial"
    try:
        writer.record_hit(rk, outcome=outcome)
        log.info(
            "writeback_exec_signal: record_hit(%s, %s) signal=%s",
            rk,
            outcome,
            signal,
        )
    except Exception as exc:
        log.warning("writeback_exec_signal: record_hit failed: %s", exc)


def writeback_exec_signal(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    node_keys: list[str],
    signal: str,
    detail: str,
    elapsed_ms: float = 0.0,
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
) -> None:
    """Async writeback after Explorer PG run: sql_error | empty_result | slow_query."""
    if async_mode:
        t = threading.Thread(
            target=_writeback_exec_signal_sync,
            kwargs=dict(
                driver=driver,
                question=question,
                db_id=db_id,
                task_type=task_type,
                reuse_card_key=reuse_card_key,
                node_keys=node_keys,
                signal=signal,
                detail=detail,
                elapsed_ms=elapsed_ms,
                neo4j_database=neo4j_database,
            ),
            daemon=True,
            name="wb_exec_sig",
        )
        t.start()
    else:
        _writeback_exec_signal_sync(
            driver,
            question=question,
            db_id=db_id,
            task_type=task_type,
            reuse_card_key=reuse_card_key,
            node_keys=node_keys,
            signal=signal,
            detail=detail,
            elapsed_ms=elapsed_ms,
            neo4j_database=neo4j_database,
        )


def writeback_user_feedback(
    driver: Driver,
    question: str,
    db_id: str,
    task_type: str,
    reuse_card_key: str,
    pred_sql: str,
    node_keys: list[str],
    reason: str,
    corrected_sql: str = "",
    *,
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
    thinking_context: str = "agent",
) -> None:
    """Async-friendly writeback triggered by user marking a result as wrong."""
    if async_mode:
        t = threading.Thread(
            target=_writeback_user_feedback_sync,
            args=(
                driver,
                question,
                db_id,
                task_type,
                reuse_card_key,
                pred_sql,
                node_keys,
                reason,
                corrected_sql,
                neo4j_database,
                thinking_context,
            ),
            daemon=True,
            name="wb_feedback",
        )
        t.start()
    else:
        _writeback_user_feedback_sync(
            driver,
            question,
            db_id,
            task_type,
            reuse_card_key,
            pred_sql,
            node_keys,
            reason,
            corrected_sql,
            neo4j_database,
            thinking_context,
        )


# ---------------------------------------------------------------------- #
# Multi-turn correction writeback (chat 内反馈闭环)
# ---------------------------------------------------------------------- #
#
# 区别于 writeback_user_feedback（点反馈按钮触发，不带新生成的 SQL 上下文）：
# correction 写回发生在 ``/api/chat_stream`` 第二轮 SQL 已生成的语境下，
# 我们同时拥有「上轮被判错的 SQL + 用户口语化原因 + 本轮新生成的 SQL」三件套，
# 所以可以一次性走通：
#   (a) record_hit(prev_card, fail) —— 让 ANN 复合评分立即跌到 0 附近；
#
# Strategy card creation (avoid / apply / supersede) moved to
# claim_strategy_synthesizer.py (trace claim distillation).
#
# 没有 prev_card 时 (a) 跳过；demo 在"清空记忆 → 第一轮 → 第二轮反馈"
# 之外的边角场景也不会崩。


@dataclass
class CorrectionWritebackSummary:
    """供前端"经验已升级"小标签使用的轻量总结。"""
    prior_card_demoted: bool = False
    avoid_card_key: str = ""
    apply_card_key: str = ""
    superseded: bool = False
    error: str = ""


def _writeback_correction_sync(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    anchor_label_types: list[str],
    prior_card_key: str,
    prior_sql: str,
    corrected_sql: str,
    reason: str,
    node_keys: list[str],
    task_key: str,
    neo4j_database: Optional[str],
    summary: "CorrectionWritebackSummary",
    thinking_context: str = "agent",
    semantics_avoid: str = "",
    semantics_apply: str = "",
) -> None:
    with _neo4j_logical_db(neo4j_database):
        _writeback_correction_sync_inner(
            driver,
            question=question,
            db_id=db_id,
            task_type=task_type,
            anchor_label_types=anchor_label_types,
            prior_card_key=prior_card_key,
            prior_sql=prior_sql,
            corrected_sql=corrected_sql,
            reason=reason,
            node_keys=node_keys,
            task_key=task_key,
            summary=summary,
            thinking_context=thinking_context,
            semantics_avoid=semantics_avoid,
            semantics_apply=semantics_apply,
        )


def _writeback_correction_sync_inner(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    anchor_label_types: list[str],
    prior_card_key: str,
    prior_sql: str,
    corrected_sql: str,
    reason: str,
    node_keys: list[str],
    task_key: str,
    summary: "CorrectionWritebackSummary",
    thinking_context: str = "agent",
    semantics_avoid: str = "",
    semantics_apply: str = "",
) -> None:
    """Record hit on prior card. Strategy card creation moved to trace claim distillation."""
    if not prior_card_key:
        return
    writer = StrategyCardWriter(driver)
    # (a) demote 上一轮被判错的卡：record_hit(fail) 让滑动均值 success_rate 立刻塌缩
    try:
        writer.record_hit(prior_card_key, outcome="fail")
        summary.prior_card_demoted = True
        log.info(
            "writeback_correction: prior card %s demoted via record_hit(fail)",
            prior_card_key,
        )
    except Exception as exc:
        log.warning("writeback_correction: record_hit failed: %s", exc)


def writeback_correction(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    task_type: str,
    anchor_label_types: list[str],
    prior_card_key: str,
    prior_sql: str,
    corrected_sql: str,
    reason: str,
    node_keys: list[str],
    task_key: str = "",
    neo4j_database: Optional[str] = None,
    async_mode: bool = True,
    thinking_context: str = "agent",
    semantics_avoid: str = "",
    semantics_apply: str = "",
) -> "CorrectionWritebackSummary":
    """多轮 chat 内"反馈→修正"的写回闭环（同步 / 异步均可）。

    Returns:
        :class:`CorrectionWritebackSummary` —— 异步模式下返回的 summary 仍是
        empty 占位（实际字段由后台线程填充，前端不强依赖即时数据），
        同步模式下字段完整可用。
    """
    summary = CorrectionWritebackSummary()
    args = dict(
        driver=driver,
        question=question,
        db_id=db_id,
        task_type=task_type,
        anchor_label_types=anchor_label_types,
        prior_card_key=prior_card_key,
        prior_sql=prior_sql,
        corrected_sql=corrected_sql,
        reason=reason,
        node_keys=node_keys,
        task_key=task_key,
        neo4j_database=neo4j_database,
        summary=summary,
        thinking_context=thinking_context,
        semantics_avoid=semantics_avoid,
        semantics_apply=semantics_apply,
    )
    if async_mode:
        t = threading.Thread(
            target=_writeback_correction_sync,
            kwargs=args,
            daemon=True,
            name="wb_correction",
        )
        t.start()
    else:
        _writeback_correction_sync(**args)
    return summary


def record_outcome_dispatch(
    driver: Driver,
    *,
    question: str,
    db_id: str,
    sql: str,
    exec_status: str,
    decision: Optional[Any],
    anchors: Any,
    subgraph: Any,
    feedback_signal: Optional[str] = None,
    feedback_reason: str = "",
    corrected_sql: str = "",
    task_key: str = "",
    plan_key: str = "",
    neo4j_database: Optional[str] = None,
) -> str:
    """Unified dispatch entry for context.record_outcome (async, daemon threads).

    Routes to the appropriate ``writeback_*`` function based on exec_status and
    feedback_signal. Returns the writeback kind string for caller logging.

    Routing table:
      exec_status=success, no feedback          -> writeback_success
      exec_status in {error,empty,slow}         -> writeback_failure (exec signal)
      feedback=avoid/supersede + corrected_sql  -> writeback_correction
      feedback=confirm/caveat                   -> writeback_memory_confirm
      feedback=fail, no corrected_sql           -> writeback_user_feedback
    """
    dec_task_type = str(getattr(decision, "task_type", "") or "")
    dec_reuse_key = str(getattr(decision, "reuse_key", "") or "")
    node_keys = list(getattr(subgraph, "node_keys", []) or []) if subgraph else []

    anchor_label_types: list[str] = []
    for a in getattr(anchors, "anchors", []) or []:
        lbl = getattr(a, "label", "")
        if lbl and lbl not in anchor_label_types:
            anchor_label_types.append(lbl)

    kind = "writeback_skipped"

    if feedback_signal in ("avoid", "supersede") and corrected_sql:
        kind = "writeback_correction"
        writeback_correction(
            driver,
            question=question,
            db_id=db_id,
            task_type=dec_task_type,
            anchor_label_types=anchor_label_types,
            prior_card_key=dec_reuse_key,
            prior_sql=sql,
            corrected_sql=corrected_sql,
            reason=feedback_reason,
            node_keys=node_keys,
            task_key=task_key,
            neo4j_database=neo4j_database,
            async_mode=True,
        )
    elif feedback_signal in ("confirm", "caveat"):
        kind = "writeback_memory_confirm"
        writeback_memory_confirm(
            driver,
            question,
            db_id,
            dec_task_type,
            dec_reuse_key,
            sql,
            node_keys,
            feedback_reason,
            polarity=feedback_signal,
            neo4j_database=neo4j_database,
            async_mode=True,
        )
    elif feedback_signal == "fail" and not corrected_sql:
        kind = "writeback_user_feedback"
        writeback_user_feedback(
            driver,
            question,
            db_id,
            dec_task_type,
            dec_reuse_key,
            sql,
            node_keys,
            feedback_reason,
            neo4j_database=neo4j_database,
            async_mode=True,
        )
    elif exec_status == "success" and not feedback_signal:
        kind = "writeback_success"
        writeback_success(
            driver,
            decision,
            anchors,
            subgraph,
            sql,
            task_key=task_key,
            plan_key=plan_key,
            neo4j_database=neo4j_database,
            async_mode=True,
        )
    elif exec_status in ("error", "empty", "slow"):
        kind = "writeback_failure"
        error_msg = f"{exec_status}: {sql[:200]}"
        writeback_failure(
            driver,
            decision,
            anchors,
            subgraph,
            error_msg,
            task_key=task_key,
            neo4j_database=neo4j_database,
            async_mode=True,
        )

    return kind


__all__ = [
    "CorrectionWritebackSummary",
    "record_outcome_dispatch",
    "writeback_exec_signal",
    "writeback_success",
    "writeback_failure",
    "writeback_user_feedback",
    "writeback_memory_confirm",
    "writeback_correction",
]
