"""Dataset 缺失归属补全：正式目录、已确认成员、聚类和失败边界。"""

import asyncio
import json
from dataclasses import replace

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from profiling.algo.evidence import (
    DomainEvidence,
    LogEvidence,
    TableEvidence,
    TaskEvidence,
    assemble,
)
from profiling.algo.handlers.dataset import DatasetHandler
from profiling.algo.handlers.domain import DomainHandler
from profiling.algo.handlers.runner import LlmRunner
from profiling.algo.snapshots import TableSnapshot
from profiling.algo.tasks import ProfileTask


class ScriptedLlm:
    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    async def complete(self, system_prompt, user_prompt, *, temperature=0.2):
        self.calls.append((system_prompt, user_prompt, temperature))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def response(*assignments):
    return json.dumps(
        {
            "domains": [
                {
                    "domain": domain,
                    "tables": list(tables),
                    "description": "LLM 不得覆盖正式描述",
                }
                for domain, tables in assignments
            ]
        },
        ensure_ascii=False,
    )


def tables(*names):
    return TableEvidence(
        TableSnapshot.build(
            pd.DataFrame(
                [{"table_name": name, "table_comment": "订单数据"} for name in names]
            ),
            {
                name: pd.DataFrame(
                    [
                        {
                            "table_name": name,
                            "column_name": "amount",
                            "data_type": "decimal",
                            "is_primary": "N",
                            "is_nullable": "Y",
                            "column_comment": "本源金额",
                        }
                    ]
                )
                for name in names
            },
        )
    )


def domain_evidence(owners=(), names=()):
    return DomainEvidence(
        members=pd.DataFrame(
            [{"table_name": table, "domain_name": domain} for table, domain in owners],
            columns=["table_name", "domain_name"],
        ),
        domains=pd.DataFrame(
            [
                {
                    "domain_id": index,
                    "domain_name": name,
                    "description": f"正式定义 {name}",
                }
                for index, name in enumerate(names, 1)
            ],
            columns=["domain_id", "domain_name", "description"],
        ),
    )


def evidence(*names, owners=(), catalog=("sales",), joins=()):
    formal = domain_evidence(owners, catalog)
    return TaskEvidence(
        task=ProfileTask.DATASET,
        tables=tables(*names),
        domains=formal,
        domain_catalog=formal.domains,
        logs=LogEvidence(
            {
                "join": pd.DataFrame(
                    [
                        {
                            "left_table": left,
                            "right_table": right,
                            "on_columns": "id",
                            "count": 3,
                        }
                        for left, right in joins
                    ]
                )
            }
        ),
    )


def input_payload(llm, index=0):
    prompt = llm.calls[index][1]
    if "完整输入证据：" in prompt:
        return json.loads(prompt.split("完整输入证据：", 1)[1])
    return json.loads(prompt.rsplit("\n\n", 1)[1])


def input_names(llm, index=0):
    payload = input_payload(llm, index)
    return {r["table_name"] for r in payload["unclustered_items"]} | {
        r["table_name"] for c in payload["cluster_items"] for r in c["tables"]
    }


def test_catalog_without_members_is_routed_without_changing_members_semantics():
    formal = domain_evidence(names=("sales",))
    assert formal.is_empty()
    result = assemble(
        ProfileTask.DATASET,
        {
            "source_table": tables("public.orders"),
            "domain_confirmed": formal,
        },
    )
    assert isinstance(result, TaskEvidence)
    assert result.domains is None
    assert_frame_equal(result.domain_catalog, formal.domains)


def test_staged_domains_never_expand_formal_catalog_and_only_confirmed_members_lock():
    staged = domain_evidence(
        [
            (f"public.{s.lower()}", s.lower())
            for s in ("CONFIRMED", "PENDING", "SKIPPED")
        ],
        ("confirmed", "pending", "skipped"),
    )
    staged.domains["probe_status"] = ["CONFIRMED", "PENDING", "SKIPPED"]
    staged.members["probe_status"] = ["CONFIRMED", "PENDING", "SKIPPED"]
    result = assemble(
        ProfileTask.DATASET,
        {
            "source_table": tables(
                "public.confirmed", "public.pending", "public.skipped"
            ),
            "domain_confirmed": domain_evidence(names=("sales",)),
            "domain_staged": staged,
        },
    )
    assert result.domain_catalog["domain_name"].tolist() == ["sales"]
    assert result.domains.members["table_name"].tolist() == ["public.confirmed"]


@pytest.mark.parametrize("mode", ["create", "refine"])
async def test_confirmed_members_bypass_llm_even_without_formal_catalog(mode):
    ev = evidence("public.orders", owners=(("public.orders", "sales"),), catalog=())
    llm = ScriptedLlm(AssertionError("不应重做已确认归属"))
    handler = DatasetHandler(
        llm, mode=mode, derived_from_tables_include_patterns=("%",)
    )
    result = await handler.handle(ev)
    assert result.table_meta["domain_name"].tolist() == ["sales"]
    assert llm.calls == []


@pytest.mark.parametrize(
    "canonical,proposed",
    [("sales", "sales"), ("SalesOps", "salesops"), ("销售域", "销售域")],
)
async def test_existing_assignment_canonicalizes_names_and_uses_formal_metadata(
    canonical, proposed
):
    ev = evidence("public.orders", catalog=(canonical,))
    llm = ScriptedLlm(response((proposed, ["public.orders"])))
    result = await DomainHandler(llm).assign_existing(ev)
    assert result.members["domain_name"].tolist() == [canonical]
    assert result.domains["description"].tolist() == [f"正式定义 {canonical}"]
    assert result.domains["domain_id"].tolist() == [0]  # 本轮局部序号，非正式 ID。


async def test_only_missing_tables_are_clustered_and_evidence_is_not_mutated():
    ev = evidence(
        "public.old",
        "public.orders",
        "public.items",
        "public.refunds",
        "public.refund_items",
        owners=(("public.old", "sales"),),
        joins=(
            ("public.old", "public.orders"),
            ("public.orders", "public.items"),
            ("public.refunds", "public.refund_items"),
        ),
    )
    original_members = ev.domains.members.copy(deep=True)
    llm = ScriptedLlm(
        response(
            ("sales", ["public.orders", "public.items"]),
            ("sales", ["public.refunds", "public.refund_items"]),
        )
    )
    handler = DatasetHandler(llm, derived_from_tables_include_patterns=("%",))
    updated = await handler._with_domain_assignments(ev)
    assert input_names(llm) == ev.tables.names() - {"public.old"}
    assert len(input_payload(llm)["cluster_items"]) == 2
    assert len(updated.domains.members) == 5
    assert updated.domains.members["table_name"].is_unique
    assert_frame_equal(ev.domains.members, original_members)
    result = await handler.handle(updated)
    assert set(result.table_meta["domain_name"]) == {"sales"}
    assert len(llm.calls) == 1  # 补全后的 evidence 不重复调用。


async def test_invalid_new_domain_is_retried_without_enabling_creation():
    llm = ScriptedLlm(
        response(("invented", ["public.orders"])),
        response(("sales", ["public.orders"])),
    )
    result = await DomainHandler(llm).assign_existing(evidence("public.orders"))
    assert result.members["domain_name"].tolist() == ["sales"]
    assert len(llm.calls) == 2
    assert "不能新增" in llm.calls[1][1]
    assert all("禁止新增" in call[0] for call in llm.calls)


async def test_llm_cannot_move_locked_tables_even_if_it_returns_them():
    ev = evidence(
        "public.old",
        "public.new",
        owners=(("public.old", "sales"),),
        catalog=("sales", "support"),
    )
    llm = ScriptedLlm(response(("support", ["public.old", "public.new"])))
    result = await DatasetHandler(
        llm, derived_from_tables_include_patterns=("%",)
    ).handle(ev)
    assert result.table_meta.set_index("table_name")["domain_name"].to_dict() == {
        "public.old": "sales",
        "public.new": "support",
    }
    assert input_names(llm) == {"public.new"}


async def test_repeated_assignment_to_same_canonical_domain_is_deduplicated():
    llm = ScriptedLlm(
        response(
            ("Sales", ["public.orders", "public.orders"]), ("sales", ["public.orders"])
        )
    )
    result = await DomainHandler(llm).assign_existing(
        evidence("public.orders", catalog=("Sales",))
    )
    assert len(result.members) == len(result.domains) == 1
    assert result.members["domain_name"].tolist() == ["Sales"]


async def test_ambiguous_casefold_cannot_fall_through_to_a_lowercase_catalog_name():
    llm = ScriptedLlm(response(("SALES", ["public.orders"])))
    with pytest.raises(ValueError, match="无法唯一匹配"):
        await DomainHandler(llm).assign_existing(
            evidence("public.orders", catalog=("Sales", "sales"))
        )
    assert len(llm.calls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "{broken",
        response(("invented", ["public.orders"])),
        response(("sales", ["public.orders"]), ("support", ["public.orders"])),
        response(("sales", ["other_source.orders"])),
        response(),
    ],
)
async def test_invalid_assignments_fail_after_retry(bad):
    llm = ScriptedLlm(bad)
    with pytest.raises(ValueError, match="public.orders"):
        await DomainHandler(llm).assign_existing(
            evidence("public.orders", catalog=("sales", "support"))
        )
    assert len(llm.calls) == 2


async def test_same_cluster_cannot_be_split_across_domains():
    ev = evidence(
        "public.orders",
        "public.items",
        catalog=("sales", "support"),
        joins=(("public.orders", "public.items"),),
    )
    llm = ScriptedLlm(
        response(("sales", ["public.orders"]), ("support", ["public.items"]))
    )
    with pytest.raises(ValueError, match="同一 cluster"):
        await DomainHandler(llm).assign_existing(ev)
    assert len(llm.calls) == 2


@pytest.mark.parametrize("assigned_count", [9, 10])
async def test_existing_assignment_above_eighty_percent_does_not_retry(assigned_count):
    names = [f"public.orders_{i}" for i in range(10)]
    llm = ScriptedLlm(
        response(("sales", names[:assigned_count])),
        AssertionError("总体覆盖率超过 80% 后不应重试"),
    )
    result = await DomainHandler(llm).assign_existing(evidence(*names))
    assert result.tables() == frozenset(names[:assigned_count])
    assert len(llm.calls) == 1


@pytest.mark.parametrize("retry_count", [7, 8, 9, 10])
async def test_exactly_eighty_percent_retries_and_must_exceed_threshold(retry_count):
    names = [f"public.orders_{i}" for i in range(10)]
    llm = ScriptedLlm(
        response(("sales", names[:8])), response(("sales", names[:retry_count]))
    )
    if retry_count > 8:
        result = await DomainHandler(llm).assign_existing(evidence(*names))
        assert result.tables() == frozenset(names[:retry_count])
    else:
        with pytest.raises(ValueError, match="总体覆盖率 80.0% 未超过 80%") as exc:
            await DomainHandler(llm).assign_existing(evidence(*names))
        assert "orders_8" in str(exc.value)
        assert "orders_7" not in str(exc.value)  # 重试更差时保留首轮覆盖较高的结果。
    assert len(llm.calls) == 2


@pytest.mark.parametrize("confirmed_count", [9, 10])
@pytest.mark.parametrize("has_llm", [False, True])
async def test_confirmed_overall_coverage_bypasses_assignment(confirmed_count, has_llm):
    names = [f"public.orders_{i}" for i in range(10)]
    ev = evidence(
        *names,
        owners=tuple((name, "sales") for name in names[:confirmed_count]),
        catalog=(),
    )
    llm = (
        ScriptedLlm(AssertionError("已有归属总体覆盖率超过 80%，无需补分配"))
        if has_llm
        else None
    )
    result = await DatasetHandler(
        llm,
        derived_from_tables_include_patterns=("%",),
    ).handle(ev)
    assert set(result.table_meta["table_name"]) == set(names[:confirmed_count])
    assert set(result.table_meta["domain_name"]) == {"sales"}
    if llm is not None:
        assert llm.calls == []


@pytest.mark.parametrize(
    ("initial_count", "retry_count"), [(3, None), (2, 3), (2, 1), (1, 2), (1, 1)]
)
async def test_assignment_coverage_includes_confirmed_input_tables(
    initial_count, retry_count
):
    names = [f"public.orders_{i}" for i in range(10)]
    # 当前输入之外的历史成员不计入总体覆盖率。
    owners = tuple((name, "sales") for name in names[:6] + ["other_source.history"])
    ev = evidence(*names, owners=owners)
    llm = ScriptedLlm(
        response(("sales", names[6 : 6 + initial_count])),
        response(("sales", names[6 : 6 + retry_count]))
        if retry_count is not None
        else AssertionError("总体超过 80% 后不应重试"),
    )
    best_count = max(initial_count, retry_count or 0)
    if best_count > 2:
        result = await DomainHandler(llm).assign_existing(ev)
        assert result.tables() == frozenset(names[6 : 6 + best_count])
    else:
        with pytest.raises(
            ValueError, match=f"总体覆盖率 {(6 + best_count) / 10:.1%} 未超过 80%"
        ):
            await DomainHandler(llm).assign_existing(ev)
    assert len(llm.calls) == (1 if retry_count is None else 2)
    for index in range(len(llm.calls)):
        assert input_names(llm, index) == set(names[6:])


@pytest.mark.parametrize("assign_one", [False, True])
async def test_exactly_eighty_percent_confirmed_still_requires_assignment(assign_one):
    names = [f"public.orders_{i}" for i in range(100)]
    ev = evidence(*names, owners=tuple((name, "sales") for name in names[:80]))
    llm = ScriptedLlm(response(("sales", names[80:81])) if assign_one else response())
    if assign_one:
        result = await DomainHandler(llm).assign_existing(ev)
        assert result.tables() == frozenset(
            names[80:81]
        )  # 总体 81%，待分配部分仅覆盖 5%。
        assert len(llm.calls) == 1
    else:
        with pytest.raises(ValueError, match="总体覆盖率 80.0% 未超过 80%"):
            await DomainHandler(llm).assign_existing(ev)
        assert len(llm.calls) == 2


async def test_normal_domain_create_still_allows_new_domains_and_eighty_percent_coverage():
    names = [f"public.orders_{i}" for i in range(10)]
    llm = ScriptedLlm(response(("new_domain", names[:8])))
    result = await DomainHandler(llm).handle(
        TaskEvidence(task=ProfileTask.DOMAIN, tables=tables(*names))
    )
    assert result.domains["domain_name"].tolist() == ["new_domain"]
    assert len(result.members) == 8
    assert len(llm.calls) == 1


async def test_normal_domain_create_coverage_still_counts_only_unassigned_tables():
    names = [f"public.orders_{i}" for i in range(10)]
    llm = ScriptedLlm(response(("sales", names[8:9])), response(("sales", names[8:])))
    result = await DomainHandler(llm).handle(
        TaskEvidence(
            task=ProfileTask.DOMAIN,
            tables=tables(*names),
            prior_confirmed=domain_evidence(
                [(name, "sales") for name in names[:8]], ("sales",)
            ),
        )
    )
    assert result.tables() == frozenset(names[8:])
    assert len(llm.calls) == 2


async def test_empty_catalog_does_not_call_llm_or_use_staged_catalog_as_formal():
    ev = evidence("public.orders", catalog=())
    ev = replace(ev, domains=domain_evidence(names=("staged_only",)))
    llm = ScriptedLlm(AssertionError("没有正式域，不应请求模型"))
    with pytest.raises(ValueError, match="没有可选的正式 Domain"):
        await DomainHandler(llm).assign_existing(ev)
    assert llm.calls == []


async def test_missing_llm_is_explicit_and_transport_errors_propagate():
    with pytest.raises(ValueError, match="需要 LLM"):
        await DomainHandler(None).assign_existing(evidence("public.orders"))
    with pytest.raises(RuntimeError, match="outage"):
        await DomainHandler(ScriptedLlm(RuntimeError("outage"))).assign_existing(
            evidence("public.orders")
        )


async def test_runner_preserves_temperature_and_checks_cancellation_before_retry():
    llm = ScriptedLlm(response())
    checks = []

    def cancel_on_retry():
        checks.append(True)
        if len(checks) == 2:
            raise asyncio.CancelledError

    runner = LlmRunner(llm, temperature=0.65, raise_if_cancelled=cancel_on_retry)
    with pytest.raises(asyncio.CancelledError):
        await DomainHandler(llm).assign_existing(
            evidence("public.orders"), runner=runner
        )
    assert len(llm.calls) == 1
    assert llm.calls[0][2] == 0.65
    assert len(checks) == 2


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_restricted_prompts_never_offer_domain_creation(language):
    llm = ScriptedLlm(response(), response(("sales", ["public.orders"])))
    await DomainHandler(llm, language=language).assign_existing(
        evidence("public.orders")
    )
    for system, user, _ in llm.calls:
        assert "只有没有合适的已有 domain 时才新建" not in user
        assert "Create a new domain only when none is suitable" not in user
        assert (
            ("禁止新增" in system) if language == "zh" else ("never create" in system)
        )


async def test_log_dataset_keeps_existing_source_and_first_parent_inheritance():
    sql = "SELECT amount AS total FROM public.orders"
    ev = evidence("public.orders")
    ev = replace(
        ev,
        logs=LogEvidence(
            {
                "sql_template": pd.DataFrame(
                    [
                        {
                            "parents": "public.orders",
                            "repeated_sql": sql,
                            "count": 10,
                        }
                    ]
                )
            }
        ),
    )
    llm = ScriptedLlm(
        response(("sales", ["public.orders"])),
        json.dumps(
            [
                {
                    "sql": sql,
                    "parents": "public.orders",
                    "name": "logical_orders",
                    "description": "销售视图",
                }
            ]
        ),
    )
    result = await DatasetHandler(
        llm, runner=LlmRunner(llm), derived_from_tables_enabled=False
    ).handle(ev)
    assert result.table_meta[["table_name", "domain_name", "source"]].to_dict(
        "records"
    ) == [
        {"table_name": "logical_orders", "domain_name": "sales", "source": sql},
    ]
    assert result.column_meta_of("logical_orders")["column_name"].tolist() == ["total"]
