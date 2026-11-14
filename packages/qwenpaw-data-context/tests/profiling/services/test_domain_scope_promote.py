"""Domain complete 只晋升实体，datasource_scope 归 semantic_config 维护。"""

import pytest
from sqlalchemy import insert

from profiling.repositories import staging_repo
from semantic_config.tables import domain


@pytest.mark.parametrize("scope", [None, "a$$$c"])
@pytest.mark.parametrize("status", ["CONFIRMED", "SKIPPED"])
async def test_domain_complete_uses_sc_defaults_and_preserves_reused_scope(
    business_flow, scope, status
):
    flow = business_flow
    await flow.add_source("b", [])
    await flow.session.execute(
        insert(domain).values(
            domain_name="sales",
            description="原正式描述",
            datasource_scope=scope,
            updated_by="original",
            created_at="2026-01-01T00:00:00+08:00",
            updated_at="2026-01-01T00:00:00+08:00",
            **flow.scope,
        )
    )
    [original] = await flow.formal(domain)
    for name in ("sales", "finance"):
        await staging_repo.insert_row(
            flow.session,
            "staging_biz_domain",
            {"domain_name": name, "description": "本轮描述"},
            datasource_id="b",
            task_id=None,
            probe_status=status,
            **flow.scope,
        )
    staged = await flow.rows("b", "BIZ_DOMAIN")

    result = await flow.complete("b", "BIZ_DOMAIN")
    assert result["promoted"] == (1 if status == "CONFIRMED" else 0)
    rows = {r["domain_name"]: r for r in await flow.formal(domain)}
    # 复用不改 ID、scope、业务元信息和审计字段；跳过也不更改正式实体。
    assert rows["sales"] == original
    if status == "CONFIRMED":
        assert rows["finance"]["datasource_scope"] == ""
        assert rows["finance"]["description"] == "本轮描述"
    else:
        assert set(rows) == {"sales"}
    # complete 不删除 staging；新增与复用候选都保留原审核状态。
    assert await flow.rows("b", "BIZ_DOMAIN") == staged

    before_retry = await flow.formal(domain)
    assert (await flow.complete("b", "BIZ_DOMAIN"))["promoted"] == 0
    assert await flow.formal(domain) == before_retry
