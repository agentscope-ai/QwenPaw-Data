"""spider2 gold SQL 作为查询日志：文件来源 + parser 方言感知归一（零 LLM）。

四层各钉一条：

1. 来源：``sql_files`` 分派 / config 与 connection 两种定位 / 无 gold 的库
   降级为空 / 指纹按注册表分派到实现（文件清单，不是时间窗）；
2. parser：BigQuery 分片三形态折成族名，PG / sqlite 行为不变；
   ``parser_template_min_columns`` 真正接线；
3. 证据：IPL 两条 gold → join 边落到真实表；ga360 十二条 gold →
   证据表名全部归一到 ``ga_sessions_*``（与 JsonInput 的族名对齐）；
4. context：``AlgoContexts.build`` 装配后 ``query_log.load()`` 产出证据
   （原始 + 解析两级缓存与指纹一并走通）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[6]
_SPIDER = _REPO / "data" / "Spider2" / "spider2-lite"
_INDEX = _SPIDER / "spider2-lite.jsonl"
_GOLD = _SPIDER / "evaluation_suite" / "gold" / "sql"
_LOCALDB = _SPIDER / "resource" / "databases" / "spider2-localdb"

pytestmark = pytest.mark.skipif(
    not (_INDEX.is_file() and _GOLD.is_dir()), reason="Spider2 gold 数据缺失"
)


def _config(db: str, *, via_connection: bool = False, **extra):
    from profiling.algo.config import AlgoConfig

    fields = dict(
        datasource_id=f"ut-{db}",
        datasource_type="sqlite",
        execute_sql_path="",
        input_query_log_datasource_type="sql_files",
        **extra,
    )
    if via_connection:
        fields["datasource_connection"] = {
            "db_path": str(_LOCALDB / f"{db}.sqlite"),
            "query_log_index": str(_INDEX),
            "query_log_dir": str(_GOLD),
            "query_log_db": db,
        }
    else:
        fields.update(
            datasource_connection={"db_path": str(_LOCALDB / f"{db}.sqlite")},
            input_query_log_path=str(_INDEX),
            input_query_log_dir=str(_GOLD),
            input_query_log_dbname=db,
        )
    return AlgoConfig.build(**fields)


# ===========================================================================
# 1. 来源
# ===========================================================================


def test_sql_files_dispatch_and_fetch_ipl():
    from profiling.algo.inputs import QueryLogInput
    from profiling.algo.inputs.query_log_sql_files import SqlFileQueryLogInput

    cfg = _config("IPL")
    source = QueryLogInput.create(cfg)
    assert isinstance(source, SqlFileQueryLogInput)
    assert source.available()
    raw = asyncio.run(source.fetch())
    assert len(raw.logs) == 2
    assert set(raw.logs["instance_id"]) == {"local022", "local023"}
    assert {"query", "instance_id", "question"} <= set(raw.logs.columns)
    assert all(text for text in raw.statements())


def test_sql_files_connection_keys_fallback():
    from profiling.algo.inputs import QueryLogInput

    raw = asyncio.run(QueryLogInput.create(_config("IPL", via_connection=True)).fetch())
    assert len(raw.logs) == 2


def test_sql_files_db_without_gold_degrades_to_empty():
    from profiling.algo.inputs import QueryLogInput
    from profiling.algo.inputs.query_log_sql_files import SqlFileQueryLogInput

    source = QueryLogInput.create(_config("chinook"))
    assert isinstance(source, SqlFileQueryLogInput)
    assert not source.available()
    assert asyncio.run(source.fetch()).is_empty()


def test_fingerprint_dispatches_to_implementation():
    from profiling.algo.config import AlgoConfig
    from profiling.algo.inputs import QueryLogInput

    files = QueryLogInput.fingerprint(_config("IPL"))
    assert files["datasource_type"] == "sql_files"
    assert [item[0] for item in files["files"]] == ["local022", "local023"]
    assert "input_query_log_start_ts" not in files
    # 未认领类型仍是 SQL 系默认判据
    plain = QueryLogInput.fingerprint(AlgoConfig.build(datasource_type="sqlite"))
    assert "input_query_log_start_ts" in plain and "files" not in plain


# ===========================================================================
# 2. parser 归一 + 阈值接线
# ===========================================================================


def test_short_table_name_is_dialect_aware():
    from profiling.algo.parser.base import (
        PARSER_VERSION,
        short_table_name,
        short_table_ref,
    )

    assert PARSER_VERSION >= 2
    for form in (
        "bigquery-public-data.google_analytics_sample.ga_sessions_20170801",
        "ga_sessions_201707*",
        "ga_sessions_*",
    ):
        assert short_table_name(form, "bigquery") == "ga_sessions_*", form
    assert short_table_name("2000_*", "bigquery") == "2000_*"
    assert short_table_name("crime", "bigquery") == "crime"
    # PG / sqlite：剥后缀回基表名，行为不变
    assert short_table_name("dwd.orders_20260101") == "orders"
    assert short_table_name("orders_20260101", "sqlite") == "orders"
    assert (
        short_table_ref("a.ga_sessions_20170801,b.crime", "bigquery")
        == "crime,ga_sessions_*"
    )


def test_template_min_columns_is_wired():
    from profiling.algo.parser.pipeline import parse_evidence
    from profiling.algo.snapshots.query_log import RawQueryLogSnapshot

    raw = RawQueryLogSnapshot.from_statements(
        ["SELECT a, b FROM t1 JOIN t2 ON t1.id = t2.id WHERE t1.c = 1"]
    )
    strict = asyncio.run(parse_evidence(raw, dialect="sqlite", template_min_count=1))
    loose = asyncio.run(
        parse_evidence(
            raw, dialect="sqlite", template_min_count=1, template_min_columns=2
        )
    )
    assert len(strict.evidence["sql_template"]) == 0  # 默认 5 列门槛
    assert len(loose.evidence["sql_template"]) == 1


# ===========================================================================
# 3. 证据
# ===========================================================================


def test_ipl_gold_yields_join_evidence_on_real_tables():
    from profiling.algo.inputs import QueryLogInput
    from profiling.algo.parser.pipeline import parse_evidence

    raw = asyncio.run(QueryLogInput.create(_config("IPL")).fetch())
    parsed = asyncio.run(parse_evidence(raw, dialect="sqlite", template_min_count=1))
    joins = parsed.evidence["join"]
    assert len(joins) > 0, "IPL gold 含 JOIN，却没有 join 证据"
    with sqlite3.connect(_LOCALDB / "IPL.sqlite") as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    referenced = set(joins["left_table"]) | set(joins["right_table"])
    assert referenced <= tables, f"join 证据引用了非物理表: {referenced - tables}"
    assert len(parsed.failed) == 0


def test_ga360_gold_normalizes_to_shard_family():
    from profiling.algo.config import AlgoConfig
    from profiling.algo.inputs import QueryLogInput
    from profiling.algo.parser.pipeline import parse_evidence

    cfg = AlgoConfig.build(
        datasource_type="bigquery_json",
        execute_sql_path="",
        input_query_log_datasource_type="sql_files",
        input_query_log_path=str(_INDEX),
        input_query_log_dir=str(_GOLD),
        input_query_log_dbname="ga360",
    )
    raw = asyncio.run(QueryLogInput.create(cfg).fetch())
    assert len(raw.logs) >= 10
    parsed = asyncio.run(parse_evidence(raw, dialect="bigquery", template_min_count=1))
    assert len(parsed.failed) == 0
    names: set[str] = set()
    for kind in ("dimension_usage", "metric_expr"):
        frame = parsed.evidence[kind]
        names |= {str(n) for n in frame["table_name"] if str(n) != "_"}
    assert names, "ga360 gold 没有抽出任何列级证据"
    assert names == {"ga_sessions_*"}, f"分片形态未折成族名: {names}"


# ===========================================================================
# 4. context 装配
# ===========================================================================


def test_query_log_context_end_to_end_ipl():
    from profiling.algo import AlgoContexts

    with tempfile.TemporaryDirectory() as tmp:
        cfg = _config(
            "IPL",
            store_out_dir=tmp,
            parser_dialect="sqlite",
            parser_template_min_count=1,
            store_excel_enabled=False,
        )
        contexts = AlgoContexts.build(cfg)
        logs = asyncio.run(contexts.query_log.load())
        assert len(logs.of("join")) > 0
        assert len(logs.of("dimension_usage")) > 0
        # 两级缓存都落了盘，且指纹匹配可回读
        assert list((Path(tmp) / cfg.datasource_id / "query_log_raw").glob("*.parquet"))
        again = asyncio.run(contexts.query_log.load(refresh=True))
        assert len(again.of("join")) == len(logs.of("join"))
