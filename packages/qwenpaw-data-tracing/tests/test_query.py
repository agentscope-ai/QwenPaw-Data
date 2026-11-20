# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from qwenpaw_data.tracing.core.names import Step
from qwenpaw_data.tracing.query import (
    LabelFilter,
    MetadataField,
    MetadataFilter,
    SpanField,
    tracer_get_chat,
    tracer_get_task,
    tracer_list_chats,
    tracer_list_sessions,
    tracer_list_tasks,
)
from qwenpaw_data.tracing.query.backend._parse import read_observation
from qwenpaw_data.tracing.query.select import window as query_window
from qwenpaw_data.tracing.runtime.host.chat import tracer_chat_ref

CHAT_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHAT_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CHAT_C = "cccccccccccccccccccccccccccccccc"
TASK_1 = "11111111111111111111111111111111"
TASK_2 = "22222222222222222222222222222222"
SETTLE = "dddddddddddddddddddddddddddddddd"

T0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 2, 0, 2, tzinfo=UTC)
T3 = datetime(2026, 1, 2, 0, 3, tzinfo=UTC)
FROM = datetime(2026, 1, 1, tzinfo=UTC)
TO = datetime(2026, 1, 3, tzinfo=UTC)


GENERATION = {
    "type": "GENERATION",
    "level": "ERROR",
    "statusMessage": "Connection error.",
    "latency": 1.5,
    "timeToFirstToken": 0.4,
    "model": "qwen3.7-flash",
    "usageDetails": {"input": 10, "output": 2, "total": 12},
    "costDetails": {"total": 0.01},
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _obs(
    *,
    obs_id: str,
    trace_id: str,
    name: str,
    start: datetime,
    end: datetime | None = None,
    parent_id: str | None = None,
    trace_name: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    session_id: str = "ses-1",
    user_id: str = "u1",
    input: object | None = None,
    output: object | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(extra or {}),
        "id": obs_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": name,
        "startTime": _iso(start),
        "endTime": _iso(end or start),
        "userId": user_id,
        "sessionId": session_id,
        "metadata": metadata or {},
        "traceName": trace_name,
        "tags": tags or [],
        "input": input,
        "output": output,
    }


def _score(
    *,
    score_id: str,
    name: str,
    value: object,
    data_type: str = "CATEGORICAL",
) -> dict[str, Any]:
    return {
        "id": score_id,
        "name": name,
        "value": value,
        "dataType": data_type,
        "comment": None,
        "metadata": None,
    }


class FakeBackend:
    def __init__(self) -> None:
        self.observations: dict[str, list[dict[str, Any]]] = {}
        self.scores: dict[str, list[dict[str, Any]]] = {}
        self.session_scores: dict[str, list[dict[str, Any]]] = {}
        self.status = 200
        self.holo_page_size = 100
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def add_trace(self, observations: list[dict[str, Any]]) -> None:
        self.observations[observations[0]["traceId"]] = observations

    def add_scores(self, trace_id: str, scores: list[dict[str, Any]]) -> None:
        self.scores[trace_id] = scores

    def root(self, trace_id: str) -> dict[str, Any]:
        return next(
            row
            for row in self.observations[trace_id]
            if row["parentObservationId"] is None
        )

    def roots(self) -> list[dict[str, Any]]:
        return [self.root(trace_id) for trace_id in self.observations]

    def paths(self, path: str) -> list[dict[str, Any]]:
        return [params for seen, params in self.requests if seen == path]


def _enable(monkeypatch: pytest.MonkeyPatch, backend: str = "langfuse") -> None:
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("TRACING_BACKEND", backend)
    monkeypatch.setenv("TRACING_HOST", "https://lf.example")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")


def _install_http(monkeypatch: pytest.MonkeyPatch, fake: FakeBackend) -> None:
    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._base = str(kwargs.get("base_url") or "")

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(
            self, path: str, params: dict[str, Any] | None = None
        ) -> httpx.Response:
            fake.requests.append((path, dict(params or {})))
            request = httpx.Request("GET", f"{self._base}{path}")
            if fake.status >= 400:
                return httpx.Response(fake.status, request=request)
            body = _route(fake, path, params or {})
            if body is None:
                return httpx.Response(404, request=request)
            return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _route(
    fake: FakeBackend, path: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    if path == "/api/public/v2/observations":
        return _langfuse_observations(fake, params)
    if path == "/api/public/v3/scores":
        return _langfuse_scores(fake, params)
    if path == "/api/public/traces":
        return _holoscope_list(fake, params)
    if path == "/api/public/observations":
        return _holoscope_observations(fake, params)
    if path.startswith("/api/public/traces/"):
        return _holoscope_get(fake, path.rsplit("/", 1)[-1])
    if path == "/api/public/v2/scores":
        return _holoscope_scores(fake, params)
    return None


def _langfuse_observations(fake: FakeBackend, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("traceId"):
        return {"data": list(fake.observations.get(params["traceId"], [])), "meta": {}}
    rows = fake.roots()
    for item in json.loads(params["filter"]):
        column, op, value = item["column"], item["operator"], item["value"]
        if column in {"sessionId", "userId", "traceName", "release"}:
            rows = [row for row in rows if row.get(column) == value]
        elif column == "tags":
            rows = [row for row in rows if set(value) <= set(row["tags"])]
        elif column == "metadata":
            rows = [row for row in rows if row["metadata"].get(item["key"]) == value]
        elif column in {"startTime", "endTime"} and op == ">=":
            rows = [
                row
                for row in rows
                if row.get(column) and _parse(row[column]) >= _parse(value)
            ]
        elif column in {"startTime", "endTime"} and op == "<":
            rows = [
                row
                for row in rows
                if row.get(column) and _parse(row[column]) < _parse(value)
            ]
        elif column == "traceId" and op == "any of":
            rows = [row for row in rows if row["traceId"] in value]
    return {"data": rows, "meta": {}}


def _langfuse_scores(fake: FakeBackend, params: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if "sessionId" in params:
        for session_id in str(params["sessionId"]).split(","):
            for score in fake.session_scores.get(session_id, []):
                rows.append({**score, "subject": {"kind": "session", "id": session_id}})
        return {"data": rows, "meta": {}}
    for trace_id in str(params["traceId"]).split(","):
        for score in fake.scores.get(trace_id, []):
            rows.append({**score, "subject": {"kind": "trace", "id": trace_id}})
    return {"data": rows, "meta": {}}


def _trace(fake: FakeBackend, trace_id: str) -> dict[str, Any]:
    root = fake.root(trace_id)
    return {
        "id": trace_id,
        "name": root["traceName"],
        "timestamp": root["startTime"],
        "sessionId": root["sessionId"],
        "userId": root["userId"],
        "metadata": root["metadata"],
        "tags": root["tags"],
        "scores": [score["id"] for score in fake.scores.get(trace_id, [])],
    }


def _holoscope_page(
    fake: FakeBackend, params: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    page = int(params["page"])
    limit = min(int(params["limit"]), fake.holo_page_size)
    total_pages = max(1, (len(rows) + limit - 1) // limit)
    return {
        "data": rows[(page - 1) * limit : page * limit],
        "meta": {"page": page, "totalPages": total_pages},
    }


def _holoscope_list(fake: FakeBackend, params: dict[str, Any]) -> dict[str, Any]:
    start, end = _parse(params["fromTimestamp"]), _parse(params["toTimestamp"])
    ids = None
    for item in json.loads(params.get("filter") or "[]"):
        if item["column"] == "id" and item["operator"] == "any of":
            ids = set(item["value"])
    traces = [
        _trace(fake, trace_id)
        for trace_id in fake.observations
        if params.get("sessionId") in (None, fake.root(trace_id)["sessionId"])
        and params.get("userId") in (None, fake.root(trace_id)["userId"])
        and fake.root(trace_id)["traceName"] == params["name"]
        and start <= _parse(fake.root(trace_id)["startTime"]) < end
        and (ids is None or trace_id in ids)
    ]
    traces.sort(key=lambda item: item["timestamp"])
    return _holoscope_page(fake, params, traces)


def _holoscope_observations(
    fake: FakeBackend, params: dict[str, Any]
) -> dict[str, Any]:
    """v3 observation list: no session or user id on the rows."""
    rows = [row for rows in fake.observations.values() for row in rows]
    if params.get("userId"):
        rows = [row for row in rows if row["userId"] == params["userId"]]
    for item in json.loads(params["filter"]):
        column, op, value = item["column"], item["operator"], item["value"]
        if column == "traceName":
            rows = [row for row in rows if row["traceName"] == value]
        elif column == "traceTags":
            rows = [row for row in rows if set(value) <= set(row["tags"])]
        elif column == "traceId" and op == "any of":
            rows = [row for row in rows if row["traceId"] in value]
        elif column == "endTime" and op == ">=":
            rows = [row for row in rows if _parse(row["endTime"]) >= _parse(value)]
        elif column == "endTime" and op == "<":
            rows = [row for row in rows if _parse(row["endTime"]) < _parse(value)]
    hidden = {"sessionId", "userId", "traceName", "tags"}
    return _holoscope_page(
        fake,
        params,
        [{k: v for k, v in row.items() if k not in hidden} for row in rows],
    )


def _holoscope_get(fake: FakeBackend, trace_id: str) -> dict[str, Any] | None:
    if trace_id not in fake.observations:
        return None
    observations = [
        {key: value for key, value in row.items() if key not in {"traceName", "tags"}}
        for row in fake.observations[trace_id]
    ]
    return {
        **_trace(fake, trace_id),
        "observations": observations,
        "scores": [
            {**score, "traceId": trace_id} for score in fake.scores.get(trace_id, [])
        ],
    }


def _holoscope_scores(fake: FakeBackend, params: dict[str, Any]) -> dict[str, Any]:
    if "sessionId" in params:
        session_id = params["sessionId"]
        rows = [
            {**score, "sessionId": session_id, "traceId": None}
            for score in fake.session_scores.get(session_id, [])
        ]
        return {"data": rows, "meta": {"page": 1, "totalPages": 1}}
    wanted = set(str(params["scoreIds"]).split(","))
    rows = [
        {**score, "traceId": trace_id}
        for trace_id, scores in fake.scores.items()
        for score in scores
        if score["id"] in wanted
    ]
    return {"data": rows, "meta": {"page": 1, "totalPages": 1}}


def _chat_root(trace_id: str, start: datetime, **kwargs: Any) -> dict[str, Any]:
    return _obs(
        obs_id=f"{trace_id}-root",
        trace_id=trace_id,
        name="invoke_agent qwenpaw-data",
        start=start,
        trace_name="chat",
        tags=["chat"],
        **kwargs,
    )


def _task(
    trace_id: str,
    start: datetime,
    desc: str,
    refs: list[str],
    first_chat_at: datetime | None = None,
) -> list[dict]:
    metadata: dict[str, Any] = {"desc": desc}
    if first_chat_at is not None:
        metadata["first_chat_at"] = first_chat_at.isoformat()
    root = _obs(
        obs_id=f"{trace_id}-root",
        trace_id=trace_id,
        name="task",
        start=start,
        trace_name="task",
        tags=["task"],
        metadata=metadata,
    )
    return [root] + [
        _obs(
            obs_id=f"{trace_id}-ref-{index}",
            trace_id=trace_id,
            name=Step.CHAT_REF,
            start=start + timedelta(milliseconds=index),
            parent_id=root["id"],
            trace_name="task",
            metadata={"source_trace_id": ref},
        )
        for index, ref in enumerate(refs)
    ]


def _seed_world(fake: FakeBackend) -> None:
    fake.add_trace(
        [
            _chat_root(
                CHAT_A,
                T0,
                metadata={"datasource_id": "ds-1"},
                input={"q": "a"},
                output={"ok": True},
            ),
            _obs(
                obs_id="chat-a-tool",
                trace_id=CHAT_A,
                name="execute_tool Skill",
                start=T0,
                parent_id=f"{CHAT_A}-root",
                trace_name="chat",
                metadata={"tool_state": "success"},
                extra=GENERATION,
            ),
        ]
    )
    fake.add_scores(
        CHAT_A,
        [
            _score(score_id="status-a", name="chat_status", value="completed"),
            _score(score_id="copy-a", name="copy", value=True, data_type="BOOLEAN"),
        ],
    )
    fake.add_trace([_chat_root(CHAT_B, T1)])
    fake.add_scores(
        CHAT_B,
        [
            _score(score_id="status-b", name="chat_status", value="failed"),
            _score(
                score_id="conf-b", name="confidence", value=1.0, data_type="NUMERIC"
            ),
        ],
    )
    fake.add_trace([_chat_root(CHAT_C, T2)])
    fake.add_scores(
        CHAT_C,
        [
            _score(score_id="status-c", name="chat_status", value="completed"),
            _score(score_id="reaction-c", name="reaction", value="like"),
        ],
    )
    fake.add_trace(
        [
            _obs(
                obs_id="settle-root",
                trace_id=SETTLE,
                name="settlement",
                start=T1,
                trace_name="settlement",
                tags=["settlement"],
                metadata={"source_trace_id": CHAT_A},
            )
        ]
    )
    fake.add_trace(
        _task(TASK_1, T2, "按支付时间统计 8 月收入总额", [CHAT_B, CHAT_A], T0)
    )
    fake.add_scores(
        TASK_1, [_score(score_id="task-label", name="task_status", value="ready")]
    )
    fake.add_trace(_task(TASK_2, T3, "第二个任务", [CHAT_C], T2))


@pytest.fixture
def langfuse(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    _enable(monkeypatch, "langfuse")
    fake = FakeBackend()
    _seed_world(fake)
    _install_http(monkeypatch, fake)
    return fake


@pytest.fixture
def holoscope(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    _enable(monkeypatch, "holoscope")
    fake = FakeBackend()
    _seed_world(fake)
    _install_http(monkeypatch, fake)
    return fake


@pytest.mark.asyncio
async def test_rejects_illegal_arguments(langfuse: FakeBackend) -> None:
    with pytest.raises(ValueError):
        await tracer_list_chats("")
    with pytest.raises(ValueError):
        await tracer_list_chats("   ")
    with pytest.raises(ValueError):
        await tracer_list_chats(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tracer_get_chat("")
    with pytest.raises(ValueError):
        await tracer_get_chat("ABC" * 10 + "ab")
    with pytest.raises(ValueError):
        await tracer_get_chat("chat-" + CHAT_A)
    with pytest.raises(ValueError):
        await tracer_list_chats(
            "ses-1", label_filters=[LabelFilter(name="", values=["completed"])]
        )
    with pytest.raises(ValueError):
        await tracer_list_chats(
            "ses-1", label_filters=[LabelFilter(name="chat_status", values=[])]
        )
    with pytest.raises(ValueError):
        await tracer_list_chats("ses-1", label_match="or")
    with pytest.raises(ValueError):
        await tracer_list_chats("ses-1", from_end_time=datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.asyncio
async def test_unconfigured_and_unknown_backend_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    monkeypatch.delenv("TRACING_HOST", raising=False)
    with pytest.raises(RuntimeError):
        await tracer_list_chats("ses-1")
    _enable(monkeypatch, "other")
    with pytest.raises(ValueError):
        await tracer_list_chats("ses-1")


@pytest.mark.asyncio
async def test_list_chats_drops_other_traces_and_has_no_spans(
    langfuse: FakeBackend,
) -> None:
    page = await tracer_list_chats("ses-1", from_end_time=FROM, to_end_time=TO)
    assert [chat.trace_id for chat in page] == [CHAT_A, CHAT_B, CHAT_C]
    assert all(chat.spans == [] for chat in page)
    assert (page[0].metadata, page[0].ended_at) == ({"datasource_id": "ds-1"}, T0)
    roots = langfuse.paths("/api/public/v2/observations")[0]
    assert roots["expandMetadata"] == "desc"
    columns = [item["column"] for item in json.loads(roots["filter"])]
    assert "endTime" in columns
    assert "startTime" not in columns
    names = [
        item["value"]
        for item in json.loads(roots["filter"])
        if item["column"] == "traceName"
    ]
    assert names == ["chat"]


@pytest.mark.asyncio
async def test_chat_trace_ids_restrict_the_page(langfuse: FakeBackend) -> None:
    page = await tracer_list_chats(
        "ses-1", chat_trace_ids=[CHAT_C], from_end_time=FROM, to_end_time=TO
    )
    assert [chat.trace_id for chat in page] == [CHAT_C]
    pushed = json.loads(langfuse.paths("/api/public/v2/observations")[0]["filter"])
    assert {
        "type": "stringOptions",
        "column": "traceId",
        "operator": "any of",
        "value": [CHAT_C],
    } in pushed


@pytest.mark.asyncio
async def test_list_chats_includes_chat_that_started_before_window(
    langfuse: FakeBackend,
) -> None:
    early = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    langfuse.add_trace([_chat_root(early, T0 - timedelta(hours=2), end=T1)])
    page = await tracer_list_chats("ses-1", from_end_time=T1, to_end_time=T2)
    assert [chat.trace_id for chat in page] == [CHAT_B, early]


@pytest.mark.asyncio
async def test_label_filters_all_and_any(langfuse: FakeBackend) -> None:
    completed = LabelFilter(name="chat_status", values=["completed"])
    liked = LabelFilter(name="reaction", values=["like"])
    all_page = await tracer_list_chats(
        "ses-1",
        from_end_time=FROM,
        to_end_time=TO,
        label_filters=[completed, liked],
        label_match="all",
    )
    assert [chat.trace_id for chat in all_page] == [CHAT_C]
    any_page = await tracer_list_chats(
        "ses-1",
        from_end_time=FROM,
        to_end_time=TO,
        label_filters=[completed, liked],
        label_match="any",
    )
    assert [chat.trace_id for chat in any_page] == [CHAT_A, CHAT_C]


@pytest.mark.asyncio
async def test_filtered_chat_keeps_all_its_scores(langfuse: FakeBackend) -> None:
    page = await tracer_list_chats(
        "ses-1",
        from_end_time=FROM,
        to_end_time=TO,
        label_filters=[LabelFilter(name="reaction", values=["like"])],
    )
    assert [chat.trace_id for chat in page] == [CHAT_C]
    assert {score.name for score in page[0].scores} == {
        "chat_status",
        "reaction",
    }
    assert all(
        "name" not in params for params in langfuse.paths("/api/public/v3/scores")
    )


@pytest.mark.asyncio
async def test_numeric_label_matches_integer_text(langfuse: FakeBackend) -> None:
    page = await tracer_list_chats(
        "ses-1",
        from_end_time=FROM,
        to_end_time=TO,
        label_filters=[LabelFilter(name="confidence", values=["1"])],
    )
    assert [chat.trace_id for chat in page] == [CHAT_B]


@pytest.mark.asyncio
async def test_window_over_cap_asks_to_narrow(
    langfuse: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(query_window, "MAX_ROOTS", 2)
    with pytest.raises(ValueError, match="narrow"):
        await tracer_list_chats("ses-1", from_end_time=FROM, to_end_time=TO)


@pytest.mark.asyncio
async def test_list_tasks_reads_chat_refs_without_member_chats(
    langfuse: FakeBackend,
) -> None:
    page = await tracer_list_tasks("ses-1", from_start_time=FROM, to_start_time=TO)
    assert [task.trace_id for task in page] == [TASK_1, TASK_2]
    task = page[0]
    assert task.metadata == {"desc": "按支付时间统计 8 月收入总额"}
    assert task.chat_trace_ids == [CHAT_B, CHAT_A]
    assert (task.first_chat_at, page[1].first_chat_at) == (T0, T2)
    assert task.chats == []
    assert task.missing_chats == []
    assert [score.name for score in task.scores] == ["task_status"]
    fetched = [
        params["traceId"]
        for params in langfuse.paths("/api/public/v2/observations")
        if params.get("traceId")
    ]
    assert sorted(fetched) == [TASK_1, TASK_2]
    listed_kinds = [
        item["value"]
        for params in langfuse.paths("/api/public/v2/observations")
        if params.get("filter")
        for item in json.loads(params["filter"])
        if item["column"] == "traceName"
    ]
    assert listed_kinds == ["task"]


@pytest.mark.asyncio
async def test_list_chats_order_limit_and_spans(langfuse: FakeBackend) -> None:
    window = {"from_end_time": FROM, "to_end_time": TO}
    latest = await tracer_list_chats("ses-1", order="desc", limit=2, **window)
    assert [chat.trace_id for chat in latest] == [CHAT_C, CHAT_B]
    assert all(chat.spans == [] for chat in latest)
    assert not [
        p for p in langfuse.paths("/api/public/v2/observations") if "traceId" in p
    ]
    oldest = await tracer_list_chats(
        "ses-1", limit=1, span_fields=SpanField.IO, **window
    )
    (chat,) = oldest
    assert chat.trace_id == CHAT_A
    assert [span.name for span in chat.spans] == [
        "invoke_agent qwenpaw-data",
        "execute_tool Skill",
    ]
    assert chat.spans[0].input == {"q": "a"}
    assert {score.name for score in chat.scores} == {"chat_status", "copy"}
    read = [
        p["traceId"]
        for p in langfuse.paths("/api/public/v2/observations")
        if "traceId" in p
    ]
    assert read == [CHAT_A]
    for bad in ({"order": "newest"}, {"limit": 0}, {"limit": True}):
        with pytest.raises(ValueError):
            await tracer_list_chats("ses-1", **bad)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_tasks_order_by_first_chat(langfuse: FakeBackend) -> None:
    task_3 = "33333333333333333333333333333333"
    langfuse.add_trace(_task(task_3, T1, "最早创建，成员最晚", [CHAT_C], T3))
    window = {"from_start_time": FROM, "to_start_time": TO}
    tasks = await tracer_list_tasks("ses-1", **window)
    assert [task.trace_id for task in tasks] == [TASK_1, TASK_2, task_3]
    langfuse.requests.clear()
    latest = await tracer_list_tasks(
        "ses-1", order="desc", limit=1, span_fields=SpanField.BASIC, **window
    )
    (task,) = latest
    assert task.trace_id == task_3
    assert [chat.trace_id for chat in task.chats] == [CHAT_C]
    assert task.chats[0].spans[0].input is None
    read = [
        p["traceId"]
        for p in langfuse.paths("/api/public/v2/observations")
        if "traceId" in p
    ]
    assert sorted(read) == sorted([task_3, CHAT_C])
    detail = await tracer_get_task(TASK_1)
    assert detail.first_chat_at == T0
    assert detail.metadata == {"desc": "按支付时间统计 8 月收入总额"}


@pytest.mark.asyncio
async def test_first_chat_before_window_still_orders(langfuse: FakeBackend) -> None:
    early = "55555555555555555555555555555555"
    langfuse.add_trace(
        _task(early, T3, "成员在窗口前", [CHAT_C], FROM - timedelta(days=30))
    )
    tasks = await tracer_list_tasks("ses-1", from_start_time=FROM, to_start_time=TO)
    assert [task.trace_id for task in tasks] == [early, TASK_1, TASK_2]


@pytest.mark.asyncio
async def test_task_without_first_chat_at_sorts_by_own_start(
    langfuse: FakeBackend,
) -> None:
    missing = "44444444444444444444444444444444"
    naive = "66666666666666666666666666666666"
    langfuse.add_trace(_task(missing, T1, "没写 first_chat_at", [CHAT_C]))
    bad = _task(naive, T1 + timedelta(seconds=1), "没带时区", [CHAT_C])
    bad[0]["metadata"]["first_chat_at"] = "2026-01-01T00:00:00"
    langfuse.add_trace(bad)
    tasks = await tracer_list_tasks("ses-1", from_start_time=FROM, to_start_time=TO)
    assert [(task.trace_id, task.first_chat_at) for task in tasks] == [
        (TASK_1, T0),
        (missing, None),
        (naive, None),
        (TASK_2, T2),
    ]


def _seed_other_session(fake: FakeBackend) -> str:
    chat_d = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    root = _chat_root(
        chat_d,
        T1,
        session_id="ses-2",
        user_id="u2",
        metadata={"workspace_id": "w2"},
    )
    root["endTime"] = _iso(T3)
    root["release"] = "v2"
    fake.add_trace([root])
    return chat_d


@pytest.mark.asyncio
async def test_list_sessions_groups_chat_roots(langfuse: FakeBackend) -> None:
    _seed_other_session(langfuse)
    window = {"from_end_time": FROM, "to_end_time": TO}
    sessions = await tracer_list_sessions(**window)
    assert [s.session_id for s in sessions] == ["ses-2", "ses-1"]
    other, main = sessions
    assert (other.user_id, other.chat_count, other.updated_at) == ("u2", 1, T3)
    assert (main.chat_count, main.first_chat_at, main.last_chat_at) == (3, T0, T2)
    assert main.updated_at == T2
    oldest = await tracer_list_sessions(order="asc", limit=1, **window)
    assert [s.session_id for s in oldest] == ["ses-1"]
    w2 = MetadataFilter(key="workspace_id", values=["w2"])
    for where in (
        {"user_id": "u2"},
        {"metadata_filters": [w2]},
        {"release": "v2"},
        {"tags": ["chat"], "user_id": "u2"},
    ):
        found = await tracer_list_sessions(**where, **window)  # type: ignore[arg-type]
        assert [s.session_id for s in found] == ["ses-2"]
    assert await tracer_list_sessions(tags=["task"], **window) == []
    pushed = json.loads(langfuse.paths("/api/public/v2/observations")[-1]["filter"])
    assert {
        "type": "arrayOptions",
        "column": "tags",
        "operator": "all of",
        "value": ["task"],
    } in pushed


@pytest.mark.asyncio
async def test_list_sessions_metadata_filters(langfuse: FakeBackend) -> None:
    _seed_other_session(langfuse)
    window = {"from_end_time": FROM, "to_end_time": TO}
    either = MetadataFilter(key="workspace_id", values=["w9", "w2"])
    found = await tracer_list_sessions(metadata_filters=[either], **window)
    assert [s.session_id for s in found] == ["ses-2"]
    pushed = json.loads(langfuse.paths("/api/public/v2/observations")[-1]["filter"])
    assert all(item["column"] != "metadata" for item in pushed)
    ds = MetadataFilter(key="datasource_id", values=["ds-1"])
    (main,) = await tracer_list_sessions(metadata_filters=[ds], **window)
    assert (main.session_id, main.chat_count) == ("ses-1", 1)
    assert await tracer_list_sessions(metadata_filters=[either, ds], **window) == []
    for bad in (
        {"user_id": " "},
        {"tags": [""]},
        {"metadata_filters": [MetadataFilter(key="workspace_id", values=[])]},
        {"metadata_filters": [MetadataFilter(key="", values=["w2"])]},
        {"metadata_filters": [MetadataFilter(key="workspace_id", values=[" "])]},
    ):
        with pytest.raises(ValueError):
            await tracer_list_sessions(**bad)  # type: ignore[arg-type]


REVISITED = LabelFilter(name="session_revisit", values=["session_revisit"])


def _revisit(score_id: str) -> dict[str, Any]:
    return _score(score_id=score_id, name="session_revisit", value="session_revisit")


@pytest.mark.asyncio
async def test_list_sessions_session_scores(langfuse: FakeBackend) -> None:
    _seed_other_session(langfuse)
    langfuse.session_scores["ses-1"] = [_revisit("rev-1")]
    langfuse.session_scores["ses-2"] = [
        _score(score_id="share-2", name="share", value="workspace")
    ]
    window = {"from_end_time": FROM, "to_end_time": TO}

    plain = await tracer_list_sessions(**window)
    assert all(s.scores is None for s in plain)
    assert not langfuse.paths("/api/public/v3/scores")

    scored = await tracer_list_sessions(with_scores=True, **window)
    assert {s.session_id: [x.id for x in s.scores or []] for s in scored} == {
        "ses-1": ["rev-1"],
        "ses-2": ["share-2"],
    }
    (asked,) = langfuse.paths("/api/public/v3/scores")
    assert set(asked["sessionId"].split(",")) == {"ses-1", "ses-2"}

    (hit,) = await tracer_list_sessions(label_filters=[REVISITED], **window)
    assert (hit.session_id, hit.scores) == ("ses-1", None)

    shared = LabelFilter(name="share", values=["workspace"])
    both = await tracer_list_sessions(
        label_filters=[REVISITED, shared], label_match="any", **window
    )
    assert [s.session_id for s in both] == ["ses-2", "ses-1"]
    assert (
        await tracer_list_sessions(
            label_filters=[REVISITED, shared], label_match="all", **window
        )
        == []
    )
    (latest_hit,) = await tracer_list_sessions(
        label_filters=[REVISITED], limit=1, **window
    )
    assert latest_hit.session_id == "ses-1"
    with pytest.raises(ValueError):
        await tracer_list_sessions(label_match="or")


@pytest.mark.asyncio
async def test_get_chat_and_task_details(langfuse: FakeBackend) -> None:
    chat = await tracer_get_chat(CHAT_A, session_id="ses-1")
    assert [span.name for span in chat.spans] == [
        "invoke_agent qwenpaw-data",
        "execute_tool Skill",
    ]
    assert chat.spans[0].input == {"q": "a"}
    assert chat.spans[0].output == {"ok": True}
    assert {score.name: score.value for score in chat.scores}["copy"] == "true"
    task = await tracer_get_task(TASK_1, session_id="ses-1")
    assert [item.trace_id for item in task.chats] == [CHAT_B, CHAT_A]
    assert task.chat_trace_ids == [CHAT_B, CHAT_A]
    assert task.chats[1].spans[0].input == {"q": "a"}
    with pytest.raises(LookupError):
        await tracer_get_chat(CHAT_A, session_id="other")
    with pytest.raises(LookupError):
        await tracer_get_chat(TASK_1)
    with pytest.raises(LookupError):
        await tracer_get_task(CHAT_A)
    with pytest.raises(LookupError):
        await tracer_get_chat("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")


PLATFORM_V4 = {
    "scope.name": "qwenpaw_data.host",
    "resourceAttributes.service.name": "qwenpaw-data-host",
    "attributes.langfuse.session.id": "ses-1",
    "attributes.gen_ai.response.finish_reasons": ["stop"],
}
PLATFORM_V3 = {
    "scope": {"name": "qwenpaw_data.host"},
    "resourceAttributes": {"service.name": "qwenpaw-data-host"},
    "attributes": {"user.id": "u1"},
}


@pytest.mark.parametrize("platform", [PLATFORM_V4, PLATFORM_V3])
@pytest.mark.asyncio
async def test_metadata_fields_business_by_default_full_on_request(
    langfuse: FakeBackend, platform: dict[str, Any]
) -> None:
    for row in langfuse.observations[CHAT_A]:
        row["metadata"] = {**row["metadata"], **platform}
    langfuse.root(TASK_1)["metadata"].update(platform)
    chat = await tracer_get_chat(CHAT_A)
    assert chat.metadata == {"datasource_id": "ds-1"}
    assert chat.spans[1].metadata == {"tool_state": "success"}
    listed = await tracer_list_chats("ses-1", from_end_time=FROM, to_end_time=TO)
    assert listed[0].metadata == {"datasource_id": "ds-1"}
    task = await tracer_get_task(TASK_1)
    assert task.metadata == {"desc": "按支付时间统计 8 月收入总额"}
    full = await tracer_get_chat(CHAT_A, metadata_fields=MetadataField.FULL)
    assert full.metadata == {"datasource_id": "ds-1", **platform}
    assert full.spans[1].metadata == {"tool_state": "success", **platform}


@pytest.mark.asyncio
async def test_span_fields_trim_spans_and_backend_request(
    langfuse: FakeBackend,
) -> None:
    langfuse.root(CHAT_A)["release"] = "fd3d746d"
    basic = await tracer_get_chat(CHAT_A, span_fields=SpanField.BASIC)
    assert basic.release == "fd3d746d"
    root, tool = basic.spans
    assert (root.input, root.output) == (None, None)
    assert (root.type, root.level, root.status_message) == (None, None, None)
    assert (tool.type, tool.level, tool.status_message) == (
        "GENERATION",
        "ERROR",
        "Connection error.",
    )
    assert (tool.latency, tool.time_to_first_token) == (1.5, 0.4)
    assert (tool.model, tool.usage, tool.cost) == (None, None, None)
    only_input = await tracer_get_chat(CHAT_A, span_fields=SpanField.INPUT)
    assert only_input.spans[0].output is None
    output = await tracer_get_chat(CHAT_A, span_fields=SpanField.OUTPUT)
    assert (output.spans[0].input, output.spans[0].output) == (None, {"ok": True})
    full = await tracer_get_chat(CHAT_A, span_fields=SpanField.FULL)
    tool = full.spans[1]
    assert tool.model == "qwen3.7-flash"
    assert tool.usage == {"input": 10, "output": 2, "total": 12}
    assert tool.cost == {"total": 0.01}
    requested = [
        params["fields"]
        for params in langfuse.paths("/api/public/v2/observations")
        if params.get("traceId")
    ]
    assert requested == [
        "core,basic,metadata,trace_context,metrics",
        "core,basic,metadata,trace_context,metrics,io",
        "core,basic,metadata,trace_context,metrics,io",
        "core,basic,metadata,trace_context,metrics,io,model,usage",
    ]


def test_empty_usage_reads_as_none() -> None:
    row = read_observation({"usageDetails": {}, "costDetails": {}, "model": ""})
    assert (row["usage"], row["cost"], row["model"]) == (None, None, None)


@pytest.mark.asyncio
async def test_unknown_fields_fail_before_any_data(langfuse: FakeBackend) -> None:
    with pytest.raises(ValueError):
        await tracer_list_chats("nobody", metadata_fields="bussiness")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tracer_list_tasks("nobody", metadata_fields="bussiness")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await tracer_get_chat(CHAT_A, span_fields="everything")  # type: ignore[arg-type]
    page = await tracer_list_chats("nobody", metadata_fields="full")  # type: ignore[arg-type]
    assert page == []


@pytest.mark.asyncio
async def test_get_task_reports_missing_chats(langfuse: FakeBackend) -> None:
    gone = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    other = "ffffffffffffffffffffffffffffffff"
    langfuse.add_trace([_chat_root(other, T0, session_id="ses-2")])
    langfuse.add_trace(_task(TASK_2, T3, "缺成员", [gone, CHAT_B, other, CHAT_A]))
    task = await tracer_get_task(TASK_2)
    assert [item.trace_id for item in task.chats] == [CHAT_B, CHAT_A]
    assert [(item.trace_id, item.reason) for item in task.missing_chats] == [
        (gone, "not_found"),
        (other, "session_mismatch"),
    ]
    assert task.chat_trace_ids == [gone, CHAT_B, other, CHAT_A]
    complete = await tracer_get_task(TASK_1)
    assert complete.missing_chats == []


@pytest.mark.asyncio
async def test_http_error_is_not_empty_page(langfuse: FakeBackend) -> None:
    langfuse.status = 500
    with pytest.raises(httpx.HTTPStatusError):
        await tracer_list_chats("ses-1", from_end_time=FROM, to_end_time=TO)


@pytest.mark.asyncio
async def test_holoscope_v3_reads(holoscope: FakeBackend) -> None:
    holoscope.holo_page_size = 1
    listed_chats = await tracer_list_chats("ses-1", from_end_time=FROM, to_end_time=TO)
    assert [chat.trace_id for chat in listed_chats] == [CHAT_A, CHAT_B, CHAT_C]
    assert {score.name for score in listed_chats[0].scores} == {"chat_status", "copy"}
    ended = json.loads(holoscope.paths("/api/public/observations")[0]["filter"])
    assert {item["column"] for item in ended} == {"traceName", "endTime"}
    listed = holoscope.paths("/api/public/traces")[0]
    assert listed["name"] == "chat"
    assert listed["sessionId"] == "ses-1"
    assert _parse(listed["fromTimestamp"]) == T0
    (by_id,) = json.loads(listed["filter"])
    assert (by_id["column"], sorted(by_id["value"])) == (
        "id",
        [CHAT_A, CHAT_B, CHAT_C],
    )
    chat = await tracer_get_chat(CHAT_A)
    assert chat.spans
    assert chat.metadata == {"datasource_id": "ds-1"}
    assert {score.name for score in chat.scores} == {"chat_status", "copy"}
    page = await tracer_list_tasks("ses-1", from_start_time=FROM, to_start_time=TO)
    assert page[0].chat_trace_ids == [CHAT_B, CHAT_A]
    assert page[0].metadata == {"desc": "按支付时间统计 8 月收入总额"}
    task = await tracer_get_task(TASK_1)
    assert [item.trace_id for item in task.chats] == [CHAT_B, CHAT_A]
    tool = task.chats[1].spans[1]
    assert tool.latency == 1.5
    assert tool.usage is None
    full = await tracer_get_chat(CHAT_A, span_fields=SpanField.FULL)
    assert full.spans[1].usage == {"input": 10, "output": 2, "total": 12}
    _seed_other_session(holoscope)
    found = await tracer_list_sessions(
        user_id="u2",
        metadata_filters=[MetadataFilter(key="workspace_id", values=["w2"])],
        from_end_time=FROM,
        to_end_time=TO,
    )
    assert [s.session_id for s in found] == ["ses-2"]
    assert holoscope.paths("/api/public/observations")[-1]["userId"] == "u2"
    sessions_listed = holoscope.paths("/api/public/traces")[-1]
    assert sessions_listed["userId"] == "u2"
    assert "sessionId" not in sessions_listed
    assert (
        await tracer_list_sessions(
            metadata_filters=[MetadataFilter(key="workspace_id", values=["nope"])],
            from_end_time=FROM,
            to_end_time=TO,
        )
        == []
    )
    holoscope.session_scores["ses-2"] = [_revisit("rev-2")]
    (revisited,) = await tracer_list_sessions(
        label_filters=[REVISITED],
        with_scores=True,
        from_end_time=FROM,
        to_end_time=TO,
    )
    assert revisited.session_id == "ses-2"
    assert [score.id for score in revisited.scores or []] == ["rev-2"]


@pytest.mark.asyncio
async def test_holoscope_end_window_uses_root_end(holoscope: FakeBackend) -> None:
    long_run = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    root = _chat_root(long_run, T0 - timedelta(days=3), end=T3)
    step = _obs(
        obs_id="long-step",
        trace_id=long_run,
        name="execute_tool Skill",
        start=T1,
        end=T1,
        parent_id=root["id"],
        trace_name="chat",
    )
    holoscope.add_trace([root, step])
    page = await tracer_list_chats("ses-1", from_end_time=T1, to_end_time=T2)
    assert [chat.trace_id for chat in page] == [CHAT_B]
    page = await tracer_list_chats("ses-1", from_end_time=T3, to_end_time=TO)
    assert [(chat.trace_id, chat.ended_at) for chat in page] == [(long_run, T3)]
    picked = await tracer_list_chats(
        "ses-1", chat_trace_ids=[CHAT_C], from_end_time=FROM, to_end_time=TO
    )
    assert [chat.trace_id for chat in picked] == [CHAT_C]
    pushed = json.loads(holoscope.paths("/api/public/observations")[-1]["filter"])
    assert {
        "type": "stringOptions",
        "column": "traceId",
        "operator": "any of",
        "value": [CHAT_C],
    } in pushed


@pytest.mark.asyncio
async def test_holoscope_categorical_reads_string_value(
    holoscope: FakeBackend,
) -> None:
    holoscope.scores[CHAT_C][1] = {
        **_score(score_id="reaction-c", name="reaction", value=None),
        "stringValue": "like",
    }
    page = await tracer_list_chats(
        "ses-1",
        from_end_time=FROM,
        to_end_time=TO,
        label_filters=[LabelFilter(name="reaction", values=["like"])],
    )
    assert [chat.trace_id for chat in page] == [CHAT_C]


@pytest.mark.asyncio
async def test_langfuse_parses_json_io_strings(langfuse: FakeBackend) -> None:
    langfuse.root(CHAT_A)["input"] = '{"q":"a"}'
    langfuse.root(CHAT_A)["output"] = "not json"
    chat = await tracer_get_chat(CHAT_A)
    assert (chat.spans[0].input, chat.spans[0].output) == ({"q": "a"}, "not json")


@pytest.mark.asyncio
async def test_holoscope_keeps_plain_text_io(holoscope: FakeBackend) -> None:
    holoscope.root(CHAT_A)["input"] = "null"
    holoscope.root(CHAT_A)["output"] = "123"
    chat = await tracer_get_chat(CHAT_A)
    assert (chat.spans[0].input, chat.spans[0].output) == ("null", "123")


def test_chat_ref_writes_member_span(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    tracer_chat_ref(CHAT_A)
    (span,) = exporter.get_finished_spans()
    assert span.name == Step.CHAT_REF
    assert span.attributes["langfuse.observation.metadata.source_trace_id"] == CHAT_A
    with pytest.raises(ValueError):
        tracer_chat_ref("chat-" + CHAT_A)
