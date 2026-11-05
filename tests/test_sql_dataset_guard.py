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
    guard.validate_dataset_sql(
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
