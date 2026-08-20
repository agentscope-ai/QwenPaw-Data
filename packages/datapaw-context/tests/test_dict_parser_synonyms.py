"""Regression test for the `aliases`/`synonyms` field mismatch in dict_parser.

`SemanticImportRequest`'s `DimensionPayload`/`MetricPayload` (see
`context_manager/contracts/import_models.py`) only define an `aliases`
field; there is no `synonyms` field on the wire. `dict_parser._convert_*`
must read `aliases` (falling back to `synonyms` for the legacy YAML import
path) — reading only `synonyms` silently drops every alias, since the key
never exists on payloads produced by `weave_assembler`.
"""
from context_manager.graph.dict_parser import _convert_dimensions, _convert_metrics


def test_convert_dimensions_reads_aliases_key() -> None:
    dims_in = [{"name": "瘦焦煤", "aliases": ["瘦煤", "Lean Coking Coal"]}]

    out = _convert_dimensions(dims_in, dataset_lookup={})

    assert out[0]["synonyms"] == ["瘦煤", "Lean Coking Coal"]


def test_convert_dimensions_falls_back_to_legacy_synonyms_key() -> None:
    dims_in = [{"name": "瘦焦煤", "synonyms": ["瘦煤"]}]

    out = _convert_dimensions(dims_in, dataset_lookup={})

    assert out[0]["synonyms"] == ["瘦煤"]


def test_convert_metrics_reads_aliases_key() -> None:
    metrics_in = [{"name": "出厂价格", "aliases": ["出厂价"]}]

    out = _convert_metrics(metrics_in, dataset_lookup={})

    assert out[0]["synonyms"] == ["出厂价"]


def test_convert_metrics_falls_back_to_legacy_synonyms_key() -> None:
    metrics_in = [{"name": "出厂价格", "synonyms": ["出厂价"]}]

    out = _convert_metrics(metrics_in, dataset_lookup={})

    assert out[0]["synonyms"] == ["出厂价"]
