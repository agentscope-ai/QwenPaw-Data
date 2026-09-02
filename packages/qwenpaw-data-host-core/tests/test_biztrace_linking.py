# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

import httpx

from qwenpaw_data.host.core.algo.biztrace.linking import EntityLinker, build_entity_url
from qwenpaw_data.host.core.algo.biztrace.semantic_vocab import (
    BIZ_DOMAIN_PATH,
    DATASET_PATH,
    DIMENSION_PATH,
    METRIC_PATH,
    SemanticVocabulary,
    VocabEntry,
    split_terms,
)

BASE_URL = "http://cm.test"


def _linker(terms: dict[str, VocabEntry]) -> EntityLinker:
    return EntityLinker(terms=terms, base_url=BASE_URL, datasource_id="ds-1")


def _vocabulary(
    records: dict[str, list[dict[str, Any]]],
    *,
    seen: list[httpx.Request] | None = None,
) -> SemanticVocabulary:
    """Serve the four endpoints from a canned mapping of path to records."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        page = int(request.url.params.get("page", 1))
        rows = records.get(request.url.path, [])
        return httpx.Response(
            200,
            json={
                "records": rows if page == 1 else [],
                "total": len(rows),
                "page": page,
                "size": 200,
            },
        )

    return SemanticVocabulary(
        base_url=BASE_URL,
        datasource_id="ds-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_each_entity_type_has_its_own_url() -> None:
    urls = {
        entry.entity_type: build_entity_url(
            entry, base_url=BASE_URL, datasource_id="ds-1"
        )
        for entry in (
            VocabEntry(entity_type="biz_domain", domain_id="d1", entity_id=None),
            VocabEntry(entity_type="dimension", domain_id="d1", entity_id="dim1"),
            VocabEntry(entity_type="metric", domain_id="d1", entity_id="m1"),
            VocabEntry(entity_type="dataset", domain_id="d1", entity_id="ds9"),
        )
    }

    assert urls["biz_domain"] == (
        f"{BASE_URL}/business-domain?datasource_id=ds-1&domain_id=d1"
    )
    assert urls["dimension"] == (
        f"{BASE_URL}/dimension?datasource_id=ds-1&domain_id=d1&dimension_id=dim1"
    )
    assert urls["metric"] == (
        f"{BASE_URL}/metric-lib?datasource_id=ds-1&domain_id=d1&metric_id=m1"
    )
    assert urls["dataset"] == (
        f"{BASE_URL}/data-set?datasource_id=ds-1&domain_id=d1&dataset_id=ds9"
    )


def test_every_occurrence_of_a_term_is_linked() -> None:
    linker = _linker(
        {"日活": VocabEntry(entity_type="metric", domain_id=None, entity_id="m1")}
    )

    body = linker.link("日活下降，日活需要复核")

    assert body.count("](") == 2
    assert body.startswith("[日活](http://cm.test/metric-lib?")
    assert "，[日活](http://cm.test/metric-lib?" in body


def test_the_longest_matching_term_wins() -> None:
    metric = VocabEntry(entity_type="metric", domain_id=None, entity_id="m1")
    dimension = VocabEntry(entity_type="dimension", domain_id=None, entity_id="d1")
    linker = _linker({"日活": metric, "日活用户数": dimension})

    body = linker.link("日活用户数偏低")

    assert body.startswith("[日活用户数](http://cm.test/dimension?")


def test_protected_regions_are_left_alone() -> None:
    linker = _linker(
        {"日活": VocabEntry(entity_type="metric", domain_id=None, entity_id="m1")}
    )

    assert linker.link("[日活](http://old)") == "[日活](http://old)"
    assert linker.link("```\n日活\n```") == "```\n日活\n```"
    assert linker.link("http://x/日活") == "http://x/日活"
    # Inline code that is not an exact vocab term stays untouched.
    assert linker.link("`日活下降`") == "`日活下降`"


def test_terms_in_inline_code_or_bold_are_linked() -> None:
    linker = _linker(
        {
            "日活": VocabEntry(entity_type="metric", domain_id=None, entity_id="m1"),
            "成交额": VocabEntry(entity_type="metric", domain_id=None, entity_id="m2"),
            "订单表": VocabEntry(entity_type="dataset", domain_id=None, entity_id="ds9"),
        }
    )

    assert linker.link("`日活` 下降").startswith("[日活](http://cm.test/metric-lib?")
    assert "`" not in linker.link("`日活`")
    assert linker.link("关注 **成交额**").startswith("关注 [成交额](http://cm.test/metric-lib?")
    assert "**" not in linker.link("**成交额**")
    assert linker.link("写入 __订单表__").startswith("写入 [订单表](http://cm.test/data-set?")
    # Marked and plain mentions are both linked.
    assert linker.link("`日活` 与 日活").count("](") == 2


def test_a_term_inside_an_html_tag_is_not_linked() -> None:
    linker = _linker(
        {
            "日活": VocabEntry(entity_type="metric", domain_id=None, entity_id="m1"),
            "text-red-500": VocabEntry(
                entity_type="metric", domain_id=None, entity_id="m2"
            ),
        }
    )

    body = linker.link('日活 <span class="text-red-500 font-bold">-3.2%</span>')

    # The colour span survives verbatim; only the prose around it is linked.
    assert '<span class="text-red-500 font-bold">-3.2%</span>' in body
    assert body.startswith("[日活](http://cm.test/metric-lib?")


def test_an_ascii_term_inside_a_longer_identifier_is_not_linked() -> None:
    linker = _linker(
        {"dau": VocabEntry(entity_type="metric", domain_id=None, entity_id="m1")}
    )

    assert linker.link("dau_7d 指标") == "dau_7d 指标"
    assert linker.link("dau 指标").startswith("[dau](")


def test_an_empty_vocabulary_leaves_the_body_untouched() -> None:
    linker = _linker({})

    assert not linker
    assert linker.link("日活下降") == "日活下降"


def test_synonyms_are_split_the_way_the_graph_loader_splits_them() -> None:
    assert split_terms("日活, DAU|活跃用户") == ["日活", "DAU", "活跃用户"]
    assert split_terms("日活") == ["日活"]
    assert split_terms(None) == []


async def test_the_vocabulary_indexes_domain_layer_without_datasource() -> None:
    """v2.1: domain / metric / dimension have no datasource_id; only datasets do."""
    seen: list[httpx.Request] = []
    vocabulary = _vocabulary(
        {
            BIZ_DOMAIN_PATH: [{"domain_name": "电商", "domain_id": "d1"}],
            METRIC_PATH: [
                {
                    "metric_name": "日活",
                    "synonyms": "DAU,活跃用户",
                    "metric_id": "m1",
                    "domain_id": "d1",
                },
                {"metric_name": "越界指标", "metric_id": "m2", "domain_id": "d2"},
            ],
            DIMENSION_PATH: [
                {
                    "dimension_name": "城市",
                    "dimension_id": "dim1",
                    "domain_id": "d1",
                }
            ],
            DATASET_PATH: [
                {"dataset_name": "订单表", "dataset_id": "t1", "datasource_id": "ds-1"},
                {
                    "dataset_name": "他源表",
                    "dataset_id": "t2",
                    "datasource_id": "ds-other",
                },
            ],
        },
        seen=seen,
    )

    await vocabulary.ensure_fresh()

    assert set(vocabulary.terms) == {
        "电商",
        "日活",
        "DAU",
        "活跃用户",
        "越界指标",
        "城市",
        "订单表",
    }
    assert "他源表" not in vocabulary.terms
    assert vocabulary.terms["DAU"].entity_type == "metric"
    assert vocabulary.terms["DAU"].entity_id == "m1"
    assert vocabulary.terms["城市"].entity_id == "dim1"

    by_path = {request.url.path: request.url.params for request in seen}
    assert "datasource_id" not in by_path[BIZ_DOMAIN_PATH]
    assert "datasource_id" not in by_path[METRIC_PATH]
    assert "datasource_id" not in by_path[DIMENSION_PATH]
    assert by_path[DATASET_PATH].get("datasource_id") == "ds-1"
    await vocabulary.aclose()


async def test_a_term_claimed_by_two_types_is_dropped() -> None:
    vocabulary = _vocabulary(
        {
            METRIC_PATH: [
                {"metric_name": "城市", "metric_id": "m1", "domain_id": "d1"}
            ],
            DIMENSION_PATH: [
                {
                    "dimension_name": "城市",
                    "dimension_id": "dim1",
                    "domain_id": "d1",
                }
            ],
        }
    )

    await vocabulary.ensure_fresh()

    assert "城市" not in vocabulary.terms
    await vocabulary.aclose()


async def test_an_unreachable_endpoint_degrades_to_no_links() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(503)

    vocabulary = SemanticVocabulary(
        base_url=BASE_URL,
        datasource_id="ds-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await vocabulary.ensure_fresh()

    assert vocabulary.terms == {}
    await vocabulary.aclose()


async def test_vocabulary_sends_bearer_token_when_configured() -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(
            200,
            json={"records": [], "total": 0, "page": 1, "size": 200},
        )

    vocabulary = SemanticVocabulary(
        base_url=BASE_URL,
        datasource_id="ds-1",
        access_token="tok-user-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await vocabulary.ensure_fresh()

    assert seen
    assert all(header == "Bearer tok-user-1" for header in seen)
    await vocabulary.aclose()


async def test_vocabulary_omits_authorization_without_a_token() -> None:
    seen: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append("Authorization" in request.headers)
        return httpx.Response(
            200,
            json={"records": [], "total": 0, "page": 1, "size": 200},
        )

    vocabulary = SemanticVocabulary(
        base_url=BASE_URL,
        datasource_id="ds-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await vocabulary.ensure_fresh()

    assert seen
    assert all(not has_auth for has_auth in seen)
    await vocabulary.aclose()


def test_same_origin_base_url_builds_relative_href() -> None:
    entry = VocabEntry(entity_type="metric", domain_id="d1", entity_id="m1")
    assert build_entity_url(entry, base_url="", datasource_id="ds-1") == (
        "/metric-lib?datasource_id=ds-1&domain_id=d1&metric_id=m1"
    )


def test_resolve_link_base_url_prefers_context_frontend(monkeypatch) -> None:
    from qwenpaw_data.host.core.algo.biztrace.settings import (
        DEFAULT_CM_FRONTEND_URL,
        BizTraceSettings,
        resolve_link_base_url,
    )

    settings = BizTraceSettings(biz_link_base_url=None)
    monkeypatch.setenv("QWENPAW_DATA_CM_FRONTEND_URL", "https://context.example/")
    assert resolve_link_base_url(settings) == "https://context.example"

    monkeypatch.setenv("QWENPAW_DATA_CM_FRONTEND_URL", "/")
    assert resolve_link_base_url(settings) == ""

    monkeypatch.delenv("QWENPAW_DATA_CM_FRONTEND_URL", raising=False)
    assert resolve_link_base_url(settings) == DEFAULT_CM_FRONTEND_URL
    assert resolve_link_base_url(settings) == "http://localhost:3000"

    override = BizTraceSettings(biz_link_base_url="https://override.example/")
    assert resolve_link_base_url(override) == "https://override.example"


async def test_build_linker_scopes_cache_by_datasource_and_refreshes_token(
    monkeypatch,
) -> None:
    from qwenpaw_data.host.core.algo.biztrace import linking as linking_module
    from qwenpaw_data.host.core.algo.biztrace.settings import BizTraceSettings

    await linking_module.shutdown_vocabularies()
    monkeypatch.setattr(
        linking_module, "resolve_link_base_url", lambda _settings: "http://frontend.test"
    )
    monkeypatch.setattr(
        linking_module, "resolve_cm_base_url", lambda: BASE_URL
    )
    settings = BizTraceSettings(biz_link_enabled=True, biz_link_ttl=300.0)

    first = linking_module.build_linker(
        settings,
        datasource_id="ds-1",
        access_token="tok-a",
    )
    second = linking_module.build_linker(
        settings,
        datasource_id="ds-1",
        access_token="tok-b",
    )
    other = linking_module.build_linker(
        settings,
        datasource_id="ds-2",
        access_token="tok-c",
    )

    assert first is not None and second is not None and other is not None
    assert first.base_url == "http://frontend.test"
    assert second.base_url == "http://frontend.test"
    shared = linking_module._VOCABULARIES[(BASE_URL, "ds-1")]
    isolated = linking_module._VOCABULARIES[(BASE_URL, "ds-2")]
    assert shared is not isolated
    assert shared.base_url == BASE_URL
    assert shared.access_token == "tok-b"
    assert isolated.access_token == "tok-c"
    await linking_module.shutdown_vocabularies()
