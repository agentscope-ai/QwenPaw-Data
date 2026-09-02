"""Trace 入图 API — 接收 JSONL trace，存储至 OSS 并写入 Trace Graph。"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from qwenpaw_data.context.uploads import save_upload_to_temp

from ..config import CFG
from ..utils import get_logger, neo4j_database_ctx, neo4j_session

log = get_logger("api.trace")

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])
callback_router = APIRouter(prefix="/api/v1/trace", tags=["trace-callback"])


# ------------------------------------------------------------------ #
# Pydantic models
# ------------------------------------------------------------------ #

class TraceIngestRequest(BaseModel):
    events: List[dict] = Field(..., description="JSONL 事件列表")
    goal: str = Field("", description="用户问题（可选，空则从 events 推断）")
    dataset: str = Field("default", description="数据集标识")
    status: str = Field("success", description="success / failed")
    failure_lesson: str = Field("", description="失败原因")
    as_of_date: str = Field("", description="业务日期 YYYYMMDD")
    auto_distill: bool = Field(True, description="是否自动触发 claim 蒸馏")


class TraceIngestResponse(BaseModel):
    task_key: str
    step_count: int
    claim_keys: List[str]
    oss_raw_key: str


class TraceListItem(BaseModel):
    task_key: str
    goal: str
    status: str
    owner_agent: str
    created_at: Optional[str] = None
    oss_raw_key: Optional[str] = None


class TraceStepDetail(BaseModel):
    plan_key: str
    step_idx: int
    intent: str
    plan_status: str
    tool_name: Optional[str] = None
    args_json: Optional[str] = None
    tc_status: Optional[str] = None
    latency_ms: Optional[int] = None
    observation: Optional[str] = None
    tool_calls: List[dict] = []
    claims: List[dict] = []


class TraceGraphResponse(BaseModel):
    task_key: str
    goal: str
    status: str
    owner_agent: str
    task_signature: str
    as_of_date: str
    created_at: Optional[str] = None
    failure_lesson: str = ""
    oss_raw_key: str = ""
    steps: List[TraceStepDetail] = []
    subtasks: List[dict] = []
    claim_count: int = 0
    summary: dict = {}


# ------------------------------------------------------------------ #
# OSS helpers (inline, no wrapper module)
# ------------------------------------------------------------------ #

def _get_oss_credentials() -> tuple[str, str, str]:
    ak = os.environ.get("OSS_AK", "")
    sk = os.environ.get("OSS_SK", "")
    endpoint = os.environ.get("OSS_ENDPOINT", "")
    if not ak or not sk:
        cfg_path = Path.home() / ".ossutilconfig"
        if cfg_path.exists():
            for line in cfg_path.read_text().splitlines():
                s = line.strip()
                if s.startswith("accessKeyID="):
                    ak = ak or s.split("=", 1)[1].strip()
                elif s.startswith("accessKeySecret="):
                    sk = sk or s.split("=", 1)[1].strip()
                elif s.startswith("endpoint="):
                    endpoint = endpoint or s.split("=", 1)[1].strip()
    endpoint = endpoint or "https://oss-cn-hangzhou.aliyuncs.com"
    return ak, sk, endpoint


def _upload_raw_trace_to_oss(content: bytes | Path, dataset: str, suffix: str) -> str:
    """上传原始 JSONL 到 OSS，返回 key。失败返回空字符串（不阻塞入图）。"""
    try:
        import oss2
    except ImportError:
        log.warning("oss2 not installed, skipping raw trace upload")
        return ""

    ak, sk, endpoint = _get_oss_credentials()
    if not ak or not sk:
        log.warning("OSS credentials not found, skipping raw trace upload")
        return ""

    bucket_name = os.environ.get("OSS_BUCKET", "datascope")
    prefix = os.environ.get("OSS_TRACE_PREFIX", "traces/")
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    oss_key = f"{prefix}{dataset}/{date_str}/{suffix}.jsonl"

    try:
        auth = oss2.Auth(ak, sk)
        bucket = oss2.Bucket(auth, endpoint, bucket_name)
        if isinstance(content, Path):
            bucket.put_object_from_file(oss_key, str(content))
        else:
            bucket.put_object(oss_key, content)
        log.info("raw trace uploaded to oss://%s/%s", bucket_name, oss_key)
        return oss_key
    except Exception as exc:
        log.warning("OSS upload failed (non-blocking): %s", exc)
        return ""


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #

@router.post("/ingest", response_model=TraceIngestResponse)
def ingest_trace_events(req: TraceIngestRequest, request: Request):
    """接收 JSONL 事件列表，存 OSS + 写入 Trace Graph。"""
    from ..graph.trace import ingest_qwenpaw_data_trace

    driver = request.app.state.driver

    raw_bytes = json.dumps(req.events, ensure_ascii=False).encode("utf-8")
    suffix = f"{uuid.uuid4().hex[:12]}"
    oss_key = _upload_raw_trace_to_oss(raw_bytes, req.dataset, suffix)

    result = ingest_qwenpaw_data_trace(
        driver,
        events=req.events,
        goal=req.goal,
        as_of_date=req.as_of_date,
        status=req.status,
        failure_lesson=req.failure_lesson,
        owner_agent="qwenpaw-data",
        auto_distill=req.auto_distill,
        oss_raw_key=oss_key,
    )

    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return TraceIngestResponse(
        task_key=result["task_key"],
        step_count=result["step_count"],
        claim_keys=result.get("claim_keys", []),
        oss_raw_key=result.get("oss_raw_key", ""),
    )


@router.post("/ingest_file", response_model=List[TraceIngestResponse])
def ingest_trace_file(
    request: Request,
    file: UploadFile = File(...),
    dataset: str = Form("default"),
    status: str = Form("success"),
    as_of_date: str = Form(""),
    auto_distill: bool = Form(True),
):
    """上传 JSONL 文件，按 qid 分组后逐条入图。"""
    from ..graph.trace import ingest_qwenpaw_data_trace
    from collections import defaultdict

    driver = request.app.state.driver
    upload_path = save_upload_to_temp(file, suffix=".jsonl")
    try:
        raw_suffix = f"{uuid.uuid4().hex[:12]}"
        oss_key = _upload_raw_trace_to_oss(upload_path, dataset, raw_suffix)

        max_line_bytes = int(os.getenv("QWENPAW_DATA_TRACE_MAX_LINE_BYTES", str(1024 * 1024)))
        max_events = int(os.getenv("QWENPAW_DATA_TRACE_MAX_EVENTS", "100000"))
        events_by_qid: dict[Any, list[dict]] = defaultdict(list)
        event_count = 0
        with upload_path.open("rb") as handle:
            while True:
                raw_line = handle.readline(max_line_bytes + 1)
                if not raw_line:
                    break
                if len(raw_line) > max_line_bytes and not raw_line.endswith(b"\n"):
                    from fastapi import HTTPException

                    raise HTTPException(413, "JSONL 单行超过 QWENPAW_DATA_TRACE_MAX_LINE_BYTES")
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                event_count += 1
                if event_count > max_events:
                    from fastapi import HTTPException

                    raise HTTPException(413, "Trace 事件数超过 QWENPAW_DATA_TRACE_MAX_EVENTS")
                qid = ev.get("qid", "__default__")
                events_by_qid[qid].append(ev)

        results: list[TraceIngestResponse] = []
        for _qid, events in events_by_qid.items():
            result = ingest_qwenpaw_data_trace(
                driver,
                events=events,
                as_of_date=as_of_date,
                status=status,
                owner_agent="qwenpaw-data",
                auto_distill=auto_distill,
                oss_raw_key=oss_key,
            )
            results.append(TraceIngestResponse(
                task_key=result["task_key"],
                step_count=result["step_count"],
                claim_keys=result.get("claim_keys", []),
                oss_raw_key=result.get("oss_raw_key", ""),
            ))

        if results:
            from .retrieval import invalidate_global_graph_snapshot_cache

            invalidate_global_graph_snapshot_cache()
        return results
    finally:
        upload_path.unlink(missing_ok=True)


@router.get("", response_model=List[TraceListItem])
def list_traces(
    request: Request,
    dataset: str = "",
    status: str = "",
    owner_agent: str = "",
    limit: int = 50,
):
    """查询 Trace Graph 中的 Task 节点列表。"""
    driver = request.app.state.driver
    effective_limit = min(limit, 200)

    # --- Task 节点（ingest_trace / ingest 写入） ---
    task_where = ["t.zone = 'trace'"]
    task_params: dict[str, Any] = {"limit": effective_limit}
    if status:
        task_where.append("t.status = $status")
        task_params["status"] = status
    if owner_agent:
        task_where.append("t.owner_agent = $owner_agent")
        task_params["owner_agent"] = owner_agent

    task_cypher = f"""
        MATCH (t:Task)
        WHERE {" AND ".join(task_where)}
        RETURN t.key AS task_key, t.goal AS goal, t.status AS status,
               t.owner_agent AS owner_agent,
               toString(t.created_at) AS created_at,
               t.oss_raw_key AS oss_raw_key
        ORDER BY t.created_at DESC
        LIMIT $limit
    """

    with neo4j_session(driver) as s:
        results = s.run(task_cypher, **task_params).data()

    return [TraceListItem(**row) for row in results]


@router.get("/{task_key:path}/graph", response_model=TraceGraphResponse)
def get_trace_graph(task_key: str, request: Request):
    """查看一条 trace：Task → Step → ToolCall（observations）→ Claim。"""
    from fastapi import HTTPException

    driver = request.app.state.driver

    with neo4j_session(driver) as s:
        # Task 节点
        task_rows = s.run(
            """
            MATCH (t:Task {key: $key})
            RETURN t.key AS task_key, coalesce(t.goal, '') AS goal,
                   coalesce(t.status, '') AS status,
                   coalesce(t.owner_agent, '') AS owner_agent,
                   coalesce(t.task_signature, '') AS task_signature,
                   coalesce(t.as_of_date, '') AS as_of_date,
                   toString(t.created_at) AS created_at,
                   coalesce(t.failure_lesson, '') AS failure_lesson,
                   coalesce(t.oss_raw_key, '') AS oss_raw_key
            """,
            key=task_key,
        ).data()

        if not task_rows:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_key}")

        task = task_rows[0]

        # Step → ToolCall
        step_rows = s.run(
            """
            MATCH (t:Task {key: $key})-[:DECOMPOSES_INTO]->(p:Step)
            OPTIONAL MATCH (p)-[:EXECUTED_BY]->(tc:ToolCall)
            RETURN p.key AS plan_key, p.step_idx AS step_idx,
                   p.intent AS intent, p.status AS plan_status,
                   tc.tool_name AS tool_name, tc.args_json AS args_json,
                   tc.status AS tc_status, tc.latency_ms AS latency_ms,
                   tc.key AS tc_key,
                   coalesce(tc.observation_summary, '') AS observation,
                   coalesce(tc.observations_json, '') AS observations_json
            ORDER BY p.step_idx
            """,
            key=task_key,
        ).data()

        # 每个 step 关联的 Claims
        claim_rows = s.run(
            """
            MATCH (t:Task {key: $key})-[:DECOMPOSES_INTO]->(p:Step)
                  -[:EXECUTED_BY]->(tc:ToolCall)-[:PRODUCES]->(cl:Claim)
            WHERE cl.valid_to IS NULL
            RETURN p.key AS plan_key,
                   cl.key AS claim_key, cl.text AS text,
                   cl.predicate AS predicate, cl.confidence AS confidence,
                   cl.subject_type AS subject_type
            """,
            key=task_key,
        ).data()

        # Recursively include subagent Tasks spawned by ToolCalls.
        subtask_rows = s.run(
            """
            MATCH (root:Task {key: $key})
            MATCH (root)-[:DECOMPOSES_INTO|EXECUTED_BY|SPAWNS*1..24]->(child:Task)
            RETURN DISTINCT child.key AS task_key,
                   child.goal AS goal,
                   child.status AS status,
                   child.owner_agent AS owner_agent,
                   child.parent_task_key AS parent_task_key,
                   child.parent_tool_call_key AS parent_tool_call_key
            """,
            key=task_key,
        ).data()
        subtask_keys = [row["task_key"] for row in subtask_rows]
        subtask_step_rows = (
            s.run(
                """
                UNWIND $keys AS task_key
                MATCH (t:Task {key: task_key})-[:DECOMPOSES_INTO]->(p:Step)
                OPTIONAL MATCH (p)-[:EXECUTED_BY]->(tc:ToolCall)
                RETURN task_key, p.key AS plan_key, p.step_idx AS step_idx,
                       p.intent AS intent, p.status AS plan_status,
                       tc.key AS tool_call_key, tc.tool_name AS tool_name,
                       tc.status AS tool_status, tc.args_json AS args_json,
                       coalesce(tc.observation_summary, '') AS observation,
                       coalesce(tc.observation_status, '') AS observation_status,
                       coalesce(tc.observations_json, '') AS observations_json
                ORDER BY task_key, p.step_idx
                """,
                keys=subtask_keys,
            ).data()
            if subtask_keys
            else []
        )

    claims_by_plan: dict[str, list[dict]] = {}
    for cr in claim_rows:
        pk = cr.pop("plan_key", "")
        claims_by_plan.setdefault(pk, []).append(cr)

    grouped_steps: dict[str, dict[str, Any]] = {}
    for row in step_rows:
        pk = row["plan_key"]
        group = grouped_steps.setdefault(
            pk,
            {
                "plan_key": pk,
                "step_idx": row.get("step_idx", 0),
                "intent": row.get("intent", ""),
                "plan_status": row.get("plan_status", ""),
                "tool_calls": [],
            },
        )
        if row.get("tc_key"):
            group["tool_calls"].append(
                {
                    "key": row.get("tc_key"),
                    "tool_name": row.get("tool_name"),
                    "args_json": row.get("args_json"),
                    "status": row.get("tc_status"),
                    "latency_ms": row.get("latency_ms"),
                    "observation": row.get("observation"),
                    "observations_json": row.get("observations_json"),
                }
            )

    steps: list[TraceStepDetail] = []
    for group in grouped_steps.values():
        first_tool = group["tool_calls"][0] if group["tool_calls"] else {}
        steps.append(
            TraceStepDetail(
                plan_key=group["plan_key"],
                step_idx=group["step_idx"],
                intent=group["intent"],
                plan_status=group["plan_status"],
                tool_name=first_tool.get("tool_name"),
                args_json=first_tool.get("args_json"),
                tc_status=first_tool.get("status"),
                latency_ms=first_tool.get("latency_ms"),
                observation=first_tool.get("observation"),
                tool_calls=group["tool_calls"],
                claims=claims_by_plan.get(group["plan_key"], []),
            )
        )

    total_claims = len(
        {
            claim.get("claim_key")
            for claims in claims_by_plan.values()
            for claim in claims
            if claim.get("claim_key")
        }
    )
    tool_names = sorted(
        {
            str(tool.get("tool_name"))
            for step in steps
            for tool in step.tool_calls
            if tool.get("tool_name") and tool.get("tool_name") != "final_answer"
        }
    )
    total_latency = sum(
        int(tool.get("latency_ms") or 0)
        for step in steps
        for tool in step.tool_calls
    )
    subtask_plan_groups: dict[str, dict[str, dict[str, Any]]] = {}
    for row in subtask_step_rows:
        child_key = row.pop("task_key")
        plan_key = row["plan_key"]
        plans = subtask_plan_groups.setdefault(child_key, {})
        plan = plans.setdefault(
            plan_key,
            {
                "plan_key": plan_key,
                "step_idx": row.get("step_idx", 0),
                "intent": row.get("intent", ""),
                "plan_status": row.get("plan_status", ""),
                "tool_calls": [],
            },
        )
        if row.get("tool_call_key"):
            plan["tool_calls"].append(
                {
                    "key": row.get("tool_call_key"),
                    "tool_name": row.get("tool_name"),
                    "status": row.get("tool_status"),
                    "args_json": row.get("args_json"),
                    "observation_key": row.get("observation_key"),
                    "observation": row.get("observation"),
                    "observation_status": row.get("observation_status"),
                    "observations_json": row.get("observations_json"),
                }
            )
    subtasks = [
        {
            **row,
            "steps": list(
                subtask_plan_groups.get(row["task_key"], {}).values()
            ),
        }
        for row in subtask_rows
    ]

    return TraceGraphResponse(
        task_key=task["task_key"],
        goal=task.get("goal") or "",
        status=task.get("status") or "",
        owner_agent=task.get("owner_agent") or "",
        task_signature=task.get("task_signature") or "",
        as_of_date=task.get("as_of_date") or "",
        created_at=task.get("created_at"),
        failure_lesson=task.get("failure_lesson") or "",
        oss_raw_key=task.get("oss_raw_key") or "",
        steps=steps,
        subtasks=subtasks,
        claim_count=total_claims,
        summary={
            "step_count": len([step for step in steps if step.step_idx >= 0]),
            "claim_count": total_claims,
            "tools_used": tool_names,
            "total_latency_ms": total_latency,
        },
    )


@router.get("/{task_key:path}/download")
def download_trace_file(task_key: str, request: Request):
    """生成原始 trace JSONL 的 OSS 预签名下载链接。"""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    driver = request.app.state.driver

    with neo4j_session(driver) as s:
        rows = s.run(
            "MATCH (t:Task {key: $key}) RETURN t.oss_raw_key AS oss_raw_key",
            key=task_key,
        ).data()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_key}")

    oss_key = (rows[0].get("oss_raw_key") or "").strip()
    if not oss_key:
        raise HTTPException(status_code=404, detail="No raw trace file stored for this task")

    try:
        import oss2
    except ImportError:
        raise HTTPException(status_code=500, detail="oss2 not installed")

    ak, sk, endpoint = _get_oss_credentials()
    if not ak or not sk:
        raise HTTPException(status_code=500, detail="OSS credentials not configured")

    bucket_name = os.environ.get("OSS_BUCKET", "datascope")
    auth = oss2.Auth(ak, sk)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    url = bucket.sign_url("GET", oss_key, 3600)

    return JSONResponse({
        "task_key": task_key,
        "oss_key": oss_key,
        "download_url": url,
        "expires_in": 3600,
    })


# ------------------------------------------------------------------ #
# submit_trace — QwenPaw Data Host snapshot
# ------------------------------------------------------------------ #

@callback_router.post("/submit_trace")
def submit_trace(payload: dict[str, Any], request: Request):
    """Parse and persist one complete QwenPaw Data Host session snapshot."""
    from fastapi.responses import JSONResponse

    from ..graph.trace import ingest_host_trace

    driver = request.app.state.driver
    try:
        agent = payload.get("agent") if isinstance(payload, dict) else None
        if not isinstance(agent, dict) or not isinstance(
            agent.get("memory"), dict
        ):
            raise ValueError(
                "complete Host trace requires agent.memory.content"
            )
        ingest_host_trace(
            driver, payload,
            neo4j_database=neo4j_database_ctx.get(),
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "status": "failed",
                "error": str(exc),
            },
        )
    except Exception as exc:
        log.exception("submit_trace failed")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "status": "failed",
                "error": str(exc),
            },
        )

    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return {"success": True, "status": "success"}
