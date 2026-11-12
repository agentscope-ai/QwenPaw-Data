"""Domain profiling 的跨数据源复用契约。

这组测试故意只使用内存 DataFrame、fake LLM 和内存 SQLite：

- 算法层要能看见「只有定义、当前数据源暂无成员」的已确认 Domain；
- create/rerun 可把当前源的新表分配给已有 Domain；
- refine 不得把「复用已有 Domain」的同名草稿当成冲突删掉；
- DATASET 要同时看见旧的 confirmed 成员和本轮 staged 增量；
- provider/sink/promote 路径保持「当前源暂存、正式 Domain 不重复创建」。

没有真实 LLM 与外部数据源，适合作为该能力的快速回归集。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import pandas as pd
import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import func, select

from profiling.algo.evidence import (
    DomainEvidence,
    LogEvidence,
    TableEvidence,
    TaskEvidence,
    assemble,
)
from profiling.algo.handlers.domain import DomainHandler, MODE_CREATE, MODE_REFINE
from profiling.algo.snapshots import DomainSnapshot, TableSnapshot
from profiling.algo.tasks import ProfileTask
from profiling.algo_provider import map_biz_domains
from profiling.provider import CandidateDataset, ProbeContext
from profiling.repositories import staging_repo
from profiling.services import promote
from profiling.services.sink import DbProbeSink
from semantic_config.tables import domain

from _sc_dbutil import new_memory_session


_DOMAIN_COLUMNS = (
    "domain_id",
    "domain_name",
    "display_name",
    "description",
)
_MEMBER_COLUMNS = ("table_name", "domain_id", "domain_name")


class _FakeLlm:
    """返回固定响应；若 handler 因覆盖率重试，继续返回最后一条。"""

    def __init__(self, *responses: str) -> None:
        assert responses
        self._responses = responses
        self.calls: list[tuple[str, str, float]] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append((system_prompt, user_prompt, temperature))
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


def _frame(rows: Iterable[dict], columns: tuple[str, ...]) -> pd.DataFrame:
    records = list(rows)
    return pd.DataFrame(records) if records else pd.DataFrame(columns=columns)


def _domains(
    domain_rows: Iterable[dict], member_rows: Iterable[dict]
) -> DomainEvidence:
    return DomainEvidence(
        domains=_frame(domain_rows, _DOMAIN_COLUMNS),
        members=_frame(member_rows, _MEMBER_COLUMNS),
    )


def _tables(*names: str) -> TableEvidence:
    return TableEvidence(
        TableSnapshot.build(
            pd.DataFrame(
                [
                    {"table_name": name, "table_comment": f"comment for {name}"}
                    for name in names
                ]
            )
        )
    )


def _response(*domains: dict) -> str:
    return json.dumps({"domains": list(domains)}, ensure_ascii=False)


def _domain_row(
    name: str,
    *,
    domain_id: int = 1,
    status: str | None = None,
    source_type: str | None = None,
    description: str = "canonical description",
) -> dict:
    row = {
        "domain_id": domain_id,
        "domain_name": name,
        "display_name": name.title(),
        "description": description,
    }
    if status is not None:
        row["probe_status"] = status
    if source_type is not None:
        row["source_type"] = source_type
    return row


def _member(
    table: str,
    domain: str,
    *,
    domain_id: int = 1,
    status: str | None = None,
    source_type: str | None = None,
) -> dict:
    row = {
        "table_name": table,
        "domain_id": domain_id,
        "domain_name": domain,
    }
    if status is not None:
        row["probe_status"] = status
    if source_type is not None:
        row["source_type"] = source_type
    return row


def _join_logs(*edges: tuple[str, str]) -> LogEvidence:
    return LogEvidence(
        {
            "join": pd.DataFrame(
                [
                    {
                        "left_table": left,
                        "right_table": right,
                        "on_columns": ["id"],
                        "count": 1,
                    }
                    for left, right in edges
                ]
            )
        }
    )


def test_confirmed_domain_without_current_members_reaches_domain_task() -> None:
    """当前源首跑时，跨源 Domain 只有定义而没有当前源成员。"""
    confirmed = _domains([_domain_row("sales", domain_id=101)], [])

    evidence = assemble(
        ProfileTask.DOMAIN,
        {
            "source_table": _tables("ds_b.orders"),
            "domain_confirmed": confirmed,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.prior_confirmed is confirmed, (
        "DOMAIN 必须看见没有当前源 member 的已确认 Domain，否则新数据源首跑无法复用它"
    )


async def test_create_reuses_workspace_domain_and_still_creates_new_domain() -> None:
    canonical = {
        **_domain_row("sales", domain_id=101),
        "display_name": "Canonical Sales",
        "description": "trusted workspace definition",
    }
    historical_table = "ds_a.historical_orders"
    confirmed = _domains(
        [canonical],
        [_member(historical_table, "sales", domain_id=101)],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales",
                "display_name": "Untrusted LLM Sales",
                "description": "must not overwrite the workspace definition",
                "tables": ["ds_b.orders"],
            },
            {
                "domain": "support",
                "display_name": "Support",
                "description": "a genuinely new domain",
                "tables": ["ds_b.tickets"],
            },
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables("ds_b.orders", "ds_b.tickets"),
        prior_confirmed=confirmed,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    assert set(snapshot.domains["domain_name"]) == {"sales", "support"}
    assert set(snapshot.members["table_name"]) == {
        "ds_b.orders",
        "ds_b.tickets",
    }
    assert snapshot.members["table_name"].value_counts().max() == 1
    assert set(snapshot.members["domain_name"]) == {"sales", "support"}
    by_name = snapshot.domains.set_index("domain_name")
    assert by_name.loc["sales", "display_name"] == "Canonical Sales"
    assert by_name.loc["sales", "description"] == "trusted workspace definition"

    prompt = llm.calls[0][1]
    context_json = prompt.split("可复用域：\n", 1)[1].split("\n\n以下是 JOIN", 1)[0]
    context = json.loads(context_json)
    assert context == [
        {
            "domain": "sales",
            "display_name": "Canonical Sales",
            "description": "trusted workspace definition",
        }
    ]
    assert all("tables" not in item for item in context)
    assert historical_table not in prompt, "跨源历史成员不应进入 LLM context"


async def test_create_coalesces_multiple_clusters_into_one_canonical_domain() -> None:
    cluster_a = ("ds_b.orders", "ds_b.order_items")
    cluster_b = ("ds_b.invoices", "ds_b.invoice_lines")
    confirmed = _domains(
        [
            {
                **_domain_row("sales", domain_id=101),
                "display_name": "Canonical Sales",
                "description": "trusted workspace definition",
            }
        ],
        [],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales",
                "display_name": "Orders supplied by LLM",
                "description": "first cluster text",
                "tables": list(cluster_a),
            },
            {
                "domain": "sales",
                "display_name": "Invoices supplied by LLM",
                "description": "second cluster text",
                "tables": list(cluster_b),
            },
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(*cluster_a, *cluster_b),
        logs=_join_logs(cluster_a, cluster_b),
        prior_confirmed=confirmed,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    assert snapshot.domains.to_dict(orient="records") == [
        {
            "domain_id": 0,
            "domain_name": "sales",
            "display_name": "Canonical Sales",
            "description": "trusted workspace definition",
            "origin": MODE_CREATE,
        }
    ]
    assert set(snapshot.members["table_name"]) == set(cluster_a + cluster_b)
    assert set(snapshot.members["domain_name"]) == {"sales"}
    assert snapshot.members["table_name"].value_counts().max() == 1
    assert len(llm.calls) == 1


async def test_new_domain_null_metadata_stays_null_in_snapshot() -> None:
    table = "ds_b.uncategorized"
    llm = _FakeLlm(
        _response(
            {
                "domain": "misc",
                "display_name": None,
                "description": None,
                "tables": [table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(table),
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    row = snapshot.domains.iloc[0]
    assert pd.isna(row["display_name"])
    assert pd.isna(row["description"])


async def test_formal_empty_metadata_is_not_filled_by_staging_or_llm() -> None:
    table = "ds_b.orders"
    formal_row = _domain_row("sales", domain_id=101)
    formal_row["display_name"] = None
    formal_row["description"] = None
    confirmed = _domains([formal_row], [])
    staged = _domains(
        [
            {
                **_domain_row(
                    "sales",
                    domain_id=201,
                    status="CONFIRMED",
                    source_type="ALGO",
                ),
                "display_name": "Staging must not fill formal metadata",
                "description": "Staging must not fill formal metadata",
            }
        ],
        [],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales",
                "display_name": "LLM must not fill formal metadata",
                "description": "LLM must not fill formal metadata",
                "tables": [table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(table),
        prior_confirmed=confirmed,
        prior_staged=staged,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    row = snapshot.domains.iloc[0]
    assert row["domain_name"] == "sales"
    assert pd.isna(row["display_name"])
    assert pd.isna(row["description"])


async def test_existing_name_uses_unique_casefold_match_and_preserves_spelling() -> (
    None
):
    table = "ds_b.orders"
    confirmed = _domains(
        [_domain_row("Sales-Core", domain_id=101)],
        [],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales-core",
                "display_name": "ignored",
                "description": "ignored",
                "tables": [table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(table),
        prior_confirmed=confirmed,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    assert snapshot.domains["domain_name"].tolist() == ["Sales-Core"]
    assert snapshot.members["domain_name"].tolist() == ["Sales-Core"]


async def test_incremental_create_can_emit_only_new_members_for_confirmed_domain() -> (
    None
):
    old_table = "ds_b.old_orders"
    new_table = "ds_b.new_orders"
    confirmed = _domains(
        [_domain_row("sales", domain_id=101)],
        [_member(old_table, "sales", domain_id=101)],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales",
                "display_name": "Sales",
                "description": "reuse existing sales domain",
                "tables": [new_table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(old_table, new_table),
        prior_confirmed=confirmed,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    assert list(snapshot.domains["domain_name"]) == ["sales"]
    assert snapshot.members.to_dict(orient="records") == [
        {"table_name": new_table, "domain_id": 0, "domain_name": "sales"}
    ]
    assert old_table not in snapshot.tables(), "已确认成员不应被重复 emit"


async def test_incremental_create_keeps_confirmed_staged_row_and_emits_same_name_delta() -> (
    None
):
    old_table = "ds_b.old_orders"
    new_table = "ds_b.new_orders"
    staged = _domains(
        [
            _domain_row(
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
        [
            _member(
                old_table,
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
    )
    llm = _FakeLlm(
        _response(
            {
                "domain": "sales",
                "display_name": "replacement must be ignored",
                "description": "replacement must be ignored",
                "tables": [new_table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(old_table, new_table),
        prior_staged=staged,
    )

    snapshot = await DomainHandler(llm, mode=MODE_CREATE).handle(evidence)

    assert snapshot.domains["domain_name"].tolist() == ["sales"]
    assert snapshot.members.to_dict(orient="records") == [
        {"table_name": new_table, "domain_id": 0, "domain_name": "sales"}
    ]
    assert old_table not in snapshot.tables()
    # handler 只产出增量；prior staging 的 CONFIRMED 行仍由编排层原地保留。
    assert staged.members["table_name"].tolist() == [old_table]


async def test_refine_preserves_pending_members_when_draft_reuses_confirmed_domain() -> (
    None
):
    old_table = "ds_b.old_orders"
    new_table = "ds_b.new_orders"
    confirmed = _domains(
        [_domain_row("sales", domain_id=101)],
        [_member(old_table, "sales", domain_id=101)],
    )
    staged = _domains(
        [
            _domain_row(
                "sales",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
                description="pending reuse assignment",
            )
        ],
        [
            _member(
                new_table,
                "sales",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            )
        ],
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(old_table, new_table),
        prior_confirmed=confirmed,
        prior_staged=staged,
    )

    # 无 LLM 时 refine 应回带草稿成员；不应因与 confirmed 同名而产出空快照。
    # 既然最终是复用正式 Domain，元信息应以 canonical 定义为准。
    snapshot = await DomainHandler(None, mode=MODE_REFINE).handle(evidence)

    assert list(snapshot.domains["domain_name"]) == ["sales"]
    assert list(snapshot.domains["origin"]) == [MODE_REFINE]
    assert list(snapshot.domains["description"]) == ["canonical description"]
    assert set(snapshot.members["table_name"]) == {new_table}


async def test_refine_coalesces_different_pending_drafts_into_canonical_domain() -> (
    None
):
    orders = "ds_b.orders"
    invoices = "ds_b.invoices"
    confirmed = _domains(
        [
            {
                **_domain_row("sales", domain_id=101),
                "display_name": "Canonical Sales",
                "description": "trusted workspace definition",
            }
        ],
        [],
    )
    staged = _domains(
        [
            _domain_row(
                "order_operations",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _domain_row(
                "invoice_operations",
                domain_id=202,
                status="PENDING",
                source_type="HUMAN",
            ),
        ],
        [
            _member(
                orders,
                "order_operations",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _member(
                invoices,
                "invoice_operations",
                domain_id=202,
                status="PENDING",
                source_type="HUMAN",
            ),
        ],
    )
    # 模型试图交换/清空成员；非空草稿的成员必须按 draft_id 确定性恢复。
    llm = _FakeLlm(
        _response(
            {
                "draft_id": "draft_0",
                "domain": "sales",
                "display_name": "LLM replacement",
                "description": "LLM replacement",
                "tables": [invoices],
            },
            {
                "draft_id": "draft_1",
                "domain": "sales",
                "display_name": "another replacement",
                "description": "another replacement",
                "tables": [],
            },
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(orders, invoices),
        prior_confirmed=confirmed,
        prior_staged=staged,
    )

    snapshot = await DomainHandler(llm, mode=MODE_REFINE).handle(evidence)

    assert snapshot.domains.to_dict(orient="records") == [
        {
            "domain_id": 0,
            "domain_name": "sales",
            "display_name": "Canonical Sales",
            "description": "trusted workspace definition",
            "origin": MODE_REFINE,
        }
    ]
    assert set(snapshot.members["table_name"]) == {orders, invoices}
    assert set(snapshot.members["domain_name"]) == {"sales"}
    assert snapshot.members["table_name"].value_counts().max() == 1
    assert len(llm.calls) == 1


async def test_refine_normalizes_omitted_draft_name_and_coalesces_members() -> None:
    alpha_table = "ds_b.alpha_orders"
    foo_table = "ds_b.foo_orders"
    staged = _domains(
        [
            _domain_row(
                "alpha",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _domain_row(
                "Foo",
                domain_id=202,
                status="PENDING",
                source_type="HUMAN",
            ),
        ],
        [
            _member(
                alpha_table,
                "alpha",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _member(
                foo_table,
                "Foo",
                domain_id=202,
                status="PENDING",
                source_type="HUMAN",
            ),
        ],
    )
    # draft_1 被模型遗漏；其原名 Foo 规范化后与 draft_0 的 foo
    # 是同一个新 Domain，应合并而不是加后缀派生第二个。
    llm = _FakeLlm(
        _response(
            {
                "draft_id": "draft_0",
                "domain": "foo",
                "display_name": "Foo",
                "description": "renamed alpha draft",
                "tables": [alpha_table],
            }
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(alpha_table, foo_table),
        prior_staged=staged,
    )

    snapshot = await DomainHandler(llm, mode=MODE_REFINE).handle(evidence)

    assert snapshot.domains["domain_name"].tolist() == ["foo"]
    assert snapshot.members.set_index("table_name")["domain_name"].to_dict() == {
        alpha_table: "foo",
        foo_table: "foo",
    }
    assert snapshot.members["table_name"].value_counts().max() == 1


async def test_refine_same_new_name_does_not_reuse_similar_catalog_name() -> None:
    alpha_table = "ds_b.alpha_orders"
    foo_table = "ds_b.foo_orders"
    confirmed = _domains([_domain_row("foo_1", domain_id=101)], [])
    staged = _domains(
        [
            _domain_row(
                "alpha",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _domain_row(
                "Foo",
                domain_id=202,
                status="PENDING",
                source_type="ALGO",
            ),
        ],
        [
            _member(
                alpha_table,
                "alpha",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
            _member(
                foo_table,
                "Foo",
                domain_id=202,
                status="PENDING",
                source_type="ALGO",
            ),
        ],
    )
    llm = _FakeLlm(
        _response(
            {
                "draft_id": "draft_0",
                "domain": "foo",
                "display_name": "Foo A",
                "description": "first new domain",
                "tables": [alpha_table],
            },
            {
                "draft_id": "draft_1",
                "domain": "foo",
                "display_name": "Foo B",
                "description": "second new domain",
                "tables": [foo_table],
            },
        )
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables(alpha_table, foo_table),
        prior_confirmed=confirmed,
        prior_staged=staged,
    )

    snapshot = await DomainHandler(llm, mode=MODE_REFINE).handle(evidence)

    assert snapshot.domains["domain_name"].tolist() == ["foo"]
    owners = snapshot.members.set_index("table_name")["domain_name"].to_dict()
    assert owners == {alpha_table: "foo", foo_table: "foo"}
    assert "foo_1" not in set(snapshot.domains["domain_name"])
    assert "foo_1" not in set(snapshot.members["domain_name"])


def test_refine_parse_rejects_one_table_in_different_domains() -> None:
    table = "ds_b.orders"
    response = _response(
        {
            "draft_id": "draft_0",
            "domain": "sales",
            "display_name": "Sales",
            "description": "sales view",
            "tables": [table],
        },
        {
            "draft_id": "draft_1",
            "domain": "support",
            "display_name": "Support",
            "description": "support view",
            "tables": [table],
        },
    )

    with pytest.raises(ValueError, match="被映射到多个 domain"):
        DomainHandler._parse_assignments(
            response,
            frozenset({table}),
            refine=True,
        )


def test_dataset_evidence_merges_confirmed_and_staged_domain_members() -> None:
    old_table = "ds_b.old_orders"
    new_table = "ds_b.new_orders"
    confirmed = _domains(
        [],
        [_member(old_table, "sales", domain_id=101)],
    )
    # staged 只补全本轮的新成员。
    staged = _domains(
        [],
        [_member(new_table, "sales", domain_id=101)],
    )

    evidence = assemble(
        ProfileTask.DATASET,
        {
            "source_table": _tables(old_table, new_table),
            "domain_confirmed": confirmed,
            "domain_staged": staged,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.domains is not None
    assert evidence.domains.domain_of(old_table) == "sales"
    assert evidence.domains.domain_of(new_table) == "sales"
    assert set(evidence.domains.members["table_name"]) == {old_table, new_table}
    assert evidence.domains.members["table_name"].value_counts().max() == 1


def test_incomplete_staged_member_does_not_pollute_formal_domain_mapping() -> None:
    table = "ds_b.orders"
    confirmed = _domains([], [_member(table, "sales", domain_id=101)])
    staged = _domains(
        [],
        [
            {
                "table_name": table,
                "domain_id": 201,
                "domain_name": None,
                "probe_status": "CONFIRMED",
                "source_type": "ALGO",
            }
        ],
    )

    evidence = assemble(
        ProfileTask.DATASET,
        {
            "source_table": _tables(table),
            "domain_confirmed": confirmed,
            "domain_staged": staged,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.domains is not None
    assert evidence.domains.domain_of(table) == "sales"


def test_domain_evidence_normalizes_member_keys_before_deduplication() -> None:
    table = "ds_b.orders"
    confirmed = _domains(
        [{**_domain_row(" sales ", domain_id=101)}],
        [_member(f" {table} ", " sales ", domain_id=101)],
    )
    staged = _domains(
        [
            _domain_row(
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
        [
            _member(
                table,
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
    )

    evidence = assemble(
        ProfileTask.DATASET,
        {
            "source_table": _tables(table),
            "domain_confirmed": confirmed,
            "domain_staged": staged,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.domains is not None
    assert evidence.domains.domain_of(table) == "sales"
    assert evidence.domains.members[["table_name", "domain_name"]].to_dict(
        orient="records"
    ) == [{"table_name": table, "domain_name": "sales"}]


def test_downstream_domain_evidence_only_accepts_confirmed_staged_rows() -> None:
    staged_rows = (
        (201, "sales", "ds_b.orders", "CONFIRMED"),
        (202, "support", "ds_b.tickets", "PENDING"),
        (203, "archive", "ds_b.old_tickets", "SKIPPED"),
    )
    staged = _domains(
        [
            _domain_row(
                name,
                domain_id=domain_id,
                status=status,
                source_type="ALGO",
            )
            for domain_id, name, _table, status in staged_rows
        ],
        [
            _member(
                table,
                name,
                domain_id=domain_id,
                status=status,
                source_type="ALGO",
            )
            for domain_id, name, table, status in staged_rows
        ],
    )

    evidence = assemble(
        ProfileTask.DATASET,
        {
            "source_table": _tables(*(row[2] for row in staged_rows)),
            "domain_staged": staged,
        },
    )

    assert isinstance(evidence, TaskEvidence)
    assert evidence.domains is not None
    assert evidence.domains.domains is not None
    assert evidence.domains.domains[
        ["domain_id", "domain_name", "probe_status", "source_type"]
    ].to_dict(orient="records") == [
        {
            "domain_id": 201,
            "domain_name": "sales",
            "probe_status": "CONFIRMED",
            "source_type": "ALGO",
        }
    ]
    assert evidence.domains.members[
        ["table_name", "domain_id", "domain_name", "probe_status", "source_type"]
    ].to_dict(orient="records") == [
        {
            "table_name": "ds_b.orders",
            "domain_id": 201,
            "domain_name": "sales",
            "probe_status": "CONFIRMED",
            "source_type": "ALGO",
        }
    ]


def test_dataset_evidence_rejects_conflicting_domain_membership() -> None:
    """同一 Dataset 不能因生命周期归并而同时属于两个 Domain。"""
    table = "ds_b.orders"
    confirmed = _domains([], [_member(table, "sales", domain_id=101)])
    staged = _domains([], [_member(table, "support", domain_id=202)])

    with pytest.raises(ValueError, match="Domain 归属冲突"):
        assemble(
            ProfileTask.DATASET,
            {
                "source_table": _tables(table),
                "domain_confirmed": confirmed,
                "domain_staged": staged,
            },
        )


async def test_refine_rejects_same_name_staged_rows_without_domain_ids() -> None:
    staged = _domains(
        [
            _domain_row(
                "sales",
                domain_id=None,
                status="CONFIRMED",
                source_type="ALGO",
            ),
            _domain_row(
                "sales",
                domain_id=None,
                status="PENDING",
                source_type="ALGO",
            ),
        ],
        [
            _member(
                "ds_b.orders",
                "sales",
                domain_id=None,
                status="PENDING",
                source_type="ALGO",
            )
        ],
    )
    evidence = TaskEvidence(
        task=ProfileTask.DOMAIN,
        tables=_tables("ds_b.orders"),
        prior_staged=staged,
    )

    with pytest.raises(ValueError):
        await DomainHandler(None, mode=MODE_REFINE).handle(evidence)


def test_prior_entries_normalize_pd_na_id_and_reject_same_name_ambiguity() -> None:
    single = _frame(
        [
            _domain_row(
                "sales",
                domain_id=pd.NA,
                status="PENDING",
                source_type="ALGO",
            )
        ],
        _DOMAIN_COLUMNS,
    )
    empty_members = _frame([], _MEMBER_COLUMNS)

    entries = DomainHandler._prior_entries(single, empty_members)

    assert len(entries) == 1
    assert entries[0]["domain_id"] is None

    ambiguous = _frame(
        [
            _domain_row(
                "sales",
                domain_id=pd.NA,
                status="PENDING",
                source_type="ALGO",
            ),
            _domain_row(
                "sales",
                domain_id=pd.NA,
                status="PENDING",
                source_type="HUMAN",
            ),
        ],
        _DOMAIN_COLUMNS,
    )
    with pytest.raises(ValueError):
        DomainHandler._prior_entries(ambiguous, empty_members)


def test_prior_entries_reject_duplicate_identity_with_different_status() -> None:
    domains = _frame(
        [
            _domain_row(
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            ),
            _domain_row(
                "sales",
                domain_id=201,
                status="PENDING",
                source_type="ALGO",
            ),
        ],
        _DOMAIN_COLUMNS,
    )

    with pytest.raises(ValueError, match="行身份冲突"):
        DomainHandler._prior_entries(
            domains,
            _frame([], _MEMBER_COLUMNS),
        )


def test_prior_entries_reject_staged_orphan_member() -> None:
    members = _frame(
        [
            _member(
                "ds_b.orders",
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
        _MEMBER_COLUMNS,
    )

    with pytest.raises(ValueError, match="缺少对应 Domain 行"):
        DomainHandler._prior_entries(
            _frame([], _DOMAIN_COLUMNS),
            members,
        )


def test_downstream_rejects_mixed_staging_status_shape() -> None:
    table = "ds_b.orders"
    staged = _domains(
        [
            _domain_row(
                "sales",
                domain_id=201,
                status="CONFIRMED",
                source_type="ALGO",
            )
        ],
        [_member(table, "sales", domain_id=201)],
    )

    with pytest.raises(ValueError, match="生命周期列不一致"):
        assemble(
            ProfileTask.DATASET,
            {
                "source_table": _tables(table),
                "domain_staged": staged,
            },
        )


async def test_reused_domain_candidate_stages_on_current_source_and_promotes_idempotently() -> (
    None
):
    tid, wid = "tenant-a", "workspace-a"
    ds_b = "source-b"
    principal = Principal(
        user_id="alice",
        tenant_id=tid,
        workspace_ids=[wid],
        active_workspace_id=wid,
        role="tenant_admin",
    )
    db = await new_memory_session()
    try:
        canonical_id = (
            await db.execute(
                domain.insert().values(
                    domain_name="sales",
                    description="canonical sales domain",
                    tenant_id=tid,
                    workspace_id=wid,
                    created_by=principal.user_id,
                )
            )
        ).inserted_primary_key[0]
        await db.commit()

        confirmed_staging_id = await staging_repo.insert_row(
            db,
            "staging_biz_domain",
            {
                "domain_name": "sales",
                "description": "previously reviewed assignment",
                "member_tables": json.dumps(
                    [
                        {
                            "source_table": "ds_b.old_orders",
                            "dataset_name": None,
                        }
                    ]
                ),
                "origin": MODE_CREATE,
            },
            datasource_id=ds_b,
            tenant_id=tid,
            workspace_id=wid,
            created_by=principal.user_id,
            task_id="PREVIOUS_DOMAIN_RUN",
            probe_status="CONFIRMED",
        )
        await db.commit()

        snapshot = DomainSnapshot.build(
            pd.DataFrame(
                [
                    {
                        **_domain_row("sales", domain_id=0),
                        "origin": MODE_CREATE,
                    }
                ]
            ),
            pd.DataFrame([_member("ds_b.orders", "sales", domain_id=0)]),
        )
        candidates = map_biz_domains(snapshot.to_frames())
        assert len(candidates) == 1

        context = ProbeContext(
            datasource_id=ds_b,
            datasource_type="sqlite",
            connection={},
            principal=principal,
            facts={
                "domain": pd.DataFrame([{"id": canonical_id, "domain_name": "sales"}])
            },
        )
        sink = DbProbeSink(
            db,
            context,
            datasource_id=ds_b,
            tenant_id=tid,
            workspace_id=wid,
            created_by=principal.user_id,
            task_id="DOMAIN_REUSE",
        )
        await sink.emit_biz_domains(candidates)

        staged = await staging_repo.list_all(db, "staging_biz_domain", ds_b, tid, wid)
        assert len(staged) == 2
        assert {row["domain_name"] for row in staged} == {"sales"}
        confirmed_row = next(row for row in staged if row["id"] == confirmed_staging_id)
        pending_row = next(row for row in staged if row["probe_status"] == "PENDING")
        assert confirmed_row["probe_status"] == "CONFIRMED"
        assert json.loads(confirmed_row["member_tables"]) == [
            {"source_table": "ds_b.old_orders", "dataset_name": None}
        ]
        assert pending_row["domain_name"] == "sales"
        assert pending_row["origin"] == MODE_CREATE
        assert pending_row["source_type"] == "ALGO"
        assert json.loads(pending_row["member_tables"]) == [
            {"source_table": "ds_b.orders", "dataset_name": None}
        ]

        # 后续候选仍按名解析到跨源的正式 Domain ID。
        await sink.emit_datasets(
            [
                CandidateDataset(
                    domain_name="sales",
                    dataset_name="orders",
                    description="orders from source B",
                    source="ds_b.orders",
                )
            ]
        )
        staged_datasets = await staging_repo.list_all(
            db, "staging_dataset", ds_b, tid, wid
        )
        assert staged_datasets[0]["domain_id"] == canonical_id

        await staging_repo.update_row(
            db,
            "staging_biz_domain",
            pending_row["id"],
            {"probe_status": "CONFIRMED"},
            datasource_id=ds_b,
            tenant_id=tid,
            workspace_id=wid,
            updated_by=principal.user_id,
        )
        await db.commit()

        result = await promote._complete_biz_domain(db, ds_b, "BIZ_DOMAIN", principal)
        await db.commit()

        assert result["promoted"] == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(domain)
                .where(
                    domain.c.tenant_id == tid,
                    domain.c.workspace_id == wid,
                    domain.c.domain_name == "sales",
                    domain.c.is_deleted == 0,
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(domain.c.id).where(
                    domain.c.domain_name == "sales",
                    domain.c.tenant_id == tid,
                    domain.c.workspace_id == wid,
                    domain.c.is_deleted == 0,
                )
            )
            == canonical_id
        )
    finally:
        await db.close()
