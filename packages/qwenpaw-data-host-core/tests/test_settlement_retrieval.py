"""Settlement CM 迁移回归；模型输出与 MCP 传输替身，保留真实 ReAct。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from agentscope.formatter import DashScopeChatFormatter
from agentscope.mcp import MCPClient
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, DashScopeChatModel
from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.algo.settlement.cm_utils.client import SettlementCmClient
from qwenpaw_data.host.core.algo.settlement.confirmer import (
    DuplicateJudgement,
    SettlementConfirmer,
)
from qwenpaw_data.host.core.algo.settlement.llm import (
    StructuredCallError,
    for_structured_calls,
)
from qwenpaw_data.host.core.algo.settlement.manager import SettlementManager
from qwenpaw_data.host.core.algo.settlement.models import DetectedItem
from qwenpaw_data.host.core.algo.settlement.retriever import SettlementRetriever
from qwenpaw_data.host.core.algo.settlement.settings import SettlementSettings
from qwenpaw_data.host.core.domain.model_usage import ModelUsageSource
from qwenpaw_data.host.core.settings.cm import CmSettings
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)


def _item(kind="metric_caliber"):
    fields = {
        "metric_caliber": {
            "metric_name": "GMV",
            "caliber": "支付金额求和",
            "table": "orders",
            "formula_sql": "SELECT SUM(amount) FROM orders",
        },
        "dimension_def": {
            "dimension_name": "渠道",
            "bind_column": "channel",
            "value_samples": "web,app",
            "table": "orders",
        },
        "column_meaning": {
            "table": "orders",
            "column_name": "status",
            "meaning": "3=已发货",
        },
        "dataset_usage": {
            "use_case": "订单分析",
            "recommended_dataset": "orders",
        },
    }[kind]
    return DetectedItem(type=kind, fields={**fields, "domain": "trade"})


def _confirmer(payload=None, *, graph_rows=None, graph_error=False, **kwargs):
    cm = SettlementCmClient(datasource_id="ds1")
    cm.call = AsyncMock(
        return_value={
            "status": "ok",
            "result": json.dumps(payload, ensure_ascii=False),
        }
    )

    async def retrieve(item, **inputs):
        if graph_error:
            raise RuntimeError("检索失败")
        return json.dumps(
            {
                "results": graph_rows if graph_rows is not None else [],
                "summary": "检索摘要",
            },
            ensure_ascii=False,
        )

    unit = SettlementConfirmer(
        object(),
        session_id="s1",
        chat_id="c1",
        cm=cm,
        datasource_id="ds1",
        domain_names=["trade"],
        **kwargs,
    )
    unit._retriever.retrieve = AsyncMock(side_effect=retrieve)
    unit._llm_judge = AsyncMock(return_value=DuplicateJudgement(duplicate=False))
    return unit, cm, unit._retriever


@pytest.mark.parametrize(
    ("kind", "tool", "payload"),
    [
        (
            "metric_caliber",
            "search_metrics",
            [
                {
                    "key": "metric-1",
                    "metric_name": "GMV",
                    "description": "金额",
                    "aliases": ["成交额"],
                    "priority": "normal",
                    "role": "",
                }
            ],
        ),
        (
            "dimension_def",
            "get_dimension",
            {
                "dimension_name": "渠道",
                "calculate_expr": "channel",
                "dataset_name": "orders",
                "is_display_dimension": True,
                "sample_values": [
                    {
                        "value": "app",
                        "business_meaning": "应用渠道",
                        "frequency": 0.35,
                        "is_rollup_sentinel": False,
                    }
                ],
            },
        ),
        (
            "column_meaning",
            "get_dataset",
            {
                "dataset_name": "orders",
                "columns": [
                    {"column_name": "status", "description": "3=已发货"},
                ],
                "datasource_type": "postgresql",
            },
        ),
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
async def test_api_evidence_keeps_original_fields_and_skips_react(
    monkeypatch, kind, tool, payload, duplicate
):
    unit, cm, retriever = _confirmer(payload)
    monkeypatch.delattr(unit, "_llm_judge")
    judge = AsyncMock(return_value=DuplicateJudgement(duplicate=duplicate))
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.confirmer.structured_call", judge
    )
    items = [_item(kind)]
    confirmed, details = await unit.confirm_with_details(items)
    assert confirmed == ([] if duplicate else items)
    assert details[0]["retrieval_status"] == "ok"
    assert cm.call.call_args.args[0] == tool
    assert cm.call.call_args.args[1]["domain"] == "trade"
    assert json.loads(cm.call.call_args.args[1]["metadata"]) == {"datasource_id": "ds1"}
    assert cm.call.call_args.kwargs == {"max_len": 0}
    retriever.retrieve.assert_not_awaited()
    judge.assert_awaited_once()
    messages = judge.call_args.args[1]
    prompt = "\n".join(block.text for block in messages[0].get_content_blocks("text"))
    message = "\n".join(block.text for block in messages[1].get_content_blocks("text"))
    context = message.split("## 已有知识\n", 1)[1]
    assert context.startswith(
        "知识来源：共享知识\n检索业务域：trade\n检索数据源：ds1\n\n"
    )
    assert json.loads(context.split("\n\n", 1)[1]) == payload
    assert all(
        term not in prompt
        for term in ("REST", "HTTP", "MCP", "Schema", "calculate_expr", "MetricCaliber")
    )
    assert all(
        term not in context
        for term in ('"source"', '"scope"', '"data"', "shared_metadata", "http_status")
    )
    assert details[0]["cm_calls"] == [{"status": "ok"}]


async def test_large_api_result_is_parsed_before_display_clipping():
    payload = {
        "dataset_name": "orders",
        "columns": [
            {"column_name": f"c{i}", "description": "其他列" * 20} for i in range(100)
        ]
        + [{"column_name": "status", "description": "3=已发货"}],
    }
    unit, _, retriever = _confirmer(payload)
    calls = []
    context = await unit._query_cm(_item("column_meaning"), calls)
    assert len(json.dumps(payload)) > 2000
    assert "result" not in calls[0]
    assert context["data"]["columns"] == payload["columns"][-1:]
    retriever.retrieve.assert_not_awaited()


async def test_formatted_knowledge_keeps_full_definition_and_returned_scope():
    description = "3=已发货；" * 500
    payload = {
        "dataset_name": "订单数据集",
        "physical_tables": ["orders"],
        "domain": "archive_trade",
        "datasource_id": "ds_archive",
        "columns": [
            {"column_name": "other_status", "description": "无关列"},
            {
                "column_name": "status",
                "description": description,
                "sample_values": [0, False, None, ""],
            },
        ],
    }
    unit, _, _ = _confirmer(payload)
    await unit.confirm_with_details([_item("column_meaning")])
    context = unit._llm_judge.call_args.args[1]
    assert "检索业务域：trade\n检索数据源：ds1" in context
    knowledge = json.loads(context.split("\n\n", 1)[1])
    assert knowledge == {**payload, "columns": payload["columns"][1:]}
    assert len(context) > 2000
    assert payload["columns"][0]["column_name"] == "other_status"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "no_match"}, "ok"),
        ({"status": "low_confidence"}, "error"),
        ({"ambiguous": True}, "error"),
        ([], "ok"),
        ({}, "error"),
        ([{"metric_name": "GMV", "description": ""}], "error"),
    ],
)
async def test_insufficient_api_evidence_uses_retriever(payload, expected):
    unit, _, retriever = _confirmer(payload, graph_rows=[{"description": "真实定义"}])
    item = _item()
    calls = []
    if expected == "error":
        with pytest.raises(RuntimeError, match="共享 Metadata"):
            await unit._query_cm(item, calls)
    else:
        context = await unit._query_cm(item, calls)
        assert context["source"] == "mcp"
        assert context["scope"]["graph"] == "personal_graph"
        assert json.loads(context["data"]) == {
            "results": [{"description": "真实定义"}],
            "summary": "检索摘要",
        }
    inputs = retriever.retrieve.call_args.kwargs
    assert inputs["domain"] == "trade" and inputs["datasource_id"] == "ds1"
    assert "query" not in inputs
    assert retriever.retrieve.call_args.args == (item,)
    assert "prior_result" not in inputs
    assert "calls" not in inputs


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("metric_caliber", {"status": "low_confidence"}),
        ("dimension_def", {"ambiguous": True}),
        ("metric_caliber", {}),
    ],
)
@pytest.mark.parametrize("rows", [[], [{"description": "个人定义"}]])
async def test_personal_results_cannot_resolve_shared_uncertainty(
    mcp_transport, kind, payload, rows
):
    mcp_transport.output.update(rows=rows, row_count=len(rows))
    unit, _, _ = _confirmer(payload)
    unit._retriever = _retriever(
        ScriptedModel(
            [
                _tool("get_graph_schema"),
                _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
                _finish(),
            ]
        )
    )
    confirmed, details = await unit.confirm_with_details([_item(kind)])
    assert confirmed == []
    assert details[0]["retrieval_status"] == "error"
    unit._llm_judge.assert_not_awaited()


@pytest.mark.parametrize("description", [None, "", "   "])
@pytest.mark.parametrize("physical_name", [False, True])
async def test_resolved_column_without_description_can_pass_after_fallback(
    mcp_transport, description, physical_name
):
    payload = {
        "dataset_name": "订单数据集" if physical_name else "ORDERS",
        "physical_tables": ["orders"] if physical_name else [],
        "columns": [{"column_name": "STATUS", "description": description}],
    }
    mcp_transport.output.update(rows=[], row_count=0)
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(),
        ]
    )
    unit, _, _ = _confirmer(payload)
    unit._retriever = _retriever(model)
    item = _item("column_meaning")
    confirmed, details = await unit.confirm_with_details([item])
    assert confirmed == [item]
    assert details[0]["retrieval_status"] == "ok"
    assert len(mcp_transport.calls) == 2
    assert "STATUS" not in str(model.seen)
    unit._llm_judge.assert_awaited_once()


@pytest.mark.parametrize("mismatch", ["table", "column", "ambiguous"])
async def test_unresolved_column_cannot_pass_on_empty_personal_results(mismatch):
    payload = {
        "dataset_name": "orders",
        "columns": [{"column_name": "status", "description": "其他定义"}],
    }
    if mismatch == "table":
        payload["dataset_name"] = "orders_archive"
    elif mismatch == "column":
        payload["columns"][0]["column_name"] = "other_status"
    else:
        payload["ambiguous"] = True
    unit, _, retriever = _confirmer(payload)
    confirmed, details = await unit.confirm_with_details([_item("column_meaning")])
    assert confirmed == []
    assert details[0]["retrieval_status"] == "error"
    retriever.retrieve.assert_awaited_once()
    unit._llm_judge.assert_not_awaited()


@pytest.mark.parametrize("rows", [[], [{"description": "订单分析用法"}]])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_dataset_results_always_go_to_confirmer(rows, duplicate):
    unit, cm, retriever = _confirmer(graph_rows=rows)
    unit._llm_judge.return_value = DuplicateJudgement(duplicate=duplicate)
    item = _item("dataset_usage")
    confirmed, details = await unit.confirm_with_details([item])
    cm.call.assert_not_awaited()
    retriever.retrieve.assert_awaited_once()
    assert confirmed == ([] if duplicate else [item])
    unit._llm_judge.assert_awaited_once()
    assert json.loads(unit._llm_judge.call_args.args[1].split("\n\n", 1)[1]) == {
        "results": rows,
        "summary": "检索摘要",
    }
    assert details[0]["retrieval_status"] == "ok"


async def test_unknown_domain_stops_without_calls():
    unit, cm, retriever = _confirmer()
    item = _item()
    item.fields["domain"] = "unknown"
    with pytest.raises(ValueError, match="有效业务域"):
        await unit._query_cm(item, [])
    cm.call.assert_not_awaited()
    retriever.retrieve.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "message", "fallback"),
    [
        (401, "Unauthorized", False),
        (403, "Forbidden", False),
        (503, "unavailable", False),
        (404, "Domain not found", False),
        (404, "Dimension not found", True),
    ],
)
async def test_http_failures_do_not_become_no_match(code, message, fallback):
    unit, cm, retriever = _confirmer()
    cm.call.return_value = {"status": "error", "result": message, "http_status": code}
    calls = []
    if fallback:
        context = await unit._query_cm(_item("dimension_def"), calls)
        assert json.loads(context["data"]) == {
            "results": [],
            "summary": "检索摘要",
        }
    else:
        with pytest.raises(RuntimeError, match="Metadata API"):
            await unit._query_cm(_item("dimension_def"), calls)
    assert calls == [{"status": "error", "http_status": code}]
    assert retriever.retrieve.await_count == int(fallback)


async def test_empty_api_then_failed_graph_is_not_recommended():
    unit, _, _ = _confirmer({"status": "no_match"}, graph_error=True)
    confirmed, details = await unit.confirm_with_details([_item()])
    assert confirmed == []
    assert details[0]["retrieval_status"] == "error"
    unit._llm_judge.assert_not_awaited()


@pytest.mark.parametrize("rows", [[], [{"description": "相关定义"}]])
async def test_successful_retrieval_still_requires_successful_judgement(
    monkeypatch, rows
):
    unit, _, retriever = _confirmer({"status": "no_match"}, graph_rows=rows)
    monkeypatch.delattr(unit, "_llm_judge")
    judge = AsyncMock(side_effect=StructuredCallError("模型调用失败"))
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.confirmer.structured_call", judge
    )
    confirmed, details = await unit.confirm_with_details([_item()])
    assert confirmed == []
    retriever.retrieve.assert_awaited_once()
    judge.assert_awaited_once()
    assert details[0]["retrieval_status"] == "ok"
    assert details[0]["reason"] == "LLM 调用失败，不确定不推"
    message = judge.call_args.args[1][1]
    content = "\n".join(block.text for block in message.get_content_blocks("text"))
    context = content.split("## 已有知识\n", 1)[1]
    assert json.loads(context.split("\n\n", 1)[1]) == {
        "results": rows,
        "summary": "检索摘要",
    }


async def test_api_and_fallback_share_one_deadline():
    unit, cm, retriever = _confirmer({"status": "no_match"}, cm_timeout=0.06)
    record = cm.call.return_value

    async def api(*args, **kwargs):
        await asyncio.sleep(0.04)
        return record

    async def graph(*args, **kwargs):
        await asyncio.sleep(0.04)
        return "检索完成，没有相关记录。"

    cm.call.side_effect = api
    retriever.retrieve.side_effect = graph
    calls = []
    with pytest.raises(TimeoutError):
        await unit._query_cm(_item(), calls)
    assert calls == [{"status": "ok"}]
    retriever.retrieve.assert_awaited_once()


def test_retrieval_prompt_structure_and_examples():
    unit = _retriever(object())
    prompt = unit._system_prompt
    assert prompt.startswith("# 语义层检索器\n")
    assert "知识图" not in prompt
    assert "预算耗尽" not in prompt
    assert "incomplete" not in prompt
    for section in ("输入说明", "检索步骤", "输出说明", "示例"):
        assert f"## {section}\n" in prompt
    for removed in (
        "## 检索原则",
        "## 匹配判断",
        "利用已有结果",
        "**status**",
        "no_match",
    ):
        assert removed not in prompt
    assert prompt.index("## 输入说明") < prompt.index("## 示例")
    assert "`Skill` 加载 `cm-skills`" in prompt
    assert "`Read`" in prompt
    assert "backend" in prompt
    assert "编写 `query_graph`" not in prompt
    assert "search_graph_fulltext" in prompt
    assert "search_graph_vector" in prompt
    assert "`Domain.domain_id`" in prompt
    assert "按 Skill 的类型规则显式传入可用的范围参数" in prompt
    assert "`Table`、`Column` 仅支持全文" in prompt
    assert "`Metric`、`Dimension` 按 `domain_id` 搜索" in prompt
    cypher = prompt.split("```cypher\n", 1)[1].split("\n```", 1)[0]
    assert "[:HAS]" in cypher
    assert "m.key IN $keys" in cypher
    for node in ("m", "c", "d"):
        assert f"{node}.domain = $domain_id" in cypher
    for node in ("c", "d"):
        assert f"{node}.datasource_id = $datasource_id" in cypher
    assert "ORDER BY m.key, c.key, d.key\nSKIP $offset" in cypher
    assert "LIMIT" not in cypher
    assert "工具 `limit=200`" in prompt
    assert "`offset` 增加本页实际 `row_count`" in prompt
    assert "直到 `truncated=false`" in prompt
    assert "HAS_CALIBER" not in prompt
    blocks = [
        json.loads(block.split("\n```", 1)[0])
        for block in prompt.split("```json\n")[1:]
    ]
    example_inputs = [
        block.split("\n```", 1)[0] for block in prompt.split("```text\n")[1:]
    ]
    assert example_inputs == [
        unit._build_user_message(_item(), datasource_id="ds1", domain="trade"),
    ]
    example = prompt.split("## 示例\n", 1)[1]
    assert "选择或组合使用 `search_graph_fulltext` 和 `search_graph_vector`" in example
    domain_search, domains, search, hits, params, queried, final = blocks
    assert domain_search == {"text": "trade", "node_types": ["Domain"], "limit": 20}
    domain = domains["rows"][0]["node"]
    assert domain["name"] == domain_search["text"]
    assert len({domain["name"], domain["domain_id"], domain["key"]}) == 3
    assert search == {
        "text": "GMV",
        "node_types": ["Metric"],
        "domain_id": domain["domain_id"],
        "limit": 20,
    }
    assert params["domain_id"] == search["domain_id"]
    assert params["offset"] == 0
    assert params["keys"] == [row["node"]["key"] for row in hits["rows"]]
    assert all(row["node"]["domain"] == params["domain_id"] for row in hits["rows"])
    for payload, node_type, method in (
        (domains, "Domain", "fulltext"),
        (hits, "Metric", "vector"),
    ):
        assert payload["searched_node_types"] == [node_type]
        assert payload["columns"] == ["node_type", "node", "retrieval"]
        assert all(row["node_type"] == node_type for row in payload["rows"])
        assert all(row["retrieval"]["method"] == method for row in payload["rows"])
        assert payload["rows"][0]["retrieval"]["lexical_matches"] == [
            {"field": "name", "relation": "exact"}
        ]
    assert hits["rows"][1]["retrieval"]["lexical_matches"] == []
    for payload in (domains, hits, queried):
        assert payload["row_count"] == len(payload["rows"])
        assert all(list(row) == payload["columns"] for row in payload["rows"])
        assert payload["truncated"] is False
    assert all(f"AS {column}" in cypher for column in queried["columns"])
    assert set(final) == {"results", "summary"}
    assert final["results"] == queried["rows"]
    assert all(
        row["datasource_id"] == params["datasource_id"]
        and row["domain"] == params["domain_id"]
        for row in final["results"]
    )
    assert len(final["results"]) == 2
    assert "两条相关口径" in final["summary"]
    assert "不要求严格匹配字段或格式" in prompt
    assert "Markdown 或自然语言" in prompt
    assert "**results**" in prompt and "**summary**" in prompt


class ScriptedModel(ChatModelBase):
    def __init__(self, blocks):
        self.model = "settlement-test"
        self.stream = False
        self.max_retries = 0
        self.context_size = 32768
        self.formatter = DashScopeChatFormatter()
        self.blocks = iter(blocks)
        self.seen = []

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        self.seen.append((deepcopy(messages), deepcopy(tools)))
        return ChatResponse(
            content=[next(self.blocks)],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
        )


def _tool(name, params=None):
    if name in {
        "get_graph_schema",
        "query_graph",
        "search_graph_fulltext",
        "search_graph_vector",
        "execute_read",
    }:
        name = f"mcp__{CmSettings.MCP_NAME}__{name}"
    return ToolCallBlock(id=uuid4().hex, name=name, input=json.dumps(params or {}))


def _finish(text="检索结束"):
    return TextBlock(text=text)


@pytest.fixture
def mcp_transport(monkeypatch):
    sessions, clients, calls = [], [], []
    output = {
        "rows": [{"key": "metric-1", "expr": "sum(amount)"}],
        "columns": ["key", "expr"],
        "row_count": 1,
        "truncated": False,
        "elapsed_ms": 1.0,
    }
    descriptors = [
        Tool(
            name="get_graph_schema",
            description="真实 Schema 描述",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="query_graph",
            description="真实图查询描述",
            inputSchema={
                "type": "object",
                "properties": {
                    "cypher": {"type": "string"},
                    "params": {"type": "object"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 100,
                    },
                },
                "required": ["cypher"],
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
        *[
            Tool(
                name=name,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 8000},
                        "node_types": {"type": "array", "items": {"type": "string"}},
                        "domain_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                        },
                        "datasource_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 20,
                        },
                    },
                    "required": ["text"],
                },
                annotations=ToolAnnotations(readOnlyHint=True),
            )
            for name in ("search_graph_fulltext", "search_graph_vector")
        ],
        Tool(name="execute_read", inputSchema={"type": "object"}),
    ]

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            sessions.append("open")
            return self

        async def __aexit__(self, *args):
            sessions.append("close")

        async def initialize(self):
            pass

        async def list_tools(self):
            return ListToolsResult(tools=descriptors)

        async def call_tool(self, name, *, arguments, **kwargs):
            calls.append((name, arguments))
            data = (
                output.get("schema", {"backend": "holoage", "nodes": [], "edges": []})
                if name == "get_graph_schema"
                else output["responses"].pop(0)
                if output.get("responses")
                else output
            )
            if output.get("block") and name == "query_graph":
                await asyncio.Event().wait()
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text=output.get("raw_text", json.dumps(data))
                    )
                ],
                isError=data.get("isError", False),
            )

    @asynccontextmanager
    async def transport(client):
        clients.append(client)
        yield None, None

    monkeypatch.setattr(MCPClient, "_get_client_gen", transport)
    monkeypatch.setattr("agentscope.mcp._mcp_client.ClientSession", Session)
    monkeypatch.setattr("agentscope.tool._adapters.ClientSession", Session)
    return SimpleNamespace(
        sessions=sessions, clients=clients, calls=calls, output=output
    )


def _retriever(model, **kwargs):
    return SettlementRetriever(
        model,
        access_token="test-only-token",
        session_id="s1",
        chat_id="c1",
        **kwargs,
    )


async def _retrieve(unit, kind="metric_caliber"):
    return await unit.retrieve(
        _item(kind),
        datasource_id="ds1",
        domain="trade",
    )


async def test_real_react_uses_native_tools_and_returns_model_text(
    monkeypatch, mcp_transport
):
    close = AsyncMock()
    monkeypatch.setattr(MCPClient, "close", close)
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool(
                "query_graph",
                {"cypher": "RETURN $term AS name", "params": {"term": "GMV"}},
            ),
            _finish(),
        ]
    )
    unit = _retriever(model, max_iters=3)
    result = await _retrieve(unit)
    assert "sum(amount)" in str(model.seen[-1][0])
    assert mcp_transport.calls == [
        ("get_graph_schema", {}),
        ("query_graph", {"cypher": "RETURN $term AS name", "params": {"term": "GMV"}}),
    ]
    schemas = model.seen[0][1]
    assert {schema["function"]["name"] for schema in schemas} == {
        "Skill",
        "Read",
        f"mcp__{CmSettings.MCP_NAME}__get_graph_schema",
        f"mcp__{CmSettings.MCP_NAME}__query_graph",
        f"mcp__{CmSettings.MCP_NAME}__search_graph_fulltext",
        f"mcp__{CmSettings.MCP_NAME}__search_graph_vector",
    }
    close.assert_not_awaited()
    assert "query_id" not in str(model.seen)
    assert result == "检索结束"
    assert any(
        schema["function"]["description"] == "真实图查询描述" for schema in schemas
    )
    assert "test-only-token" not in str(model.seen)
    client = mcp_transport.clients[0]
    assert client.is_stateful is False
    assert client.enable_tools == [
        "get_graph_schema",
        "query_graph",
        "search_graph_fulltext",
        "search_graph_vector",
    ]
    assert client._runtime_headers == {
        "Authorization": "Bearer test-only-token",
        "datasource_id": "ds1",
        "session_id": "s1",
        "chat_id": "c1",
    }
    assert mcp_transport.sessions.count("open") == mcp_transport.sessions.count("close")
    assert unit._model is model


@pytest.mark.parametrize("backend", ["neo4j", "holoage"])
@pytest.mark.parametrize("search", ["search_graph_fulltext", "search_graph_vector"])
@pytest.mark.parametrize("pages", [1, 2])
async def test_react_loads_cm_skill_references_and_searches(
    mcp_transport, backend, search, pages
):
    skill_dir = next(
        skill.src_dir
        for skill in discover_builtin_skills()
        if skill.name == "cm-skills"
    )
    reference = skill_dir / "references"
    domain_id = "domain-42"
    domain_search = {"text": "trade", "node_types": ["Domain"], "limit": 20}
    metric_search = {
        "text": "GMV",
        "node_types": ["Metric"],
        "domain_id": domain_id,
        "limit": 20,
    }
    search_responses = [
        {
            "columns": ["node_type", "node", "retrieval"],
            "rows": [
                {
                    "node_type": node_type,
                    "node": node,
                    "retrieval": {
                        "method": method,
                        "lexical_matches": [{"field": "name", "relation": "exact"}],
                    },
                }
            ],
            "row_count": 1,
            "truncated": False,
            "elapsed_ms": 1.0,
            "searched_node_types": [node_type],
        }
        for node_type, node, method in (
            (
                "Domain",
                {
                    "key": "domain-node",
                    "name": "trade",
                    "domain_id": domain_id,
                    "description": "交易业务",
                },
                "fulltext",
            ),
            (
                "Metric",
                {
                    "key": "metric-1",
                    "name": "GMV",
                    "domain": domain_id,
                },
                search.removeprefix("search_graph_"),
            ),
        )
    ]
    rows = [
        {"key": "metric-1", "expr": expr, "domain": domain_id, "datasource_id": "ds1"}
        for expr in ("sum(amount)", "sum(amount - refund_amount)")[:pages]
    ]
    query_responses = [
        {
            "columns": list(row),
            "rows": [
                {key: json.dumps(value) for key, value in row.items()}
                if backend == "holoage"
                else row
            ],
            "row_count": 1,
            "truncated": index < pages - 1,
            "elapsed_ms": 1.0,
        }
        for index, row in enumerate(rows)
    ]
    mcp_transport.output["schema"] = {"backend": backend, "graphs": ["test_graph"]}
    mcp_transport.output["responses"] = search_responses + query_responses
    cypher = (
        "MATCH (m:Metric) WHERE m.key IN $keys AND m.domain = $domain_id "
        "OPTIONAL MATCH (m)-[r:HAS]->(c:MetricCaliber) "
        "WHERE c.domain = $domain_id AND c.datasource_id = $datasource_id "
        "RETURN m.key AS key, c.expr AS expr, c.domain AS domain, "
        "c.datasource_id AS datasource_id ORDER BY m.key, c.key SKIP $offset"
    )
    queries = []
    offset = 0
    for response in query_responses:
        params = {
            "keys": ["metric-1"],
            "domain_id": domain_id,
            "datasource_id": "ds1",
            "offset": offset,
        }
        query = {"cypher": cypher, "params": params, "limit": 200}
        if backend == "holoage":
            query = {
                "cypher": (
                    "SELECT result.key, result.expr, result.domain, result.datasource_id "
                    "FROM ag_catalog.cypher('test_graph', $$ "
                    + cypher
                    + " $$, %(values)s::ag_catalog.agtype) "
                    "AS result(key ag_catalog.agtype, expr ag_catalog.agtype, "
                    "domain ag_catalog.agtype, datasource_id ag_catalog.agtype)"
                ),
                "params": {"values": json.dumps(params)},
                "limit": 200,
            }
        queries.append(query)
        offset += response["row_count"]
    final_text = "GMV 口径：" + "；".join(row["expr"] for row in rows)
    model = ScriptedModel(
        [
            _tool("Skill", {"skill": "cm-skills"}),
            _tool("Read", {"file_path": str(reference / "mcp-tools.md")}),
            _tool("search_graph_fulltext", domain_search),
            _tool(search, metric_search),
            _tool("get_graph_schema"),
            _tool("Read", {"file_path": str(reference / f"{backend}.md")}),
            *[_tool("query_graph", query) for query in queries],
            _finish(final_text),
        ]
    )
    assert await _retrieve(_retriever(model, max_iters=10)) == final_text
    assert mcp_transport.calls == [
        ("search_graph_fulltext", domain_search),
        (search, metric_search),
        ("get_graph_schema", {}),
        *[("query_graph", query) for query in queries],
    ]
    assert mcp_transport.output["responses"] == []
    for index, (_, query) in enumerate(mcp_transport.calls[3:]):
        params = (
            json.loads(query["params"]["values"])
            if backend == "holoage"
            else query["params"]
        )
        assert params == {
            "keys": ["metric-1"],
            "domain_id": domain_id,
            "datasource_id": "ds1",
            "offset": index,
        }
        assert query["limit"] == 200
        assert "LIMIT" not in query["cypher"]
    initial = str(model.seen[0][0])
    assert "业务域名称：trade" in initial
    assert domain_id not in initial
    assert "<name>cm-skills</name>" in initial
    assert "<name>query-odps</name>" not in initial
    assert str(skill_dir) in initial
    assert "## 选择工具" not in initial
    assert "## 选择工具" in str(model.seen[1][0])
    assert "## 搜索节点" in str(model.seen[2][0])
    for index, response in enumerate(search_responses, start=3):
        tool_results = [
            block
            for msg in model.seen[index][0]
            for block in msg.get_content_blocks("tool_result")
        ]
        assert tool_results[-1].output[0].text == json.dumps(response)
    final = str(model.seen[-1][0])
    read_results = [
        block
        for msg in model.seen[-1][0]
        for block in msg.get_content_blocks("tool_result")
        if block.name == "Read"
    ]
    assert len(read_results) == 2
    assert all(block.state == ToolResultState.SUCCESS for block in read_results)
    assert "## 从搜索结果继续查询" in final
    assert ":HAS]" in final
    assert all(row["expr"] in final for row in rows)
    assert ("# Neo4j 查询规则" in final) == (backend == "neo4j")
    assert ("# Holo AGE 查询规则" in final) == (backend == "holoage")


@pytest.mark.parametrize(
    ("search", "node_type", "scope"),
    [
        ("search_graph_fulltext", "Table", {"datasource_id": "ds1"}),
        ("search_graph_fulltext", "Column", {"datasource_id": "ds1"}),
        (
            "search_graph_vector",
            "Dataset",
            {"domain_id": "domain-42", "datasource_id": "ds1"},
        ),
    ],
)
async def test_search_passes_supported_scope_parameters(
    mcp_transport, search, node_type, scope
):
    params = {"text": "orders", "node_types": [node_type], **scope, "limit": 20}
    response = {
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "elapsed_ms": 1.0,
        "searched_node_types": [node_type],
    }
    mcp_transport.output["responses"] = [response]
    model = ScriptedModel([_tool(search, params), _finish("指定范围内未返回记录。")])
    assert await _retrieve(_retriever(model)) == "指定范围内未返回记录。"
    assert mcp_transport.calls == [(search, params)]
    tool_results = [
        block
        for msg in model.seen[-1][0]
        for block in msg.get_content_blocks("tool_result")
    ]
    assert tool_results[-1].output[0].text == json.dumps(response)


@pytest.mark.parametrize("path_kind", ["outside", "traversal", "symlink"])
async def test_retriever_read_is_limited_to_skill_directory(
    monkeypatch, mcp_transport, tmp_path, path_kind
):
    skill_dir = tmp_path / "cm-skills"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: cm-skills\ndescription: CM 测试指南\n---\n# CM 测试指南\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-private-content", encoding="utf-8")
    link = skill_dir / "linked.txt"
    link.symlink_to(outside)
    file_path = {
        "outside": outside,
        "traversal": skill_dir / ".." / outside.name,
        "symlink": link,
    }[path_kind]
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.retriever.discover_builtin_skills",
        lambda: [SimpleNamespace(name="cm-skills", src_dir=skill_dir)],
    )
    model = ScriptedModel([_tool("Read", {"file_path": str(file_path)}), _finish()])
    assert await _retrieve(_retriever(model)) == "检索结束"
    assert "只允许读取 cm-skills 目录内的文件" in str(model.seen[-1][0])
    assert "outside-private-content" not in str(model.seen)
    assert mcp_transport.calls == []


@pytest.mark.parametrize("output_format", ["json", "markdown", "text"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_native_fallback_passes_model_text_to_confirmer(
    mcp_transport, empty, duplicate, output_format
):
    if empty:
        mcp_transport.output.update(rows=[], row_count=0)
    else:
        row = {
            "key": "metric-1",
            "expr": "sum(amount)",
            "custom_alias": {"description": "实际定义", "描述": "另一条说明"},
            "values": [0, False, None, ""],
        }
        mcp_transport.output.update(rows=[row], columns=list(row))
    summary = "trade 业务域 ds1 数据源检索结束：" + (
        "没有返回记录。" if empty else "取得指标定义。"
    )
    rows_text = json.dumps(mcp_transport.output["rows"], ensure_ascii=False)
    final_text = {
        "json": json.dumps(
            {"results": mcp_transport.output["rows"], "summary": summary},
            ensure_ascii=False,
        ),
        "markdown": f"## results\n{rows_text}\n\n## summary\n{summary}",
        "text": f"{summary}\n相关记录：{rows_text}",
    }[output_format]
    unit, _, _ = _confirmer({"status": "no_match"})
    unit._retriever = _retriever(
        ScriptedModel(
            [
                _tool("get_graph_schema"),
                _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
                _finish(final_text),
            ]
        )
    )
    unit._llm_judge.return_value = DuplicateJudgement(duplicate=duplicate)
    item = _item()
    confirmed, details = await unit.confirm_with_details([item])
    assert confirmed == ([] if duplicate else [item])
    detail = details[0]
    assert detail["retrieval_status"] == "ok"
    assert detail["cm_calls"] == [{"status": "ok"}]
    assert "test-only-token" not in json.dumps(details)
    unit._llm_judge.assert_awaited_once()
    context = unit._llm_judge.call_args.args[1]
    assert context.startswith(
        "知识来源：个人知识\n检索业务域：trade\n检索数据源：ds1\n\n"
    )
    assert context.split("\n\n", 1)[1] == final_text
    assert json.loads(detail["cm_context"])["data"] == final_text
    assert all(
        term not in context
        for term in ('"source"', '"scope"', "personal_graph", "get_graph_schema")
    )


@pytest.mark.parametrize("phase", ["model", "tool"])
async def test_native_fallback_timeout_fails_closed(mcp_transport, phase):
    mcp_transport.output["block"] = True
    unit, _, _ = _confirmer({"status": "no_match"}, cm_timeout=0.1)
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
        ]
    )
    if phase == "model":

        async def wait_for_model(*args, **kwargs):
            await asyncio.Event().wait()

        model._call_api = AsyncMock(side_effect=wait_for_model)
    unit._retriever = _retriever(model)
    item = _item()
    confirmed, details = await unit.confirm_with_details([item])
    detail = details[0]
    assert detail["cm_calls"] == [{"status": "ok"}]
    assert confirmed == []
    assert detail["retrieval_status"] == "error"
    unit._llm_judge.assert_not_awaited()
    if phase == "model":
        model._call_api.assert_awaited_once()
        assert mcp_transport.calls == []
    else:
        assert [name for name, _ in mcp_transport.calls] == [
            "get_graph_schema",
            "query_graph",
        ]
    assert mcp_transport.sessions.count("open") == mcp_transport.sessions.count("close")


async def test_real_react_iteration_limit_returns_framework_summary(mcp_transport):
    model = ScriptedModel([_tool("get_graph_schema"), TextBlock(text="结束")])
    unit = _retriever(model, max_iters=1)
    assert await _retrieve(unit) == "结束"
    assert mcp_transport.calls == [("get_graph_schema", {})]
    assert len(model.seen) == 2


@pytest.mark.parametrize(
    "text",
    [
        "检索结束",
        "没有匹配",
        '{"results": [], "summary": "检索结束"}',
        '```json\n{"results": [], "summary": "检索结束"}\n```',
        '{"results": [格式不完整，但保留模型原文',
        "  ## results\n支付金额求和。\n\n## summary\n检索完成。\n",
        "指标定义：支付金额求和。\n" * 300,
    ],
)
async def test_final_model_text_is_returned_without_parsing(mcp_transport, text):
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(text),
        ]
    )
    unit = _retriever(model)
    assert await _retrieve(unit) == text


async def test_budget_exhaustion_returns_model_summary(mcp_transport):
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(),
        ]
    )
    unit = _retriever(model, max_iters=2)
    assert await _retrieve(unit) == "检索结束"
    assert len(mcp_transport.calls) == 2
    assert len(model.seen) == 3


@pytest.mark.parametrize("text", ["没有找到", '{"rows": [{"name": "虚构定义"}]}'])
async def test_final_text_without_queries_is_returned(mcp_transport, text):
    unit = _retriever(ScriptedModel([_finish(text)]))
    assert await _retrieve(unit) == text
    assert mcp_transport.calls == []


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("truncated", [False, True])
async def test_model_summarizes_multiple_queries_for_confirmer(
    mcp_transport, duplicate, truncated
):
    first = {"name": "GMV", "description": "支付金额求和"}
    second = {"name": "净GMV", "description": "支付金额扣除退款"}
    final_text = json.dumps(
        {"results": [first, second], "summary": "没有完全一致的口径"},
        ensure_ascii=False,
    )
    mcp_transport.output["responses"] = [
        {"rows": [first], "truncated": truncated},
        {"rows": [second], "truncated": truncated},
    ]
    unit, _, _ = _confirmer({"status": "no_match"})
    unit._retriever = _retriever(
        ScriptedModel(
            [
                _tool("get_graph_schema"),
                _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
                _tool("query_graph", {"cypher": "RETURN 2 AS n"}),
                _finish(final_text),
            ]
        )
    )
    unit._llm_judge.return_value = DuplicateJudgement(duplicate=duplicate)
    item = _item()
    confirmed, _ = await unit.confirm_with_details([item])
    assert confirmed == ([] if duplicate else [item])
    unit._llm_judge.assert_awaited_once()
    assert unit._llm_judge.call_args.args[1].split("\n\n", 1)[1] == final_text


@pytest.mark.parametrize(
    "kind", ["metric_caliber", "dimension_def", "column_meaning", "dataset_usage"]
)
def test_retrieval_message_keeps_original_field_names_and_values(kind):
    unit = _retriever(object())
    item = _item(kind)
    original_fields = item.fields.copy()
    message = unit._build_user_message(
        item,
        datasource_id="ds1",
        domain="trade",
    )
    assert message.startswith(f"## 检索对象\n类型：{kind}\n")
    fields_text = message.split("\n\n## 检索条件", 1)[0].splitlines()[2:]
    expected_fields = {
        name: value for name, value in original_fields.items() if name != "domain"
    }
    assert fields_text == [
        f"- {name}: {value}" for name, value in expected_fields.items()
    ]
    assert item.fields == original_fields
    definitions = unit._system_prompt.split("## 检索步骤", 1)[0]
    row = next(line for line in definitions.splitlines() if f"`{kind}`" in line)
    assert {
        field.split("`")[1] for field in row.split("|")[2].strip().split("、")
    } == set(expected_fields)
    assert message.count("业务域名称：trade") == 1
    assert "## 检索条件" in message
    assert "图范围：" not in message
    assert "数据源 ID：ds1" in message
    assert all(value in message for value in item.fields.values())
    assert "已有元数据查询结果" not in message
    for text in (message, unit._system_prompt):
        for marker in (
            "candidate",
            "Settlement",
            "Confirmer",
            "output_schema",
            "检索问题",
            "检索线索",
            "输入线索",
            "字段字典",
            "字段：{",
            "提取项",
        ):
            assert marker not in text
        assert "evidence_refs" not in text


@pytest.mark.parametrize("datasource_id", ["ds1", None])
def test_retrieval_message_preserves_multiline_fields_without_prior_result(
    datasource_id,
):
    unit = _retriever(object())
    item = _item()
    item.fields["caliber"] = "支付金额：含退款\n退款以负数抵扣"
    item.fields["formula_sql"] = (
        "SELECT SUM(amount)\nFROM orders\nWHERE status = 'paid'"
    )
    item.fields["extra_context"] = "补充说明"
    original_fields = item.fields.copy()
    message = unit._build_user_message(
        item, datasource_id=datasource_id, domain="trade"
    )
    assert f"- caliber: {item.fields['caliber']}\n" in message
    assert f"- formula_sql: {item.fields['formula_sql']}\n" in message
    assert "- extra_context: 补充说明\n" in message
    expected_datasource = datasource_id or "未指定，不预设数据源"
    assert message.endswith(f"数据源 ID：{expected_datasource}")
    assert "已有元数据查询结果" not in message
    assert item.fields == original_fields


async def test_native_query_errors_are_summarized_by_model(mcp_transport):
    mcp_transport.output["isError"] = True
    final_text = "图查询失败，未取得可用定义。"
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(final_text),
        ]
    )
    unit = _retriever(model)
    assert await _retrieve(unit) == final_text
    assert len(mcp_transport.calls) == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"rows": [{"description": "定义"}], "truncated": false}',
        '{"rows": [], "truncated": false}',
        '{"data": [{"name": "GMV"}]}',
        "{}",
        "[]",
        "查询结果：GMV = SUM(amount)",
    ],
)
async def test_tool_payload_is_interpreted_by_model_not_retriever(mcp_transport, text):
    mcp_transport.output["raw_text"] = text
    final_text = "已整理查询内容；不足以确认的内容已注明。"
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(final_text),
        ]
    )
    unit = _retriever(model)
    assert await _retrieve(unit) == final_text
    assert text in str(model.seen[-1][0])


@pytest.mark.parametrize("issue", ["error", "truncated"])
@pytest.mark.parametrize("last_issue", [False, True])
async def test_query_status_does_not_rewrite_model_output(
    mcp_transport, issue, last_issue
):
    good = dict(mcp_transport.output)
    partial = {**good, "isError": issue == "error", "truncated": issue == "truncated"}
    mcp_transport.output["responses"] = (
        [good, partial] if last_issue else [partial, good]
    )
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _tool("query_graph", {"cypher": "RETURN 2 AS n"}),
            _finish(),
        ]
    )
    unit = _retriever(model)
    result = await _retrieve(unit)
    assert len(mcp_transport.calls) == 3
    assert result == "检索结束"


@pytest.mark.parametrize("kind", ["column_meaning", "dataset_usage"])
async def test_empty_query_result_does_not_require_target_judgement(
    mcp_transport, kind
):
    mcp_transport.output.update(rows=[], row_count=0)
    model = ScriptedModel(
        [
            _tool("get_graph_schema"),
            _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            _finish(),
        ]
    )
    unit = _retriever(model)
    assert await _retrieve(unit, kind) == "检索结束"


@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_empty_model_output_is_returned_without_validation(mcp_transport, text):
    unit = _retriever(
        ScriptedModel(
            [
                _tool("get_graph_schema"),
                _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
                _finish(text),
            ]
        )
    )
    assert await _retrieve(unit) == text


async def test_schema_only_returns_model_text(mcp_transport):
    unit = _retriever(ScriptedModel([_tool("get_graph_schema"), _finish()]))
    assert await _retrieve(unit) == "检索结束"
    assert mcp_transport.calls == [("get_graph_schema", {})]


async def test_native_tool_whitelist_blocks_business_sql(mcp_transport):
    unit = _retriever(
        ScriptedModel([_tool("execute_read", {"sql": "SELECT 1"}), _finish()])
    )
    assert await _retrieve(unit) == "检索结束"
    assert mcp_transport.calls == []


async def test_tool_cancellation_propagates_and_closes_sessions(
    mcp_transport,
):
    mcp_transport.output["block"] = True
    unit = _retriever(
        ScriptedModel(
            [
                _tool("get_graph_schema"),
                _tool("query_graph", {"cypher": "RETURN 1 AS n"}),
            ]
        )
    )
    task = asyncio.create_task(_retrieve(unit))
    async with asyncio.timeout(2):
        while not any(name == "query_graph" for name, _ in mcp_transport.calls):
            await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert mcp_transport.sessions.count("open") == mcp_transport.sessions.count("close")


async def test_rest_endpoints_and_removed_search_context(monkeypatch):
    cm = SettlementCmClient(datasource_id="ds1")
    request = AsyncMock(return_value={})
    monkeypatch.setattr(cm, "_request", request)
    for tool, path in [
        ("search_metrics", "search-metrics"),
        ("get_dimension", "dimensions"),
        ("get_dataset", "datasets"),
    ]:
        await cm.call(tool, {"domain": "trade", "name": "x", "query": "x"})
        assert request.call_args.args == ("GET", f"/api/v1/cm/{path}")
        assert request.call_args.kwargs["params"]["datasource_id"] == "ds1"
    request.reset_mock()
    assert (await cm.call("search_context", {}))["status"].startswith("error")
    request.assert_not_awaited()
    await cm.call("feedback_card", {"id": "card"}, mode="test")
    assert request.call_args.args == ("POST", "/api/v1/semantic/feedback_card")
    assert request.call_args.kwargs["params"] == {"mode": "test"}


async def test_http_status_is_preserved_without_credentials(monkeypatch):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, json={"detail": "Metadata read is forbidden"}
        )
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    cm = SettlementCmClient(access_token="test-only-token")
    record = await cm.call("get_dataset", {"name": "orders"})
    assert record["http_status"] == 403
    assert "test-only-token" not in str(record)
    with pytest.raises(httpx.HTTPStatusError) as error:
        await cm.get_dataset({"name": "orders"})
    assert error.value.response.status_code == 403
    assert "Metadata read is forbidden" in str(error.value)
    assert "test-only-token" not in str(error.value)


async def test_manager_model_factory_preserves_usage(monkeypatch):
    manager = SettlementManager(object())
    active = object()
    config = SimpleNamespace(default=active, light=None)
    load = AsyncMock(return_value=SimpleNamespace(resolve=lambda: config))
    monkeypatch.setattr(
        "qwenpaw_data.host.core.repositories.PreferencesRepository.load", load
    )
    model = object()
    build = Mock(return_value=model)
    profile = Mock(side_effect=lambda model: model)
    monkeypatch.setattr("qwenpaw_data.host.core.providers.factory.build_model", build)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.llm.for_structured_calls", profile
    )
    assert await manager._build_model(session_id="s1", chat_id="c1") is model
    profile.assert_called_once_with(model)
    load.assert_awaited_once()
    build.assert_called_once()
    assert build.call_args.args == (active,)
    usage = build.call_args.kwargs["usage"]
    assert usage.source == ModelUsageSource.SETTLEMENT
    assert usage.identity is manager._identity
    assert (usage.session_id, usage.chat_id) == ("s1", "c1")


async def test_manager_api_fast_path_skips_retrieval(monkeypatch):
    manager = SettlementManager(object())
    build = AsyncMock()
    monkeypatch.setattr(manager, "_build_model", build)
    retrieve = AsyncMock()
    monkeypatch.setattr(SettlementRetriever, "retrieve", retrieve)
    judge = AsyncMock(return_value=DuplicateJudgement(duplicate=True))
    monkeypatch.setattr(SettlementConfirmer, "_llm_judge", judge)
    _, cm, _ = _confirmer([{"metric_name": "GMV", "description": "支付金额"}])
    assert (
        await manager._confirm_with_cm(
            object(),
            cm,
            [_item()],
            session_id="s1",
            chat_id="c1",
            datasource_id="ds1",
            domain_names=["trade"],
        )
        == []
    )
    build.assert_not_called()
    retrieve.assert_not_awaited()
    judge.assert_awaited_once()


@pytest.mark.parametrize("use_light", [False, True])
async def test_manager_shares_model_with_isolated_retrieval_and_judgement(
    monkeypatch, mcp_transport, use_light
):
    manager = SettlementManager(
        object(), settings=SettlementSettings(retrieval_max_iters=5, cm_timeout=7)
    )
    default, light = object(), object() if use_light else None
    config = SimpleNamespace(default=default, light=light)
    load = AsyncMock(return_value=SimpleNamespace(resolve=lambda: config))
    monkeypatch.setattr(
        "qwenpaw_data.host.core.repositories.PreferencesRepository.load", load
    )
    mcp_transport.output.update(
        rows=[{"key": "orders", "name": "orders", "description": None}],
        columns=["key", "name", "description"],
    )
    first_calls = asyncio.Barrier(2)
    judge_started = asyncio.Event()

    class SharedModel(ScriptedModel):
        async def _call_api(
            self, model_name, messages, tools=None, tool_choice=None, **kwargs
        ):
            self.seen.append((deepcopy(messages), deepcopy(tools)))
            user_text = next(
                msg.get_text_content() for msg in messages if msg.role == "user"
            )
            subject = "渠道分析" if "渠道分析" in user_text else "订单分析"
            names = {tool["function"]["name"] for tool in tools}
            if names == {"generate_structured_output"}:
                assert f"{subject}相关定义" in user_text
                judge_started.set()
                block = _tool(
                    "generate_structured_output",
                    {"duplicate": subject == "渠道分析", "reason": "已比较相关定义"},
                )
            else:
                history = [
                    block
                    for msg in messages
                    for block in msg.get_content_blocks("tool_call")
                ]
                if not history:
                    await first_calls.wait()
                    block = _tool("get_graph_schema")
                elif len(history) == 1:
                    block = _tool(
                        "query_graph",
                        {"cypher": "RETURN $name AS name", "params": {"name": subject}},
                    )
                else:
                    if subject == "渠道分析":
                        await judge_started.wait()
                    block = _finish(f"{subject}相关定义")
            return ChatResponse(
                content=[block],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )

    model = SharedModel([])
    model.stream = True
    model.parameters = DashScopeChatModel.Parameters(thinking_enable=True)
    build = Mock(return_value=model)
    profile = Mock(wraps=for_structured_calls)
    monkeypatch.setattr("qwenpaw_data.host.core.providers.factory.build_model", build)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.llm.for_structured_calls", profile
    )
    agents = []

    def create_agent(**kwargs):
        agent = QwenPawDataAgent(**kwargs)
        agents.append(agent)
        return agent

    build_agent = Mock(side_effect=create_agent)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.algo.settlement.retriever.QwenPawDataAgent", build_agent
    )
    cm = SettlementCmClient(access_token="test-only-token", datasource_id="ds1")
    items = [_item("dataset_usage"), _item("dataset_usage")]
    items[1].fields["use_case"] = "渠道分析"
    events = [
        SimpleNamespace(
            object="message", role="user", content=[TextBlock(text="订单分析")]
        )
    ]
    cards = [{"id": "card-1"}]
    for name, output in {
        "_read_window_events": events,
        "_load_pool_summary": [],
        "_load_datasource_id": "ds1",
        "_create_cm_client": cm,
        "_resolve_domain_names": ["trade"],
        "_detect": SimpleNamespace(items=items),
        "_filter_dismissed": items,
        "_filter_feedback_dry_run": items[:1],
        "_commit_cards": cards,
    }.items():
        monkeypatch.setattr(manager, name, AsyncMock(return_value=output))

    assert await manager.detect_for_chat(session_id="s1", chat_id="c1") == cards
    load.assert_awaited_once()
    profile.assert_called_once_with(model)
    build.assert_called_once()
    assert build.call_args.args == (light or default,)
    usage = build.call_args.kwargs["usage"]
    assert usage.source == ModelUsageSource.SETTLEMENT
    assert usage.identity is manager._identity
    assert (usage.session_id, usage.chat_id) == ("s1", "c1")
    assert model.stream is False
    assert model.parameters.thinking_enable is False
    assert model.parameters.temperature == 0.0
    assert len(agents) == 2
    assert agents[0].state is not agents[1].state
    assert all(agent.model is model for agent in agents)
    assert all(
        call.kwargs["react_config"].max_iters == 5
        for call in build_agent.call_args_list
    )
    assert len(model.seen) == 8
    judgements = []
    for messages, tools in model.seen:
        user_text = next(
            msg.get_text_content() for msg in messages if msg.role == "user"
        )
        other = "订单分析" if "渠道分析" in user_text else "渠道分析"
        assert other not in str(messages[1:])
        names = {tool["function"]["name"] for tool in tools}
        if names == {"generate_structured_output"}:
            judgements.append(user_text)
            assert not any(msg.get_content_blocks("tool_result") for msg in messages)
        else:
            assert names == {
                "Skill",
                "Read",
                f"mcp__{CmSettings.MCP_NAME}__get_graph_schema",
                f"mcp__{CmSettings.MCP_NAME}__query_graph",
                f"mcp__{CmSettings.MCP_NAME}__search_graph_fulltext",
                f"mcp__{CmSettings.MCP_NAME}__search_graph_vector",
            }
    assert len(judgements) == 2
    assert len({id(client) for client in mcp_transport.clients}) == 2
    for client in mcp_transport.clients:
        assert client._runtime_headers == {
            "Authorization": "Bearer test-only-token",
            "datasource_id": "ds1",
            "session_id": "s1",
            "chat_id": "c1",
        }
        assert client.execution_timeout == 7
    assert mcp_transport.sessions.count("open") == mcp_transport.sessions.count("close")
    assert manager._filter_feedback_dry_run.call_args.args[0] == items[:1]


@pytest.mark.parametrize("ack", ["accepted", "updated", "duplicate", "rejected"])
async def test_detection_still_requires_feedback_preflight_before_commit(
    monkeypatch, ack
):
    manager = SettlementManager(object())
    build = AsyncMock(return_value=object())
    monkeypatch.setattr(manager, "_build_model", build)
    item = _item()
    events = [
        SimpleNamespace(
            object="message", role="user", content=[TextBlock(text="支付金额求和")]
        )
    ]
    _, cm, _ = _confirmer()
    cm.call.return_value = {"status": "ok", "result": json.dumps({"status": ack})}
    for name, output in {
        "_read_window_events": events,
        "_load_pool_summary": [],
        "_load_datasource_id": "ds1",
        "_create_cm_client": cm,
        "_resolve_domain_names": ["trade"],
        "_detect": SimpleNamespace(items=[item]),
        "_filter_dismissed": [item],
        "_confirm_with_cm": [item],
    }.items():
        monkeypatch.setattr(manager, name, AsyncMock(return_value=output))
    commit = AsyncMock(return_value=[{"id": "new-card"}])
    monkeypatch.setattr(manager, "_commit_cards", commit)
    result = await manager.detect_for_chat(session_id="s1", chat_id="c1")
    assert bool(result) == (ack in {"accepted", "updated"})
    assert commit.await_count == int(ack in {"accepted", "updated"})
    build.assert_awaited_once_with(session_id="s1", chat_id="c1")
    manager._confirm_with_cm.assert_awaited_once()
    assert manager._confirm_with_cm.call_args.args[0] is build.return_value
    assert manager._confirm_with_cm.call_args.kwargs["session_id"] == "s1"
    assert manager._confirm_with_cm.call_args.kwargs["chat_id"] == "c1"
    cm.call.assert_awaited_once()
    call = cm.call.call_args
    assert call.args[0] == "feedback_card"
    assert call.kwargs == {"max_len": 0, "mode": "test"}
    assert call.args[1]["datasource_id"] == "ds1"
    assert call.args[1]["extra"]["session_id"] == "s1"
    assert call.args[1]["extra"]["source_chat_id"] == "c1"


def test_retrieval_budget_setting(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_SETTLEMENT_RETRIEVAL_MAX_ITERS", "5")
    assert SettlementSettings().retrieval_max_iters == 5
    with pytest.raises(ValueError):
        SettlementSettings(retrieval_max_iters=0)
