"""CALIBER 实体复用、本源关系锁定与确定性收口。"""

from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest
from profiling.algo.evidence import (
    CaliberEvidence,
    SemanticCatalogEvidence,
    TableEvidence,
    TaskEvidence,
)
from profiling.algo.handlers.caliber import (
    CaliberHandler,
    _aliases,
    _filter_locked_and_dedupe,
    _SemanticCatalog,
)
from profiling.algo.handlers.prompts import build_column_role_system_prompt
from profiling.algo.handlers.runner import LlmRunner
from profiling.algo.handlers.validators import validate_name_normalization_items
from profiling.algo.snapshots import TableSnapshot
from profiling.algo.tasks import ProfileTask


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ()),
        ("", ()),
        ("[]", ()),
        ("foo$$$bar$$$foo", ("foo", "bar")),
        ("daily active users", ("daily active users",)),
        (["foo", None, " bar ", "foo"], ("foo", "bar")),
        (pd.Series(["foo", "bar"]).to_numpy(), ("foo", "bar")),
        ('["foo", "bar"]', ("foo", "bar")),
        ("['foo', 'bar']", ("foo", "bar")),
        (["[foo]", "bar [USD]"], ("[foo]", "bar [USD]")),
        ('["[foo]", "bar [USD]", "foo$$$bar"]', ("[foo]", "bar [USD]", "foo$$$bar")),
    ],
)
def test_catalog_aliases_preserve_supported_values(value, expected):
    assert _aliases(value) == expected


@pytest.mark.parametrize(
    "kind,name_key,frame_key",
    [
        ("dimension", "dimension_name", "dimensions"),
        ("metric", "metric_name", "metrics"),
    ],
)
@pytest.mark.parametrize("value", ["[foo$$$bar]", "[foo, bar]", "['foo', bar]"])
def test_malformed_alias_list_does_not_pollute_catalog(
    kind, name_key, frame_key, value, caplog
):
    frames = {"dimensions": pd.DataFrame(), "metrics": pd.DataFrame()}
    frames[frame_key] = pd.DataFrame(
        [
            {
                "domain_name": "sales",
                name_key: "Canonical [USD]",
                "synonyms": value,
            }
        ]
    )
    catalog = _SemanticCatalog.from_evidence(SemanticCatalogEvidence(**frames))

    assert catalog.match(kind, "sales", "Canonical [USD]") == "Canonical [USD]"
    for name in ("foo", "bar", "[foo", "bar]", "'bar']", "['foo'"):
        assert catalog.match(kind, "sales", name) is None
    assert catalog.prompt_records(kind, "sales")[0]["synonyms"] == []
    assert "Invalid synonyms list; aliases ignored" in caplog.text


class _FakeLlm:
    def __init__(
        self,
        roles: list[dict],
        *,
        matches: dict[str, str] | None = None,
        fail_matching: bool = False,
    ) -> None:
        self._roles = roles
        self._matches = matches or {}
        self._fail_matching = fail_matching
        self.match_payloads: list[dict] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str:
        if "reusable_entities" not in user_prompt:
            return json.dumps(self._roles, ensure_ascii=False)
        payload = json.loads(user_prompt.rsplit("\n\n", 1)[-1])
        self.match_payloads.append(payload)
        if self._fail_matching:
            raise RuntimeError("semantic matcher unavailable")
        return json.dumps(
            [
                {
                    "name": candidate["name"],
                    "canonical_name": self._matches.get(
                        candidate["name"], candidate["name"]
                    ),
                }
                for candidate in payload["candidates"]
            ],
            ensure_ascii=False,
        )


def _datasets(columns: list[dict]) -> TableEvidence:
    table_name = "public.orders"
    return TableEvidence(
        TableSnapshot.build(
            pd.DataFrame(
                [
                    {
                        "table_name": table_name,
                        "table_comment": "订单明细",
                        "domain_name": "sales",
                    }
                ]
            ),
            {
                table_name: pd.DataFrame(
                    [
                        {
                            "table_name": table_name,
                            "column_name": column["column_name"],
                            "data_type": column.get("data_type", "text"),
                            "is_primary": "N",
                            "is_nullable": "Y",
                            "column_comment": column.get("column_comment"),
                            "column_enums": column.get("column_enums", []),
                        }
                        for column in columns
                    ]
                )
            },
        )
    )


def _datasets_in_domains(
    table_domains: dict[str, str], columns: list[dict]
) -> TableEvidence:
    return TableEvidence(
        TableSnapshot.build(
            pd.DataFrame(
                [
                    {
                        "table_name": table_name,
                        "table_comment": f"{domain_name} orders",
                        "domain_name": domain_name,
                    }
                    for table_name, domain_name in table_domains.items()
                ]
            ),
            {
                table_name: pd.DataFrame(
                    [
                        {
                            "table_name": table_name,
                            "column_name": column["column_name"],
                            "data_type": column.get("data_type", "text"),
                            "is_primary": "N",
                            "is_nullable": "Y",
                            "column_comment": column.get("column_comment"),
                            "column_enums": column.get("column_enums", []),
                        }
                        for column in columns
                    ]
                )
                for table_name in table_domains
            },
        )
    )


def _catalog() -> SemanticCatalogEvidence:
    return SemanticCatalogEvidence(
        dimensions=pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "dimension_name": "City",
                    "description": "客户所在城市",
                    "synonyms": ["城市"],
                    "dimension_role": "categorical",
                    # 即使上游误带关系字段，handler 也必须二次白名单投影。
                    "tables": ["another_source.customers"],
                    "value_example": ["杭州"],
                },
                {
                    "domain_name": "sales",
                    "dimension_name": "Country",
                    "description": "客户所在国家",
                    "synonyms": ["Nation"],
                    "dimension_role": "categorical",
                },
            ]
        ),
        metrics=pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "metric_name": "Revenue",
                    "description": "订单成交金额",
                    "synonyms": ["GMV"],
                    "formulas": ["SUM(secret_amount)"],
                    "time_windows": ["30d"],
                    "tables": ["another_source.orders"],
                }
            ]
        ),
    )


def _evidence(
    columns: list[dict],
    *,
    catalog: SemanticCatalogEvidence | None = None,
    prior_staged: CaliberEvidence | None = None,
) -> TaskEvidence:
    return TaskEvidence(
        task=ProfileTask.CALIBER,
        datasets=_datasets(columns),
        semantic_catalog=catalog,
        prior_staged=prior_staged,
    )


def test_caliber_matches_catalog_and_only_rewrites_semantic_names() -> None:
    roles = [
        {
            "column_name": "city_code",
            "column_role": "dimension",
            "dimension_name": "city",
            "metric_name": None,
            "time_window": None,
            "additivity": None,
            "expr": None,
            "reason": "城市维度",
            "confidence": 0.9,
        },
        {
            "column_name": "country_code",
            "column_role": "dimension",
            "dimension_name": "nation",
            "metric_name": None,
            "time_window": None,
            "additivity": None,
            "expr": None,
            "reason": "国家维度",
            "confidence": 0.9,
        },
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "dimension_name": None,
            "metric_name": "成交金额",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        },
    ]
    fake = _FakeLlm(roles, matches={"成交金额": "Revenue"})
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {"column_name": "city_code", "column_comment": "城市"},
                    {"column_name": "country_code", "column_comment": "国家"},
                    {
                        "column_name": "gross_amount",
                        "data_type": "decimal",
                        "column_comment": "成交金额",
                    },
                ],
                catalog=_catalog(),
            )
        )
    )

    rows = snapshot.calibers.set_index("column_name")
    # canonical name 分别命中唯一 casefold、唯一 synonym 与 LLM 语义匹配。
    assert rows.loc["city_code", "dimension_name"] == "City"
    assert rows.loc["country_code", "dimension_name"] == "Country"
    assert rows.loc["gross_amount", "metric_name"] == "Revenue"
    # 只改名称，当前源的口径属性不变。
    assert rows.loc["gross_amount", "expr"] == "SUM(gross_amount)"
    assert rows.loc["gross_amount", "time_window"] == "1d"
    assert rows.loc["gross_amount", "additivity"] == "additive"

    assert len(fake.match_payloads) == 1
    reusable = fake.match_payloads[0]["reusable_entities"]
    assert reusable == [
        {
            "name": "Revenue",
            "description": "订单成交金额",
            "synonyms": ["GMV"],
        }
    ]
    forbidden = {"tables", "formulas", "time_windows", "value_example"}
    assert not any(forbidden & set(item) for item in reusable)


def test_exact_catalog_names_do_not_enter_semantic_matcher_payload() -> None:
    roles = [
        {
            "column_name": "city_code",
            "column_role": "dimension",
            "dimension_name": "City",
            "reason": "城市维度",
            "confidence": 0.9,
        },
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "Revenue",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        },
    ]
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {"column_name": "city_code", "column_comment": "城市"},
                    {
                        "column_name": "gross_amount",
                        "data_type": "decimal",
                        "column_comment": "成交金额",
                    },
                ],
                catalog=_catalog(),
            )
        )
    )

    assert snapshot.calibers["dimension_name"].dropna().tolist() == ["City"]
    assert snapshot.calibers["metric_name"].dropna().tolist() == ["Revenue"]
    assert fake.match_payloads == []


def test_catalog_and_matcher_are_partitioned_by_domain_and_entity_kind() -> None:
    roles = [
        {
            "column_name": "shared_dim",
            "column_role": "dimension",
            "dimension_name": "SharedAlias",
            "reason": "共享别名维度",
            "confidence": 0.9,
        },
        {
            "column_name": "fresh_dim",
            "column_role": "dimension",
            "dimension_name": "FreshDimension",
            "reason": "新维度",
            "confidence": 0.9,
        },
        {
            "column_name": "shared_metric",
            "column_role": "metric",
            "metric_name": "SharedAlias",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(shared_metric)",
            "reason": "共享别名指标",
            "confidence": 0.9,
        },
        {
            "column_name": "fresh_metric",
            "column_role": "metric",
            "metric_name": "FreshMetric",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(fresh_metric)",
            "reason": "新指标",
            "confidence": 0.9,
        },
    ]
    catalog = SemanticCatalogEvidence(
        dimensions=pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "dimension_name": "SalesDimension",
                    "description": "销售域维度",
                    "synonyms": ["SharedAlias"],
                    "dimension_role": "categorical",
                },
                {
                    "domain_name": "support",
                    "dimension_name": "SupportDimension",
                    "description": "客服域维度",
                    "synonyms": ["SharedAlias"],
                    "dimension_role": "categorical",
                },
            ]
        ),
        metrics=pd.DataFrame(
            [
                {
                    "domain_name": "sales",
                    "metric_name": "SalesMetric",
                    "description": "销售域指标",
                    "synonyms": ["SharedAlias"],
                },
                {
                    "domain_name": "support",
                    "metric_name": "SupportMetric",
                    "description": "客服域指标",
                    "synonyms": ["SharedAlias"],
                },
            ]
        ),
    )
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))
    evidence = TaskEvidence(
        task=ProfileTask.CALIBER,
        datasets=_datasets_in_domains(
            {"sales.orders": "sales", "support.orders": "support"},
            [
                {"column_name": "shared_dim", "column_comment": "共享维度"},
                {"column_name": "fresh_dim", "column_comment": "新维度"},
                {
                    "column_name": "shared_metric",
                    "data_type": "decimal",
                    "column_comment": "共享指标",
                },
                {
                    "column_name": "fresh_metric",
                    "data_type": "decimal",
                    "column_comment": "新指标",
                },
            ],
        ),
        semantic_catalog=catalog,
    )

    snapshot = asyncio.run(handler.handle(evidence))
    rows = snapshot.calibers.set_index(["table_name", "column_name"])
    assert rows.loc[("sales.orders", "shared_dim"), "dimension_name"] == (
        "SalesDimension"
    )
    assert rows.loc[("support.orders", "shared_dim"), "dimension_name"] == (
        "SupportDimension"
    )
    assert rows.loc[("sales.orders", "shared_metric"), "metric_name"] == ("SalesMetric")
    assert rows.loc[("support.orders", "shared_metric"), "metric_name"] == (
        "SupportMetric"
    )

    payloads = {
        (payload["domain_name"], payload["entity_kind"]): payload
        for payload in fake.match_payloads
    }
    assert set(payloads) == {
        ("sales", "维度"),
        ("support", "维度"),
        ("sales", "指标"),
        ("support", "指标"),
    }
    expected_catalog_name = {
        ("sales", "维度"): "SalesDimension",
        ("support", "维度"): "SupportDimension",
        ("sales", "指标"): "SalesMetric",
        ("support", "指标"): "SupportMetric",
    }
    for scope, payload in payloads.items():
        assert [item["name"] for item in payload["reusable_entities"]] == [
            expected_catalog_name[scope]
        ]
        assert [item["name"] for item in payload["candidates"]] == [
            "FreshDimension" if scope[1] == "维度" else "FreshMetric"
        ]


def test_confirmed_current_source_relation_locks_but_pending_does_not() -> None:
    roles = [
        {
            "column_name": "city_code",
            "column_role": "dimension",
            "dimension_name": "City",
            "reason": "城市维度",
            "confidence": 0.9,
        },
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "Revenue",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        },
    ]
    prior = CaliberEvidence(
        calibers=pd.DataFrame(
            [
                {
                    "table_name": "public.orders",
                    "column_name": "city_code",
                    "domain_name": "sales",
                    "column_role": "dimension",
                    "dimension_name": "City",
                    "metric_name": None,
                    "time_window": None,
                    "probe_status": "CONFIRMED",
                },
                {
                    "table_name": "public.orders",
                    "column_name": "gross_amount",
                    "domain_name": "sales",
                    "column_role": "metric",
                    "dimension_name": None,
                    "metric_name": "Revenue",
                    "time_window": "1d",
                    "probe_status": "PENDING",
                },
                {
                    "table_name": "public.orders",
                    "column_name": "gross_amount",
                    "domain_name": "sales",
                    "column_role": "metric",
                    "dimension_name": None,
                    "metric_name": "Revenue",
                    "time_window": "7d",
                    "probe_status": "CONFIRMED",
                },
            ]
        )
    )
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))
    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {"column_name": "city_code", "column_comment": "城市"},
                    {
                        "column_name": "gross_amount",
                        "data_type": "decimal",
                        "column_comment": "成交金额",
                    },
                ],
                catalog=_catalog(),
                prior_staged=prior,
            )
        )
    )

    assert snapshot.calibers["column_name"].tolist() == ["gross_amount"]
    assert snapshot.calibers.iloc[0]["time_window"] == "1d"


def test_skipped_current_source_relation_does_not_lock() -> None:
    roles = [
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "Revenue",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        }
    ]
    prior = CaliberEvidence(
        calibers=pd.DataFrame(
            [
                {
                    "table_name": "public.orders",
                    "column_name": "gross_amount",
                    "domain_name": "sales",
                    "column_role": "metric",
                    "metric_name": "Revenue",
                    "time_window": "1d",
                    "probe_status": "SKIPPED",
                }
            ]
        )
    )
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {
                        "column_name": "gross_amount",
                        "data_type": "decimal",
                        "column_comment": "成交金额",
                    }
                ],
                catalog=_catalog(),
                prior_staged=prior,
            )
        )
    )

    assert snapshot.calibers["metric_name"].tolist() == ["Revenue"]


def test_create_and_refine_share_canonicalize_and_lock_pipeline() -> None:
    roles = [
        {
            "column_name": "city_code",
            "column_role": "dimension",
            "dimension_name": "City",
            "reason": "城市维度",
            "confidence": 0.9,
        },
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "成交额",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        },
    ]
    prior = CaliberEvidence(
        calibers=pd.DataFrame(
            [
                {
                    "table_name": "public.orders",
                    "column_name": "old_amount",
                    "domain_name": "sales",
                    "column_role": "metric",
                    "metric_name": "Revenue",
                    "time_window": "1d",
                    "probe_status": "CONFIRMED",
                }
            ]
        )
    )
    results: dict[str, pd.DataFrame] = {}
    match_payloads: dict[str, list[dict]] = {}
    for mode in ("create", "refine"):
        fake = _FakeLlm(roles, matches={"成交额": "Revenue"})
        handler = CaliberHandler(
            fake,
            runner=LlmRunner(fake, concurrency=1),
            mode=mode,
        )
        snapshot = asyncio.run(
            handler.handle(
                _evidence(
                    [
                        {"column_name": "city_code", "column_comment": "城市"},
                        {
                            "column_name": "gross_amount",
                            "data_type": "decimal",
                            "column_comment": "成交金额",
                        },
                    ],
                    catalog=_catalog(),
                    prior_staged=prior,
                )
            )
        )
        results[mode] = snapshot.calibers.reset_index(drop=True)
        match_payloads[mode] = fake.match_payloads

    pd.testing.assert_frame_equal(results["create"], results["refine"])
    assert results["create"]["column_name"].tolist() == ["city_code"]
    assert results["create"]["dimension_name"].tolist() == ["City"]
    # 成交额先映射到 Revenue，再被同 dataset/window 的已确认关系锁定剥离。
    assert len(match_payloads["create"]) == 1
    assert len(match_payloads["refine"]) == 1


def test_semantic_match_failure_keeps_provisional_name() -> None:
    roles = [
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "本源成交额",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "金额指标",
            "confidence": 0.9,
        }
    ]
    fake = _FakeLlm(roles, fail_matching=True)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))
    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {
                        "column_name": "gross_amount",
                        "data_type": "decimal",
                        "column_comment": "成交金额",
                    }
                ],
                catalog=_catalog(),
            )
        )
    )

    assert snapshot.calibers.iloc[0]["metric_name"] == "本源成交额"
    assert snapshot.calibers.iloc[0]["expr"] == "SUM(gross_amount)"


def test_unpersisted_confirmed_name_is_pinned_and_locks_rerun() -> None:
    roles = [
        {
            "column_name": "city_code",
            "column_role": "dimension",
            "dimension_name": "客户城市",
            "reason": "城市维度",
            "confidence": 0.9,
        }
    ]
    prior = CaliberEvidence(
        calibers=pd.DataFrame(
            [
                {
                    "table_name": "public.orders",
                    "column_name": "city_code",
                    "domain_name": "sales",
                    "column_role": "dimension",
                    "dimension_name": "客户城市",
                    "metric_name": None,
                    "time_window": None,
                    "probe_status": "CONFIRMED",
                }
            ]
        )
    )
    empty_catalog = SemanticCatalogEvidence(pd.DataFrame(), pd.DataFrame())
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [{"column_name": "city_code", "column_comment": "客户城市"}],
                catalog=empty_catalog,
                prior_staged=prior,
            )
        )
    )

    assert snapshot.is_empty()
    # exact pinned 名称是确定性命中，无需再调语义匹配模型。
    assert fake.match_payloads == []


def test_duplicate_relations_are_removed_but_conflicts_remain() -> None:
    base = {
        "table_name": "public.orders",
        "domain_name": "sales",
        "column_role": "metric",
        "dimension_name": None,
        "metric_name": "Revenue",
        "time_window": "1d",
        "additivity": "additive",
        "column_name": "gross_amount",
        "expr": "SUM(gross_amount)",
    }
    frame = pd.DataFrame(
        [
            {**base, "role_reason": "first"},
            {
                **base,
                "column_name": "duplicate_lineage_alias",
                "role_reason": "same persisted relation, different lineage",
            },
            {
                **base,
                "column_name": "net_amount",
                "expr": "SUM(net_amount)",
                "role_reason": "conflicting caliber",
            },
        ]
    )

    result = _filter_locked_and_dedupe(frame, set())

    assert len(result) == 2
    assert result["expr"].tolist() == ["SUM(gross_amount)", "SUM(net_amount)"]


def test_ambiguous_synonym_is_not_selected_deterministically() -> None:
    catalog = _SemanticCatalog.from_evidence(
        SemanticCatalogEvidence(
            dimensions=pd.DataFrame(
                [
                    {
                        "domain_name": "sales",
                        "dimension_name": "Buyer City",
                        "description": None,
                        "synonyms": ["city"],
                        "dimension_role": "categorical",
                    },
                    {
                        "domain_name": "sales",
                        "dimension_name": "Seller City",
                        "description": None,
                        "synonyms": ["city"],
                        "dimension_role": "categorical",
                    },
                ]
            ),
            metrics=pd.DataFrame(),
        )
    )

    assert catalog.match("dimension", "sales", "CITY") is None


def test_duplicate_existing_metric_is_not_a_discard_rule() -> None:
    prompt = build_column_role_system_prompt("zh")
    assert "语义相同不是 discarded" in prompt
    assert "语义与另一个已有指标完全重复" not in prompt


@pytest.mark.parametrize(
    "items",
    [
        [
            {"name": "A", "canonical_name": "A"},
            {"name": "A", "canonical_name": "A"},
        ],
        [
            {"name": "A", "canonical_name": "A"},
            {"name": "unexpected", "canonical_name": "A"},
        ],
        [{"name": " A ", "canonical_name": "A"}],
        [{"name": "A", "canonical_name": ["invalid"]}],
    ],
)
def test_semantic_match_validation_rejects_malformed_batch(items: list[dict]) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_name_normalization_items(items, {"A"})


def test_semantic_match_cycle_converges_incoming_names_to_the_cycle() -> None:
    roles = [
        {
            "column_name": column_name,
            "column_role": "metric",
            "metric_name": metric_name,
            "time_window": "1d",
            "additivity": "additive",
            "expr": f"SUM({column_name})",
            "reason": "metric",
            "confidence": 0.9,
        }
        for column_name, metric_name in (("a", "A"), ("b", "B"), ("c", "C"))
    ]
    fake = _FakeLlm(roles, matches={"A": "B", "B": "C", "C": "B"})
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1), language="en")

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [
                    {"column_name": "a", "data_type": "decimal"},
                    {"column_name": "b", "data_type": "decimal"},
                    {"column_name": "c", "data_type": "decimal"},
                ]
            )
        )
    )

    assert set(snapshot.calibers["metric_name"]) == {"B"}


def test_online_unstatused_staging_does_not_lock_caliber_candidate() -> None:
    roles = [
        {
            "column_name": "gross_amount",
            "column_role": "metric",
            "metric_name": "Revenue",
            "time_window": "1d",
            "additivity": "additive",
            "expr": "SUM(gross_amount)",
            "reason": "revenue",
            "confidence": 0.9,
        }
    ]
    stale_cache = CaliberEvidence(
        pd.DataFrame(
            [
                {
                    "table_name": "public.orders",
                    "column_name": "gross_amount",
                    "domain_name": "sales",
                    "column_role": "metric",
                    "dimension_name": None,
                    "metric_name": "Revenue",
                    "time_window": "1d",
                    "expr": "SUM(gross_amount)",
                    "additivity": "additive",
                }
            ]
        ),
        allow_unstatused=False,
    )
    fake = _FakeLlm(roles)
    handler = CaliberHandler(fake, runner=LlmRunner(fake, concurrency=1))

    snapshot = asyncio.run(
        handler.handle(
            _evidence(
                [{"column_name": "gross_amount", "data_type": "decimal"}],
                prior_staged=stale_cache,
            )
        )
    )

    assert snapshot.calibers["metric_name"].tolist() == ["Revenue"]
