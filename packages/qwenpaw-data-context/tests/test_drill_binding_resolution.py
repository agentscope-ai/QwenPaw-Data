from copy import deepcopy
from types import SimpleNamespace

import pytest

from context_manager.api import retrieval
from context_manager.api.semantic_pack import (
    _build_drill_dimensions,
    render_schema_prompt,
)


def formula(dataset="sales", table="orders"):
    return {
        "dataset": dataset,
        "dataset_key": f"ds:{dataset}",
        "f": {"key": "f", "dataset": dataset},
        "t": {"key": f"t:{table}", "schema": "public", "name": table},
    }


def binding(
    dataset="sales", table="orders", logical="region_alias", physical="region_code"
):
    return {
        "dim": {"key": "dim:region", "name": "地区"},
        "dataset": dataset,
        "dataset_key": f"ds:{dataset}",
        "col": {"key": f"dscol:{dataset}.{logical}", "name": logical},
        "phys_col": {
            "key": f"col:{table}.{physical}",
            "schema": "public",
            "table": table,
            "name": physical,
        },
        "expr": f"select({logical})",
        "filter": "active = true",
    }


def resolve(rows, formulas=None):
    return retrieval._resolve_drill_dimensions(rows, formulas or [formula()])


def test_alias_uses_both_identifiers_from_physical_column():
    dim = resolve([binding()])[0]
    assert dim["column"] == "region_code"
    assert dim["column_table"] == "public.orders"
    assert dim["bindings"][0]["dataset_column"] == "region_alias"
    assert dim["bindings"][0]["filter"] == "active = true"


def test_unrelated_dataset_cannot_win_by_row_order():
    rows = [binding("inventory", "stocks"), binding()]
    assert resolve(rows) == resolve(list(reversed(rows)))
    dim = resolve(rows)[0]
    assert len(dim["bindings"]) == 1
    assert dim["bindings"][0]["dataset"] == "sales"


def test_multiple_relevant_bindings_preserved_deduplicated_and_rendered():
    rows = [binding("inventory", "stocks"), binding(), binding()]
    formulas = [formula(), formula("inventory", "stocks")]
    dims = resolve(rows, formulas)
    assert dims == resolve(list(reversed(rows)), formulas)
    assert len(dims[0]["bindings"]) == 2
    assert dims[0]["column"] == dims[0]["column_table"] == ""
    focus = SimpleNamespace(
        metric_name="m",
        description="",
        caliber="",
        aliases=[],
        unit="",
        source_columns=[],
        common_filters=[],
        table_joins=[],
        drill_dimensions=_build_drill_dimensions({"raw": {"drill_dims": dims}}, 8),
    )
    prompt = render_schema_prompt([focus], [])
    assert "public.orders.region_code" in prompt
    assert "public.stocks.region_code" in prompt
    assert "public.orders.region_alias" not in prompt
    assert "绑定表达式 select(region_alias)" in prompt
    assert "绑定过滤 active = true" in prompt


@pytest.mark.parametrize("kind", ["missing", "multiple", "composite"])
def test_unresolved_or_composite_lineage_is_not_a_direct_column(kind):
    row = binding()
    rows = [row]
    if kind == "missing":
        row["phys_col"] = None
    elif kind == "composite":
        row["col"]["composite"] = True
    else:
        other = deepcopy(row)
        other["phys_col"]["name"] = "other_input"
        rows.append(other)
    dim = resolve(rows)[0]
    assert dim["column"] == dim["column_table"] == ""
    assert dim["bindings"][0]["dataset_column"] == "region_alias"


def test_direct_column_and_same_named_table_in_other_schema():
    row = binding()
    row["col"] = row.pop("phys_col")
    row.pop("dataset")
    row.pop("dataset_key")
    assert resolve([row])[0]["column"] == "region_code"
    row["col"]["schema"] = "other"
    assert resolve([row])[0]["bindings"] == []


def test_graph_expansion_passes_scoped_bindings_to_schema_pack(monkeypatch):
    from contextlib import nullcontext

    record = {
        "metric": {"key": "m", "name": "sales"},
        "domain": None,
        "formula_rows": [formula()],
        "drill_rows": [binding("inventory", "stocks"), binding()],
        "derived_rows": [],
        "caliber_rows": [],
    }
    calls = []

    def run(query, **params):
        calls.append(query)
        return SimpleNamespace(single=lambda: record, data=lambda: [])

    monkeypatch.setattr(
        retrieval, "neo4j_session", lambda _: nullcontext(SimpleNamespace(run=run))
    )
    expanded = retrieval.expand_subgraph(
        object(),
        "m",
        include_anomaly=False,
        include_calibers=False,
        include_derived=False,
        include_cross_graph=False,
    )
    dims = _build_drill_dimensions(expanded, 8)
    assert dims[0].column == "region_code"
    assert len(dims[0].bindings) == 1
    assert "dataset_key: formula_ds.key" in calls[0]
    assert "dataset_key: dim_ds.key" in calls[0]
