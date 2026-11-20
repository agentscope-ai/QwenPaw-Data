"""Verify prepared Metadata imports preserve scoped snapshot and transaction semantics."""

from copy import copy, deepcopy

import pytest
from context_manager.auth import AccessMode, WriteOwner
from context_manager.metadata import (
    MetadataImportService,
    MetadataSyncService,
    SemanticImportRequest,
)
from context_manager.schema import (
    COLUMN,
    DATA_SOURCE,
    JOINS,
    MAPS_TO_FIELD,
    METRIC,
    TABLE,
)
from context_manager.store import (
    StoreError,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.metadata.support import MemoryGraphStore, UnstackedImports


async def prepare_one(service, request, scope):
    from contextlib import aclosing

    async with aclosing(service._prepare_batches(request, scope)) as batches:
        plan = await service._executor.run(await anext(batches))
        with pytest.raises(StopAsyncIteration):
            await anext(batches)
        return plan


class TransactionStore(MemoryGraphStore):
    """Give callbacks a separate Store and keep writes local until validation succeeds."""

    def __init__(self):
        super().__init__()
        self.transaction_open = False
        self.transaction_bound = False

    def _check_transaction(self, mode):
        if self.transaction_open:
            assert self.transaction_bound, "executor used the outer Store inside atomic"
        if mode is AccessMode.WRITE:
            assert self.transaction_bound, "preparation must not write"

    def _read(self, spec, scope):
        self._check_transaction(AccessMode.READ)
        super()._read(spec, scope)

    def _write(self, spec, scope):
        self._check_transaction(AccessMode.WRITE)
        super()._write(spec, scope)

    def atomic(self, work):
        if self.transaction_bound:
            return work(self)
        assert not self.transaction_open
        transaction = copy(self)
        transaction.nodes = {key: deepcopy(value) for key, value in self.nodes.items()}
        transaction.edges = {key: deepcopy(value) for key, value in self.edges.items()}
        transaction.transaction_open = transaction.transaction_bound = True
        self.transaction_open = True
        try:
            result = work(transaction)
            self.nodes, self.edges = transaction.nodes, transaction.edges
            return result
        finally:
            self.transaction_open = False

    def delete_nodes(self, spec, keys, *, scope):
        if any(
            (edge.source is spec and source == key)
            or (edge.target is spec and target == key)
            for edge, source, target in self.edges
            for key in keys
        ):
            raise StoreError("node still has relationships", operation="delete_nodes")
        return super().delete_nodes(spec, keys, scope=scope)


@pytest.fixture
def graph():
    store = TransactionStore()
    scope = store.access.issue_write_scope(
        authorization(role="tenant_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )
    return (
        store,
        MetadataImportService(
            store,
            StaticModelService(FakeCaller()),
            executor=executor,
            recovery=UnstackedImports(store),
        ),
        scope,
    )


def snapshot(*, description="成交金额", expr="country", full=False):
    return SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "task_id": "sales-import",
            "drop_semantic_first": full,
            "datasource": {
                "datasource_name": "销售数仓",
                "datasource_type": "postgresql",
            },
            "tables": [
                {
                    "name": "orders",
                    "columns": [{"name": "amount", "data_type": "decimal"}],
                },
                {"name": "customers"},
            ],
            "physical_joins": [
                {
                    "left_table": "orders",
                    "right_table": "customers",
                    "left_columns": ["customer_id"],
                    "right_columns": ["id"],
                    "join_type": "left",
                }
            ],
            "semantic": {
                "domains": [
                    {
                        "domain_id": "sales",
                        "name": "销售分析",
                        "datasets": [
                            {
                                "name": "orders",
                                "description": "订单明细",
                                "source": "SELECT * FROM orders",
                            }
                        ],
                        "dimensions": [
                            {
                                "name": "Country",
                                "dimension_bindings": [
                                    {
                                        "dataset": "orders",
                                        "expr": expr,
                                        "value_example": ["全部", "中国"],
                                    }
                                ],
                            }
                        ],
                        "metrics": [
                            {
                                "name": "Revenue",
                                "description": description,
                                "metric_calibers": [
                                    {
                                        "dataset": "orders",
                                        "expr": "SUM(amount)",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    )


def ordered_domains_snapshot(*, revision="初版", full=False):
    return SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "drop_semantic_first": full,
            "semantic": {
                "domains": [
                    {
                        "domain_id": domain,
                        "name": title,
                        "description": revision,
                        "datasets": [
                            {"name": name, "description": revision}
                            for name in ("orders", "customers")
                        ],
                        "dimensions": [
                            {
                                "name": "地区",
                                "description": revision,
                                "dimension_bindings": [
                                    {
                                        "dataset": name,
                                        "expr": "country",
                                        "value_example": [revision],
                                    }
                                    for name in ("orders", "customers")
                                ],
                            },
                            {"name": "渠道", "description": revision},
                        ],
                        "metrics": [
                            {"name": name, "description": revision}
                            for name in ("成交额", "订单量")
                        ],
                    }
                    for domain, title in (
                        ("sales", "销售分析"),
                        ("support", "售后分析"),
                    )
                ]
            },
        }
    )


@pytest.mark.asyncio
async def test_import_update_and_full_replacement_preserve_unrelated_facts(
    graph,
    database_engine,
):
    store, importer, scope = graph
    request = snapshot()
    await importer.import_snapshot(request, scope)
    original_keys = set(store.nodes)
    dimension = request.semantic.domains[0].dimensions[0]
    request.semantic.domains[0].dimensions.append(
        dimension.model_copy(
            update={
                "description": "订单账单所在国家",
                "dimension_bindings": [
                    dimension.dimension_bindings[0].model_copy(
                        update={"expr": "select(country)"}
                    )
                ],
            }
        )
    )
    await importer.import_snapshot(request, scope)
    assert set(store.nodes) == original_keys
    binding = store.list_edges(MAPS_TO_FIELD, scope=scope)[0]
    assert binding.properties["expr"] == "select(country)"
    assert (
        store.get_nodes(binding.spec.source, (binding.source_key,), scope=scope)[
            0
        ].properties["description"]
        == "订单账单所在国家"
    )
    other_scope = store.access.issue_write_scope(
        authorization(user_id="other"),
        owner=WriteOwner.USER,
    )
    MetadataSyncService(store, database_engine=database_engine).sync(other_scope)
    other_datasource = store.access.issue_write_scope(
        authorization(role="tenant_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="archive",
        zone="metadata",
    )
    await importer.import_snapshot(
        SemanticImportRequest.model_validate(
            {
                "datasource_id": "archive",
                "semantic": {"domains": [{"name": "Archived"}]},
            }
        ),
        other_datasource,
    )
    protected = {
        identity: deepcopy(values)
        for identity, values in store.nodes.items()
        if identity[0] in {DATA_SOURCE, TABLE, COLUMN}
        or values["owner_id"] == "other"
        or values["datasource_id"] == "archive"
    }
    request = SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "drop_semantic_first": True,
            "semantic": {
                "domains": [{"name": "New sales", "metrics": [{"name": "Net revenue"}]}]
            },
        }
    )
    await importer.import_snapshot(request, scope)
    assert (
        store.get_nodes(METRIC, ("met:__global__:sales:Revenue",), scope=scope)[0]
        is None
    )
    assert (
        store.get_nodes(METRIC, ("met:__global__:New sales:Net revenue",), scope=scope)[
            0
        ]
        is not None
    )
    assert all(
        store.nodes[identity] == values for identity, values in protected.items()
    )
    assert store.list_edges(JOINS, scope=scope) == ()
