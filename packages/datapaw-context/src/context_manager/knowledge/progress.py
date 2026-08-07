"""Thread-safe progress snapshot for knowledge ingest (CLI + web monitor)."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from typing import Any, Optional

# 监视快照里单块原文最大长度（超出截断并标记 chunk_text_truncated）
_MAX_CHUNK_TEXT_IN_SNAPSHOT = 14_000

# LLM 轨迹：单条 message content、整段 trace JSON 上限（防止快照过大）
_MAX_LLM_MSG_CHARS = 24_000
_MAX_LLM_TRACE_JSON_CHARS = 350_000
_MAX_LLM_TURNS = 100


def _truncate_chunk_text(s: str) -> tuple[str, bool]:
    if len(s) <= _MAX_CHUNK_TEXT_IN_SNAPSHOT:
        return s, False
    return s[:_MAX_CHUNK_TEXT_IN_SNAPSHOT], True


def _shrink_response_for_trace(phase: str, obj: Any) -> Any:
    """缩小写入监视 JSON 的 LLM 响应（保留结构，截断超长数组）。"""
    if not isinstance(obj, dict):
        return copy.deepcopy(obj) if isinstance(obj, list) else obj
    o: dict[str, Any] = copy.deepcopy(obj)
    o.pop("_ingest_from_cache", None)
    o.pop("_ingest_skip_llm", None)
    if phase != "pass_a":
        if phase == "pass_b":
            clusters = o.get("clusters")
            if isinstance(clusters, list) and len(clusters) > 120:
                rest = len(clusters) - 120
                o["clusters"] = clusters[:120] + [{"_note": f"… 另有 {rest} 个 cluster 已省略"}]
        elif phase == "pass_c":
            kt = o.get("kg_topology")
            if isinstance(kt, dict):
                nodes = kt.get("nodes")
                rels = kt.get("relationships")
                if isinstance(nodes, list) and len(nodes) > 80:
                    o["kg_topology"] = dict(kt)
                    o["kg_topology"]["nodes"] = nodes[:80] + [{"_note": f"… 另有 {len(nodes) - 80} 个 node 已省略"}]
                if isinstance(rels, list) and len(rels) > 120:
                    if "kg_topology" not in o:
                        o["kg_topology"] = dict(kt)
                    o["kg_topology"]["relationships"] = rels[:120] + [
                        {"_note": f"… 另有 {len(rels) - 120} 条 relationship 已省略"}
                    ]
        elif phase == "pass_d":
            edges = o.get("edges")
            if isinstance(edges, list) and len(edges) > 120:
                rest = len(edges) - 120
                o["edges"] = edges[:120] + [{"_note": f"… 另有 {rest} 条 edge 已省略"}]
            ca = o.get("connection_assessment")
            if isinstance(ca, str) and len(ca) > 4000:
                o["connection_assessment"] = ca[:4000] + "\n… [truncated]"
        return o
    limits = (
        ("entities", 150),
        ("events", 100),
        ("entity_relations", 100),
    )
    for key, lim in limits:
        arr = o.get(key)
        if isinstance(arr, list) and len(arr) > lim:
            n = len(arr) - lim
            o[key] = arr[:lim] + [{"_note": f"… 另有 {n} 条已省略"}]
    return o


class IngestProgress:
    """Collects phase / chunk / event stream for UI polling."""

    def __init__(self, *, max_events: int = 400) -> None:
        self._lock = threading.Lock()
        self._max_events = max_events
        self._reset()

    def _reset(self) -> None:
        self.run_id: str = ""
        self.status: str = "idle"  # idle | running | done | error
        self.phase: str = ""
        self.message: str = ""
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.source_path: str = ""
        self.chunks_total: int = 0
        self.chunk_index: int = 0
        self.chunk_summaries: list[dict[str, Any]] = []
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None
        self.events: list[dict[str, Any]] = []
        self.llm_trace: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "source_path": self.source_path,
                "chunks_total": self.chunks_total,
                "chunk_index": self.chunk_index,
                "chunk_summaries": copy.deepcopy(self.chunk_summaries[-50:]),
                "llm_trace": copy.deepcopy(self.llm_trace[-_MAX_LLM_TURNS:]),
                "result": copy.deepcopy(self.result) if self.result else None,
                "error": self.error,
                "events": copy.deepcopy(self.events[-self._max_events :]),
            }

    def _emit(self, level: str, phase: str, message: str, detail: Any = None) -> None:
        row: dict[str, Any] = {
            "t": time.time(),
            "level": level,
            "phase": phase,
            "message": message,
        }
        if detail is not None:
            row["detail"] = detail
        self.events.append(row)
        if len(self.events) > self._max_events * 2:
            self.events = self.events[-self._max_events :]

    def begin_run(
        self,
        *,
        source_path: str,
        max_chunks: int,
        dry_run: bool,
        skip_llm: bool,
        pass_d_apply_edges: bool,
        min_chars: int,
        max_chars: int,
        dataset: Optional[str],
        pass_c_cluster_batch_size: Optional[int] = None,
        pass_c_max_outer_rounds: Optional[int] = None,
    ) -> None:
        with self._lock:
            self._reset()
            self.run_id = uuid.uuid4().hex[:12]
            self.status = "running"
            self.phase = "init"
            self.message = "starting"
            self.started_at = time.time()
            self.source_path = source_path
            self._emit(
                "info",
                "init",
                "run started",
                {
                    "max_chunks": max_chunks,
                    "dry_run": dry_run,
                    "skip_llm": skip_llm,
                    "pass_d_apply_edges": pass_d_apply_edges,
                    "min_chars": min_chars,
                    "max_chars": max_chars,
                    "dataset": dataset,
                    "pass_c_cluster_batch_size": pass_c_cluster_batch_size,
                    "pass_c_max_outer_rounds": pass_c_max_outer_rounds,
                },
            )

    def chunking_done(self, n_chunks: int) -> None:
        with self._lock:
            self.chunks_total = n_chunks
            self.phase = "chunking"
            self.message = f"{n_chunks} chunks"
            self._emit("info", "chunking", f"split into {n_chunks} chunks", {"n_chunks": n_chunks})

    def pass_a_chunk_start(self, idx: int, total: int, source_doc: str) -> None:
        with self._lock:
            self.chunk_index = idx + 1
            self.phase = "pass_a"
            self.message = f"chunk {idx + 1}/{total} ({source_doc})"
            self._emit(
                "info",
                "pass_a",
                f"Pass A start chunk {idx + 1}/{total}",
                {"source_doc": source_doc, "chunk_idx": idx},
            )

    def pass_a_chunk_done(
        self,
        idx: int,
        total: int,
        source_doc: str,
        *,
        n_entities: int,
        n_events: int,
        confidence: Optional[float],
        from_cache: bool,
        char_start: int,
        char_end: int,
        chunk_text: str,
        entities: list[dict[str, Any]],
        events_preview: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            text_stored, trunc = _truncate_chunk_text(chunk_text)
            summary: dict[str, Any] = {
                "chunk_idx": idx,
                "source_doc": source_doc,
                "n_entities": n_entities,
                "n_events": n_events,
                "self_confidence": confidence,
                "from_cache": from_cache,
                "char_start": char_start,
                "char_end": char_end,
                "chunk_char_len": len(chunk_text),
                "chunk_text": text_stored,
                "chunk_text_truncated": trunc,
                "entities": entities,
                "events_preview": events_preview,
            }
            self.chunk_summaries.append(summary)
            emit_detail = {
                "chunk_idx": idx,
                "source_doc": source_doc,
                "n_entities": n_entities,
                "n_events": n_events,
                "from_cache": from_cache,
                "chunk_char_len": len(chunk_text),
                "char_start": char_start,
                "char_end": char_end,
            }
            self._emit(
                "ok" if n_entities or n_events else "warn",
                "pass_a",
                f"Pass A done chunk {idx + 1}/{total}",
                emit_detail,
            )

    def pass_a_chunk_error(
        self,
        idx: int,
        total: int,
        source_doc: str,
        err: str,
        *,
        char_start: int = 0,
        char_end: int = 0,
        chunk_preview: str = "",
    ) -> None:
        with self._lock:
            prev, trunc = _truncate_chunk_text(chunk_preview) if chunk_preview else ("", False)
            self.chunk_summaries.append(
                {
                    "chunk_idx": idx,
                    "source_doc": source_doc,
                    "error": err,
                    "char_start": char_start,
                    "char_end": char_end,
                    "chunk_char_len": len(chunk_preview) if chunk_preview else 0,
                    "chunk_text": prev,
                    "chunk_text_truncated": trunc,
                    "entities": [],
                    "events_preview": [],
                }
            )
            self._emit(
                "error",
                "pass_a",
                f"Pass A failed chunk {idx + 1}/{total}: {err}",
                {"source_doc": source_doc, "char_start": char_start, "char_end": char_end},
            )

    def pass_b_done(self, n_surfaces: int, n_clusters: int) -> None:
        with self._lock:
            self.phase = "pass_b"
            self.message = f"{n_clusters} clusters from {n_surfaces} surfaces"
            self._emit(
                "info",
                "pass_b",
                "Pass B complete",
                {"n_surfaces": n_surfaces, "n_clusters": n_clusters},
            )

    def pass_c_done(self, *, n_kg_nodes: int = 0, n_kg_rels: int = 0) -> None:
        with self._lock:
            self.phase = "pass_c"
            self.message = f"{n_kg_nodes} kg nodes, {n_kg_rels} kg rels"
            self._emit(
                "info",
                "pass_c",
                "Pass C complete",
                {"n_kg_nodes": n_kg_nodes, "n_kg_rels": n_kg_rels},
            )

    def pass_c_batch_done(self, batch_index: int, n_batches: int, *, n_nodes: int, n_rels: int) -> None:
        with self._lock:
            self.phase = "pass_c"
            self.message = f"Pass C batch {batch_index}/{n_batches}: {n_nodes} nd, {n_rels} rel"
            self._emit(
                "info",
                "pass_c",
                f"Pass C batch {batch_index}/{n_batches} done",
                {
                    "batch_index": batch_index,
                    "n_batches": n_batches,
                    "n_kg_nodes": n_nodes,
                    "n_kg_rels": n_rels,
                },
            )

    def pass_c_neo4j_written(
        self,
        *,
        dry_run: bool,
        n_kg_nodes: int,
        n_kg_rels: int,
        n_entities: int,
        n_related_to: int,
        n_has_instance: int,
    ) -> None:
        """After ``ingest_all`` / ``write_kg_topology_v4``: topology counts + Neo4j apply stats for the monitor."""
        with self._lock:
            self.phase = "pass_c"
            detail: dict[str, Any] = {
                "n_kg_nodes": n_kg_nodes,
                "n_kg_rels": n_kg_rels,
                "dry_run": dry_run,
                "n_entities_written": n_entities,
                "n_related_to_written": n_related_to,
                "n_has_instance_written": n_has_instance,
            }
            if dry_run:
                self.message = f"{n_kg_nodes} kg nd, {n_kg_rels} kg rel (dry_run, 未落库)"
                self._emit("info", "pass_c", "Pass C topology (Neo4j 写入已跳过 dry_run)", detail)
            else:
                self.message = (
                    f"{n_kg_nodes} kg nd, {n_kg_rels} kg rel → Neo4j {n_entities} ent, "
                    f"{n_related_to} RELATED_TO, {n_has_instance} HAS_INSTANCE"
                )
                self._emit("info", "pass_c", "Pass C applied to Neo4j (kg_topology)", detail)

    def pass_d_done(
        self,
        n_edges: int,
        *,
        n_proposed: Optional[int] = None,
        preview_only: bool = False,
    ) -> None:
        with self._lock:
            self.phase = "pass_d"
            np = n_proposed if n_proposed is not None else n_edges
            if preview_only:
                self.message = f"{np} proposed, {n_edges} applied (preview)"
                self._emit(
                    "info",
                    "pass_d",
                    "Pass D complete (proposals only)",
                    {"n_edges": n_edges, "n_proposed": np, "preview_only": True},
                )
            else:
                self.message = f"{n_edges} cross-graph edges"
                detail: dict[str, Any] = {"n_edges": n_edges}
                if n_proposed is not None and n_proposed != n_edges:
                    detail["n_proposed"] = n_proposed
                self._emit("info", "pass_d", "Pass D complete", detail)

    def pass_d_skipped(self, reason: str) -> None:
        """Pass D 未执行（例如 dry_run / skip_llm）；便于监视台与事件流区分「0 条」与「未跑」。"""
        with self._lock:
            self.phase = "pass_d"
            self.message = f"Pass D skipped ({reason})"
            self._emit(
                "info",
                "pass_d",
                "Pass D skipped",
                {"skipped": True, "reason": reason},
            )

    def write_begin(self, dry_run: bool) -> None:
        with self._lock:
            self.phase = "neo4j_write" if not dry_run else "dry_run"
            self.message = (
                "writing graph"
                if not dry_run
                else "dry_run: skip ingest_all; Pass D may still read Neo4j"
            )
            self._emit("info", self.phase, self.message)

    def write_done(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.result = result
            self.status = "done"
            self.phase = "done"
            self.message = "finished"
            self.finished_at = time.time()
            self._emit("info", "done", "pipeline finished", result)

    def fail(self, err: str) -> None:
        with self._lock:
            self.error = err
            self.status = "error"
            self.phase = "error"
            self.message = err
            self.finished_at = time.time()
            self._emit("error", "error", err)

    def record_llm_exchange(
        self,
        *,
        phase: str,
        step: str,
        model: str,
        messages: list[dict[str, Any]],
        response: Any,
        from_cache: bool = False,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append one LLM call for UI (prompts + structured response)."""
        with self._lock:
            safe_messages: list[dict[str, Any]] = []
            for m in messages:
                if not isinstance(m, dict):
                    continue
                raw = str(m.get("content") or "")
                tl = len(raw) > _MAX_LLM_MSG_CHARS
                content = raw[:_MAX_LLM_MSG_CHARS] + ("\n\n… [content truncated for UI]" if tl else "")
                safe_messages.append(
                    {
                        "role": str(m.get("role") or ""),
                        "content": content,
                        "content_len": len(raw),
                        "content_truncated": tl,
                    }
                )
            shrunk = _shrink_response_for_trace(phase, response)
            row: dict[str, Any] = {
                "t": time.time(),
                "phase": phase,
                "step": step,
                "model": model or "",
                "from_cache": from_cache,
                "messages": safe_messages,
                "response": shrunk,
                "meta": dict(meta or {}),
            }
            try:
                js = json.dumps(row, ensure_ascii=False)
            except (TypeError, ValueError):
                row["response"] = {"_error": "response not JSON-serializable for trace"}
                js = json.dumps(row, ensure_ascii=False)
            if len(js) > _MAX_LLM_TRACE_JSON_CHARS:
                row["response"] = {
                    "_too_large": True,
                    "hint": "响应经 shrink 后仍过大，已省略；可看事件流或本地 llm_cache JSON。",
                    "phase": phase,
                    "step": step,
                }
            self.llm_trace.append(row)
            if len(self.llm_trace) > _MAX_LLM_TURNS * 2:
                self.llm_trace = self.llm_trace[-_MAX_LLM_TURNS:]
