"""Read actual editor/weaving output through Host's four public MG endpoints."""

import httpx
import pytest
from context_manager.auth import bind_authorization
from context_manager.schema import DIMENSION, DOMAIN, METRIC
from context_manager.store import legacy_reads

from cm.infrastructure.access.support import authorization
from cm.knowledge.support import MemoryGraphStore
from cm.metadata.test_weaving import (
    editor as editor,  # noqa: PLC0414 - expose the shared pytest fixture.
)
from cm.metadata.test_weaving import (
    editor_database as editor_database,  # noqa: PLC0414 - intentional test fixture
)
from cm.metadata.test_weaving import seed
from cm.retrieval.metadata_support import metric_index_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("editor", ["memory", "neo4j"], indirect=True)
async def test_editor_publication_is_readable_with_datasource_and_domain_name(
    editor, monkeypatch, caplog  # noqa: F811 - imported pytest fixture
):
    native = not isinstance(editor.store, MemoryGraphStore)
    if native:
        editor.caller.options["dimensions"] = 1024
    data = await seed(editor)
    for spec in (DOMAIN, METRIC, DIMENSION):
        records = editor.store.list_nodes(spec, scope=editor.scope)
        assert records and all(
            not record.properties.get("datasource_id") for record in records
        )
    # The in-memory Store deliberately has no vector index; real Neo4j uses its index.
    if not native:
        monkeypatch.setattr(editor.store, "search_vector", lambda *args, **kwargs: ())
        monkeypatch.setattr(legacy_reads, "search_fulltext_scores", metric_index_rows)
    member = authorization("member", workspace_id=editor.scope.workspace_id)
    before = {
        spec: tuple(editor.store.list_nodes(spec, scope=editor.scope))
        for spec in (DOMAIN, METRIC, DIMENSION)
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=editor.app), base_url="http://test"
    ) as client:

        async def read(path, **params):
            with bind_authorization(member):
                response = await client.get(
                    "/api/v1/cm/" + path,
                    params={"datasource_id": data["datasource"], **params},
                )
            assert response.status_code == 200, response.text
            return response.json()

        assert [row["name"] for row in await read("domains")] == ["零售"]
        assert [
            row["dataset_name"] for row in await read("datasets", domain="零售")
        ] == ["orders"]
        dimension = await read("dimensions", domain="零售", name="地区")
        assert dimension["dimension_type"] == "OLAP维度"
        assert dimension["dataset_name"] == "orders"
        assert dimension["sample_values"][0]["value"] == "华东"
        assert dimension["sample_values_total"] == 2
        metrics = await read("search-metrics", domain="零售", query="GMV")
        assert metrics[0]["metric_name"] == "GMV"
        dataset = await read("datasets", domain="零售", name="orders")
        assert dataset["physical_tables"] == ["public.orders"]
        assert any(field["column_name"] == "amount" for field in dataset["columns"])
        assert await read("domains", datasource_id="missing") == []
        with bind_authorization(
            authorization("member", workspace_id="other-workspace")
        ):
            response = await client.get(
                "/api/v1/cm/domains", params={"datasource_id": data["datasource"]}
            )
        assert response.status_code == 403
    assert before == {
        spec: tuple(editor.store.list_nodes(spec, scope=editor.scope))
        for spec in before
    }
    if native:
        assert "Metric query embedding unavailable" not in caplog.text
        assert "Metric vector index unavailable" not in caplog.text
        assert "Metric fulltext query failed" not in caplog.text
