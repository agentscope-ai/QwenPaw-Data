"""Embedding 重建：模型/维度变更时全量重新向量化 + 索引重建。

当 Embedding 的 Provider、Model、Base URL 或 Dim 变化时，
``model_config_api.PUT /embedding`` 自动创建重建任务并启动后台 Worker。

重建期间 ``is_embedding_rebuild_active()`` 返回 True，
``server.py`` 中间件据此阻塞除状态查询和健康检查外的所有请求。

任务状态持久化到 ``$EMBEDDING_JOBS_DIR``（默认 ``<project_root>/data/jobs/``）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from datapaw.context.paths import embedding_jobs_dir

from .config import CFG
from .utils import get_logger, neo4j_database_ctx, neo4j_session

log = get_logger("embedding_rebuild")

_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_JOBS_DIR = embedding_jobs_dir()


# ---------------------------------------------------------------------- #
# Data model
# ---------------------------------------------------------------------- #

@dataclass
class RebuildProgress:
    phase: str = "pending"
    current_label: str = ""
    labels_done: int = 0
    labels_total: int = 0


@dataclass
class EmbeddingRebuildJob:
    job_id: str
    status: str = "pending"
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    progress: RebuildProgress = field(default_factory=RebuildProgress)
    config_snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EmbeddingRebuildJob":
        prog = d.pop("progress", {})
        job = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if isinstance(prog, dict):
            job.progress = RebuildProgress(**{k: v for k, v in prog.items() if k in RebuildProgress.__dataclass_fields__})
        return job


# ---------------------------------------------------------------------- #
# Persistent store
# ---------------------------------------------------------------------- #

class EmbeddingRebuildStore:

    def __init__(self):
        raw = (os.getenv("EMBEDDING_JOBS_DIR") or "").strip()
        self._dir = Path(raw) if raw else _DEFAULT_JOBS_DIR
        self._lock = threading.Lock()
        self._active_job_id: Optional[str] = None
        self._scan_active()

    def _scan_active(self) -> None:
        if not self._dir.is_dir():
            return
        for p in sorted(self._dir.glob("emb-rebuild-*.json"), reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("status") in ("pending", "running"):
                    self._active_job_id = data["job_id"]
                    return
            except (OSError, json.JSONDecodeError, KeyError):
                continue

    def _job_path(self, job_id: str) -> Path:
        return self._dir / f"emb-rebuild-{job_id}.json"

    def _save(self, job: EmbeddingRebuildJob) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._job_path(job.job_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._job_path(job.job_id))

    def create_job(self, config_snapshot: dict) -> EmbeddingRebuildJob:
        with self._lock:
            if self._active_job_id:
                existing = self.get_job(self._active_job_id)
                if existing and existing.status in ("pending", "running"):
                    raise RuntimeError(
                        f"Another rebuild job is already active: {self._active_job_id}"
                    )
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            job = EmbeddingRebuildJob(
                job_id=job_id,
                status="pending",
                created_at=datetime.now(timezone.utc).isoformat(),
                config_snapshot=config_snapshot,
            )
            self._save(job)
            self._active_job_id = job_id
            log.info("Created rebuild job: %s", job_id)
            return job

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        current_label: Optional[str] = None,
        labels_done: Optional[int] = None,
        labels_total: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._load_job(job_id)
            if not job:
                log.warning("update_job: job %s not found", job_id)
                return
            if status:
                job.status = status
                if status == "running" and not job.started_at:
                    job.started_at = datetime.now(timezone.utc).isoformat()
                if status in ("success", "failed"):
                    job.finished_at = datetime.now(timezone.utc).isoformat()
                    self._active_job_id = None
            if phase is not None:
                job.progress.phase = phase
            if current_label is not None:
                job.progress.current_label = current_label
            if labels_done is not None:
                job.progress.labels_done = labels_done
            if labels_total is not None:
                job.progress.labels_total = labels_total
            if error is not None:
                job.error = error
            self._save(job)

    def _load_job(self, job_id: str) -> Optional[EmbeddingRebuildJob]:
        p = self._job_path(job_id)
        if not p.is_file():
            return None
        try:
            return EmbeddingRebuildJob.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.warning("Failed to read job %s: %s", job_id, exc)
            return None

    def get_job(self, job_id: str) -> Optional[EmbeddingRebuildJob]:
        return self._load_job(job_id)

    def get_latest_active(self) -> Optional[EmbeddingRebuildJob]:
        with self._lock:
            if self._active_job_id:
                return self._load_job(self._active_job_id)
        return None

    def get_latest_job(self) -> Optional[EmbeddingRebuildJob]:
        if not self._dir.is_dir():
            return None
        files = sorted(self._dir.glob("emb-rebuild-*.json"), reverse=True)
        for p in files:
            try:
                return EmbeddingRebuildJob.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return None

    def is_active(self) -> tuple[bool, Optional[str]]:
        jid = self._active_job_id
        return (True, jid) if jid else (False, None)

    def mark_retryable(self, job_id: str) -> None:
        with self._lock:
            job = self._load_job(job_id)
            if not job:
                raise ValueError(f"Job {job_id} not found")
            if job.status != "failed":
                raise ValueError(f"Job {job_id} is {job.status}, only failed jobs can be retried")
            job.status = "pending"
            job.error = None
            job.started_at = None
            job.finished_at = None
            job.progress = RebuildProgress()
            self._save(job)
            self._active_job_id = job_id


_store_instance: Optional[EmbeddingRebuildStore] = None
_store_lock = threading.Lock()


def get_rebuild_store() -> EmbeddingRebuildStore:
    global _store_instance
    if _store_instance is not None:
        return _store_instance
    with _store_lock:
        if _store_instance is not None:
            return _store_instance
        _store_instance = EmbeddingRebuildStore()
        return _store_instance


def is_embedding_rebuild_active() -> tuple[bool, Optional[str]]:
    return get_rebuild_store().is_active()


# ---------------------------------------------------------------------- #
# Rebuild worker
# ---------------------------------------------------------------------- #

_ALL_REBUILD_LABELS = [
    "Metric", "Dimension", "Column", "DatasetColumn", "Dataset",
    "Event", "Entity",
    "Strategy",
    "Claim",
    "Task", "Step", "Turn", "ToolCall", "Experience",
]


def _drop_all_vector_indexes(driver: Any) -> None:
    from .graph.schema_init import _VECTOR_INDEXES
    with neo4j_session(driver) as s:
        for name, _, _ in _VECTOR_INDEXES:
            s.run(f"DROP INDEX {name} IF EXISTS")
    log.info("Dropped all %d vector indexes", len(_VECTOR_INDEXES))


def _rebuild_standard_labels(driver: Any, store: EmbeddingRebuildStore, job_id: str, offset: int) -> int:
    from .graph.embeddings import index_embeddings
    labels = ["metric", "dimension", "column", "datasetcolumn", "dataset", "event", "entity"]
    for i, label in enumerate(labels):
        store.update_job(job_id, current_label=label.title(), labels_done=offset + i)
        index_embeddings(driver, scope=[label], reset=True, ensure_indexes=False)
    return len(labels)


def _rebuild_strategy_embeddings(driver: Any) -> None:
    from .embedder import embed_one

    with neo4j_session(driver) as s:
        rows = s.run(
            "MATCH (st:Strategy) "
            "WHERE st.semantics IS NOT NULL AND st.semantics <> '' "
            "RETURN st.key AS key, st.semantics AS semantics"
        ).data()

    if not rows:
        log.info("[Strategy] no nodes with semantics to re-embed")
        return

    log.info("[Strategy] re-embedding %d nodes (signature_emb + embedding)", len(rows))
    batch: list[dict] = []
    for r in rows:
        text = (r.get("semantics") or "").strip()
        if not text:
            continue
        try:
            vec = embed_one(text)
        except Exception as exc:
            log.warning("[Strategy] embed failed for %s: %s", r["key"], exc)
            continue
        h = hashlib.sha1()
        h.update(text.encode("utf-8"))
        batch.append({"key": r["key"], "vec": vec, "h": h.hexdigest()})

        if len(batch) >= 32:
            _write_strategy_batch(driver, batch)
            batch = []

    if batch:
        _write_strategy_batch(driver, batch)
    log.info("[Strategy] done: %d nodes", len(rows))


def _write_strategy_batch(driver: Any, rows: list[dict]) -> None:
    with neo4j_session(driver) as s:
        s.run(
            "UNWIND $rows AS row "
            "MATCH (st:Strategy {key: row.key}) "
            "SET st.signature_emb = row.vec, "
            "    st.embedding = row.vec, "
            "    st.embedding_hash = row.h",
            rows=rows,
        )


def _rebuild_trace_label(driver: Any, label: str, text_fn, *, fetch_cypher: str, write_cypher: str) -> None:
    from .embedder import embed, embed_one

    with neo4j_session(driver) as s:
        rows = s.run(fetch_cypher).data()

    if not rows:
        log.info("[%s] no nodes to re-embed", label)
        return

    log.info("[%s] re-embedding %d nodes", label, len(rows))
    batch_size = 32
    written = 0

    for chunk_start in range(0, len(rows), batch_size):
        chunk = rows[chunk_start: chunk_start + batch_size]
        texts = []
        valid_rows = []
        for r in chunk:
            text = text_fn(r)
            if not text:
                continue
            texts.append(text)
            valid_rows.append(r)

        if not texts:
            continue

        try:
            vecs = embed(texts)
        except Exception as exc:
            log.warning("[%s] embed batch failed: %s", label, exc)
            continue

        write_rows = []
        for r, vec, text in zip(valid_rows, vecs, texts):
            h = hashlib.sha1()
            h.update(text.encode("utf-8"))
            write_rows.append({"key": r["key"], "vec": vec, "h": h.hexdigest()})

        with neo4j_session(driver) as s:
            s.run(write_cypher, rows=write_rows)
        written += len(write_rows)

    log.info("[%s] done: written=%d total=%d", label, written, len(rows))


def _rebuild_toolcall_observation(driver: Any) -> None:
    from .embedder import embed

    fetch = (
        "MATCH (tc:ToolCall) "
        "WHERE tc.observation_summary IS NOT NULL AND tc.observation_summary <> '' "
        "RETURN tc.key AS key, tc.observation_summary AS summary"
    )
    write = (
        "UNWIND $rows AS row "
        "MATCH (tc:ToolCall {key: row.key}) "
        "SET tc.observation_embedding = row.vec, tc.observation_embedding_hash = row.h"
    )

    with neo4j_session(driver) as s:
        rows = s.run(fetch).data()

    if not rows:
        log.info("[ToolCall.observation] no nodes to re-embed")
        return

    log.info("[ToolCall.observation] re-embedding %d nodes", len(rows))
    batch_size = 32
    written = 0

    for chunk_start in range(0, len(rows), batch_size):
        chunk = rows[chunk_start: chunk_start + batch_size]
        texts = [f"Observation: {(r.get('summary') or '').strip()[:500]}" for r in chunk]
        valid = [(r, t) for r, t in zip(chunk, texts) if t.strip() != "Observation:"]
        if not valid:
            continue
        valid_rows, valid_texts = zip(*valid)
        try:
            vecs = embed(list(valid_texts))
        except Exception as exc:
            log.warning("[ToolCall.observation] embed failed: %s", exc)
            continue
        write_rows = []
        for r, vec, text in zip(valid_rows, vecs, valid_texts):
            h = hashlib.sha1()
            h.update(text.encode("utf-8"))
            write_rows.append({"key": r["key"], "vec": vec, "h": h.hexdigest()})
        with neo4j_session(driver) as s:
            s.run(write, rows=write_rows)
        written += len(write_rows)

    log.info("[ToolCall.observation] done: written=%d", written)


# Trace label text derivation + Cypher definitions
_TRACE_DEFS: list[dict[str, Any]] = [
    {
        "label": "Claim",
        "fetch": (
            "MATCH (cl:Claim) WHERE cl.text IS NOT NULL AND cl.text <> '' "
            "RETURN cl.key AS key, cl.text AS text, coalesce(cl.predicate, '') AS predicate"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (cl:Claim {key: row.key}) "
            "SET cl.embedding = row.vec, cl.embedding_hash = row.h"
        ),
        "text_fn": lambda r: f"Claim [{r.get('predicate', '')}]: {r.get('text', '')}".strip(),
    },
    {
        "label": "Task",
        "fetch": (
            "MATCH (t:Task) WHERE t.goal IS NOT NULL "
            "RETURN t.key AS key, coalesce(t.goal, '') AS goal, "
            "coalesce(t.task_signature, '') AS task_signature"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (t:Task {key: row.key}) "
            "SET t.embedding = row.vec, t.embedding_hash = row.h"
        ),
        "text_fn": lambda r: (
            f"Task: {(r.get('goal') or '').strip()}"
            + (f" [{(r.get('task_signature') or '').strip()[:32]}]" if (r.get('task_signature') or '').strip() else "")
        ),
    },
    {
        "label": "Step",
        "fetch": (
            "MATCH (p:Step) WHERE p.intent IS NOT NULL "
            "RETURN p.key AS key, coalesce(p.intent, '') AS intent, "
            "coalesce(p.tool_hint, '') AS tool_hint"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (p:Step {key: row.key}) "
            "SET p.embedding = row.vec, p.embedding_hash = row.h"
        ),
        "text_fn": lambda r: (
            f"Step: {(r.get('intent') or '').strip()}"
            + (f" (tool: {(r.get('tool_hint') or '').strip()})" if (r.get('tool_hint') or '').strip() else "")
        ),
    },
    {
        "label": "Turn",
        "fetch": (
            "MATCH (tn:Turn) "
            "RETURN tn.key AS key, coalesce(tn.role, 'user') AS role, "
            "coalesce(tn.content, '') AS content"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (tn:Turn {key: row.key}) "
            "SET tn.embedding = row.vec, tn.embedding_hash = row.h"
        ),
        "text_fn": lambda r: f"{(r.get('role') or 'user')}: {(r.get('content') or '').strip()[:500]}",
    },
    {
        "label": "ToolCall",
        "fetch": (
            "MATCH (tc:ToolCall) "
            "RETURN tc.key AS key, coalesce(tc.tool_name, '') AS tool_name, "
            "coalesce(tc.args_json, '') AS args_json, coalesce(tc.status, '') AS status"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (tc:ToolCall {key: row.key}) "
            "SET tc.embedding = row.vec, tc.embedding_hash = row.h"
        ),
        "text_fn": lambda r: (
            f"ToolCall: {(r.get('tool_name') or '').strip()}"
            f"({(r.get('args_json') or '').strip()[:200]}) "
            f"[{(r.get('status') or '').strip()}]"
        ),
    },
    {
        "label": "Experience",
        "fetch": (
            "MATCH (e:Experience) "
            "RETURN e.key AS key, coalesce(e.outcome, '') AS outcome, "
            "coalesce(e.key_insight, '') AS key_insight"
        ),
        "write": (
            "UNWIND $rows AS row MATCH (e:Experience {key: row.key}) "
            "SET e.embedding = row.vec, e.embedding_hash = row.h"
        ),
        "text_fn": lambda r: f"Experience [{(r.get('outcome') or '').strip()}]: {(r.get('key_insight') or '').strip()}",
    },
]


def _run_rebuild(driver: Any, job_id: str, neo4j_database: Optional[str] = None) -> None:
    if neo4j_database:
        neo4j_database_ctx.set(neo4j_database)

    store = get_rebuild_store()
    total = len(_ALL_REBUILD_LABELS)
    store.update_job(job_id, status="running", phase="drop-indexes", labels_total=total)

    try:
        # Phase 1: drop all vector indexes
        _drop_all_vector_indexes(driver)

        # Phase 2: re-embedding
        store.update_job(job_id, phase="re-embedding")

        from .embedder import warmup_embedding_model
        warmup_embedding_model()

        # Group A: standard 7 labels via index_embeddings
        count = _rebuild_standard_labels(driver, store, job_id, offset=0)

        # Group B: Strategy (signature_emb + embedding)
        store.update_job(job_id, current_label="Strategy", labels_done=count)
        _rebuild_strategy_embeddings(driver)
        count += 1

        # Group C + D: Claim + Trace nodes
        for td in _TRACE_DEFS:
            store.update_job(job_id, current_label=td["label"], labels_done=count)
            _rebuild_trace_label(
                driver,
                td["label"],
                td["text_fn"],
                fetch_cypher=td["fetch"],
                write_cypher=td["write"],
            )
            count += 1

        # ToolCall observation_embedding (not a separate label, but extra property)
        _rebuild_toolcall_observation(driver)

        # Phase 3: rebuild vector indexes with new dimension
        store.update_job(job_id, phase="rebuild-indexes", labels_done=total)
        from .model_config_store import get_model_config_store
        from .graph.schema_init import init_vector_indexes
        new_dim = get_model_config_store().embed_dim
        init_vector_indexes(driver, embed_dim=new_dim)

        # Phase 4: verify
        store.update_job(job_id, phase="verify")
        _verify_coverage(driver)

        store.update_job(job_id, status="success", phase="done", labels_done=total)
        log.info("Embedding rebuild completed: %s", job_id)

    except Exception as exc:
        log.exception("Embedding rebuild failed: %s", job_id)
        store.update_job(job_id, status="failed", error=str(exc)[:2000])


def _verify_coverage(driver: Any) -> None:
    labels_to_check = [
        ("Metric", "embedding"), ("Dimension", "embedding"), ("Column", "embedding"),
        ("DatasetColumn", "embedding"), ("Dataset", "embedding"),
        ("Event", "embedding"), ("Entity", "embedding"),
        ("Strategy", "signature_emb"), ("Claim", "embedding"),
    ]
    for label, prop in labels_to_check:
        with neo4j_session(driver) as s:
            rec = s.run(
                f"MATCH (n:{label}) WHERE n.{prop} IS NULL RETURN count(n) AS c"
            ).single()
            missing = int(rec["c"]) if rec else 0
        if missing > 0:
            log.warning("[verify] %s: %d nodes missing %s", label, missing, prop)


def start_rebuild(driver: Any, job_id: str, neo4j_database: Optional[str] = None) -> None:
    db = neo4j_database or neo4j_database_ctx.get() or CFG.neo4j_database
    t = threading.Thread(
        target=_run_rebuild,
        args=(driver, job_id, db),
        daemon=True,
        name=f"emb-rebuild-{job_id[:16]}",
    )
    t.start()
    log.info("Rebuild worker started: %s", job_id)
