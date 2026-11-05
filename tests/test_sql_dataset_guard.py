from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from context_manager.api import sql_dataset_guard as guard


def seed(monkeypatch, records):
    monkeypatch.setattr(
        guard, "load_datasource_config", lambda _: {"_datasource_type": "duckdb"}
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, query, **params):
            assert "ds.datasource_id = $datasource_id" in query
            assert params["datasource_id"] == "econ"
            return SimpleNamespace(data=lambda: records)

    monkeypatch.setattr(guard, "neo4j_session", lambda _: Session())


def validate(sql, names=None):
    return guard.validate_dataset_sql(
        sql=sql,
        dataset_names=names or ["retail"],
        datasource_id="econ",
        driver=object(),
        dialect="duckdb",
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM econ_observations",
        'SELECT * FROM "econ_observations" AS e',
        "WITH retail_monthly AS (SELECT * FROM econ_observations) SELECT * FROM retail_monthly",
        "SELECT * FROM (SELECT * FROM econ_observations) AS x JOIN econ_observations y ON true",
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<3) SELECT * FROM x",
    ],
)
def test_real_tables_and_ctes(monkeypatch, sql):
    seed(monkeypatch, [{"name": "retail", "parents": "econ_observations"}])
    validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM retail_monthly",
        "WITH x AS (SELECT * FROM missing) SELECT * FROM x",
        "SELECT * FROM econ_observations e JOIN missing m ON true",
        "WITH x AS (SELECT * FROM econ_observations) SELECT * FROM other.x",
        "SELECT * FROM other.econ_observations",
    ],
)
def test_unknown_tables_rejected_with_allowed_names(monkeypatch, sql):
    seed(monkeypatch, [{"name": "retail", "parents": "econ_observations"}])
    with pytest.raises(HTTPException) as error:
        validate(sql)
    assert error.value.detail["code"] == "SQL_TABLE_OUTSIDE_DATASETS"
    assert error.value.detail["allowed_tables"] == ["econ_observations"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM",
        "DROP TABLE econ_observations",
        "SELECT 1; SELECT 2",
        "SELECT * FROM read_csv('/tmp/data.csv')",
        "SELECT * FROM range(10)",
    ],
)
def test_unparsed_and_unsupported_never_bypass(monkeypatch, sql):
    seed(monkeypatch, [{"name": "retail", "parents": "econ_observations"}])
    with pytest.raises(HTTPException):
        validate(sql)


def test_multiple_datasets_and_qualified_tables(monkeypatch):
    seed(
        monkeypatch,
        [
            {"name": "retail", "parents": '["main.econ_observations"]'},
            {"name": "macro", "parents": ["main.macro"]},
        ],
    )
    validate(
        "SELECT * FROM main.econ_observations JOIN macro ON true", ["retail", "macro"]
    )
    with pytest.raises(HTTPException):
        validate("SELECT * FROM other.macro", ["retail", "macro"])


def test_missing_ambiguous_and_graph_failure(monkeypatch):
    seed(monkeypatch, [])
    with pytest.raises(HTTPException, match="Confirm datasets"):
        validate("SELECT 1")
    seed(
        monkeypatch,
        [{"name": "retail", "parents": "a"}, {"name": "retail", "parents": "b"}],
    )
    with pytest.raises(HTTPException) as err:
        validate("SELECT 1")
    assert err.value.detail["code"] == "SQL_DATASET_AMBIGUOUS"
    monkeypatch.setattr(
        guard,
        "neo4j_session",
        lambda _: (_ for _ in ()).throw(RuntimeError("graph down")),
    )
    with pytest.raises(HTTPException) as err:
        validate("SELECT 1")
    assert err.value.status_code == 503
    guard.validate_dataset_sql(
        sql="SELECT * FROM anything", dataset_names=None, datasource_id="", driver=None
    )
    with pytest.raises(HTTPException):
        guard.validate_dataset_sql(
            sql="SELECT 1", dataset_names=[], datasource_id="econ", driver=None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["cm_execute_sql", "cm_execute_sql_stream"])
async def test_guard_before_cache_or_execution(monkeypatch, endpoint):
    from context_manager.api import cm_api
    from context_manager.api.cm_models import ExecuteSqlRequest

    seed(monkeypatch, [{"name": "retail", "parents": "econ_observations"}])

    class IO:
        async def run(self, pool, operation, fn, *args, **kwargs):
            if operation == "context.session.ensure":
                return SimpleNamespace(scope={}), "session"
            if operation == "context.datasource.resolve":
                return "econ"
            assert operation == "sql.dataset_scope.validate", (
                "cache/execution reached before rejecting SQL"
            )
            return fn(*args, **kwargs)

    monkeypatch.setattr(cm_api, "_driver", lambda _: object())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(blocking_io=IO()))
    )
    with pytest.raises(HTTPException) as err:
        await getattr(cm_api, endpoint)(
            ExecuteSqlRequest(
                sql="SELECT * FROM retail_monthly", dataset_names=["retail"]
            ),
            request,
        )
    assert err.value.detail["code"] == "SQL_TABLE_OUTSIDE_DATASETS"


def test_parser_uses_selected_datasource_dialect(monkeypatch):
    seed(monkeypatch, [{"name": "retail", "parents": "`db`.`order`"}])
    monkeypatch.setattr(
        guard, "load_datasource_config", lambda _: {"_datasource_type": "mysql"}
    )
    guard.validate_dataset_sql(
        sql="SELECT * FROM `db`.`order`",
        dataset_names=["retail"],
        datasource_id="econ",
        driver=object(),
    )


@pytest.mark.asyncio
async def test_mcp_forwards_explicit_and_metadata_scope(monkeypatch):
    from context_manager.mcp import cm_server

    captured = []

    async def post(endpoint, body):
        captured.append(body)
        return {"exec_status": "empty"}

    async def keepalive(awaitable, *args, **kwargs):
        return await awaitable

    monkeypatch.setattr(cm_server, "_post", post)
    monkeypatch.setattr(cm_server, "_with_keepalive", keepalive)
    monkeypatch.setattr(cm_server, "_meta_datasource_id", lambda _: "econ")
    await cm_server.execute_sql("SELECT 1", None, dataset_names=["retail"])
    await cm_server.execute_sql(
        "SELECT 1", None, metadata='{"dataset_names": ["retail"]}'
    )
    assert all(
        b["dataset_names"] == ["retail"] and b["datasource_id"] == "econ"
        for b in captured
    )


@pytest.mark.parametrize("table", ["retail", "main.retail", "cm_view.retail"])
def test_logical_dataset_resolved_before_physical_scope_check(monkeypatch, table):
    seed(monkeypatch, [{"name": "retail", "parents": "main.econ_observations"}])
    resolved = validate(f"SELECT * FROM {table}")
    assert guard.extract_physical_tables(resolved, dialect="duckdb") == {
        ("main", "econ_observations")
    }


def test_logical_dataset_cannot_cross_schema_or_choose_multiple_parents(monkeypatch):
    seed(monkeypatch, [{"name": "retail", "parents": "main.econ_observations"}])
    with pytest.raises(HTTPException):
        validate("SELECT * FROM other.retail")
    seed(monkeypatch, [{"name": "retail", "parents": ["main.a", "main.b"]}])
    with pytest.raises(HTTPException) as raised:
        validate("SELECT * FROM retail")
    assert raised.value.detail["code"] == "SQL_DATASET_AMBIGUOUS"
    validate("SELECT * FROM main.a JOIN main.b ON true")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM main.econ_observations WHERE year > 2020",
        "SELECT sum(value) FROM main.econ_observations",
        "SELECT value AS renamed FROM main.econ_observations",
    ],
)
def test_dataset_transformations_cannot_be_silently_dropped(monkeypatch, sql):
    seed(
        monkeypatch,
        [{"name": "retail", "parents": "main.econ_observations", "sql": sql}],
    )
    with pytest.raises(HTTPException) as raised:
        validate("SELECT * FROM retail")
    assert raised.value.detail["code"] == "SQL_DATASET_REWRITE_UNSAFE"
    validate("SELECT sum(value) FROM main.econ_observations")


def test_plain_projection_can_be_resolved(monkeypatch):
    seed(
        monkeypatch,
        [
            {
                "name": "retail",
                "parents": "main.econ_observations",
                "sql": "SELECT * FROM main.econ_observations",
            }
        ],
    )
    assert "econ_observations" in validate("SELECT * FROM retail")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["cm_execute_sql", "cm_execute_sql_stream"])
@pytest.mark.parametrize("stale_cache", [False, True])
async def test_both_routes_execute_resolved_sql_without_fallthrough(
    monkeypatch, endpoint, stale_cache
):
    from context_manager.api import cm_api
    from context_manager.api.cm_models import ExecuteSqlRequest
    from context_manager.api.executor import ExecResult

    seed(monkeypatch, [{"name": "retail", "parents": "main.econ_observations"}])
    calls = []

    def execute(sql, **kwargs):
        assert kwargs["allow_fallthrough"] is False
        assert guard.extract_physical_tables(sql, dialect="duckdb") == {
            ("main", "econ_observations")
        }
        calls.append("execute")
        return ExecResult(sql=sql)

    class IO:
        async def run(self, pool, operation, fn, *args, **kwargs):
            if operation == "context.session.ensure":
                return SimpleNamespace(scope={}), "session"
            if operation == "context.datasource.resolve":
                return "econ"
            if operation == "sql.dataset_scope.validate":
                calls.append("validate")
                return fn(*args, **kwargs)
            if operation == "sql.cache.get":
                assert calls == ["validate"]
                assert "econ_observations" in args[0]
                return (
                    SimpleNamespace(result=ExecResult(sql="SELECT * FROM outside"))
                    if stale_cache
                    else None
                )
            return fn(*args, **kwargs)

    async def execute_async(sql, **kwargs):
        return execute(sql, **kwargs)

    monkeypatch.setattr(cm_api, "_driver", lambda _: object())
    monkeypatch.setattr(cm_api, "execute_sql_async", execute_async)
    monkeypatch.setattr(cm_api, "topology_execute_sql", execute)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(blocking_io=IO()))
    )
    response = await getattr(cm_api, endpoint)(
        ExecuteSqlRequest(sql="SELECT * FROM retail", dataset_names=["retail"]), request
    )
    if endpoint.endswith("stream"):
        chunks = [chunk async for chunk in response.body_iterator]
        assert any("done" in str(chunk) for chunk in chunks)
    else:
        assert response.exec_status == "empty"
    assert calls == ["validate", "execute"]
