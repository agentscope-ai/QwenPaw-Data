"""Run authenticated rebuild jobs with real embeddings and disposable Neo4j.

Fixtures represent existing nodes with obsolete vectors but current text hashes;
model responses and Store writes are real. No business database/job directory is used.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from hashlib import sha256

import pytest
import pytest_asyncio
from context_manager.auth import WriteOwner
from context_manager.embedding import EmbeddingJobManager
from context_manager.embedding.service import public_job
from context_manager.process import build_embedding_text
from context_manager.schema import (
    ENTITY,
    HAS_MEMORY,
    HIERARCHY,
    USER,
    USER_MEMORY,
    Zone,
)

from cm.acceptance.test_graph_roundtrip import (
    assert_embeddings,
    graph_edges,
    graph_nodes,
)
from cm.acceptance.test_graph_roundtrip import (
    live_graph as live_graph,  # noqa: PLC0414 - intentional test fixture
)

CURRENT = "/api/v1/embed"
LEGACY = "/api/system/model-config/embedding"
BOB = "live-bob"


def stored_properties(spec, values):
    text = build_embedding_text(spec, values)
    return {
        **values,
        "embedding": (0.25,) * 1024,
        "embedding_hash": sha256(text.encode()).hexdigest(),
    }


def seed_existing_graph(live):
    def seed_user(store, user, entity_count):
        scope = live.write_scope(user, owner=WriteOwner.USER)
        names = (
            "会员商城",
            "订单服务",
            "退款服务",
            "会员服务",
            "营销中心",
            "优惠券服务",
            "库存服务",
            "支付服务",
            "履约服务",
            "客服平台",
            "经营看板",
        )[:entity_count]
        entities = store.get_nodes(
            ENTITY,
            store.create_nodes(
                ENTITY,
                tuple(
                    stored_properties(
                        ENTITY,
                        {
                            "identity_hash": sha256(name.encode()).hexdigest(),
                            "type": "product",
                            "name": name,
                            "description": f"{name}用于星舟零售的日常经营。",
                            "source_ids": ("synthetic-rebuild-fixture",),
                        },
                    )
                    for name in names
                ),
                scope=scope,
            ),
            scope=scope,
        )
        store.create_edges(
            HIERARCHY,
            tuple(
                (entities[0].key, node.key, {"description": "业务子系统"})
                for node in entities[1:]
            ),
            scope=scope,
        )
        profile = store.get_nodes(
            USER,
            store.create_nodes(
                USER, ({"user_id": user, "status": "active"},), scope=scope
            ),
            scope=scope,
        )[0]
        memories = store.get_nodes(
            USER_MEMORY,
            store.create_nodes(
                USER_MEMORY,
                tuple(
                    stored_properties(
                        USER_MEMORY,
                        {
                            "id": f"fixture-memory-{index}",
                            "type": "preference",
                            "description": description,
                            "source_type": "user_explicit",
                            "source_refs": (f"chat:synthetic-{index}",),
                            "status": "active",
                            "triggers": (),
                        },
                    )
                    for index, description in enumerate(
                        ("解释问题时请先给结论。", "导出的文件名请加上当天日期。")
                    )
                ),
                scope=scope,
            ),
            scope=scope,
        )
        store.create_edges(
            HAS_MEMORY,
            tuple((profile.key, node.key, {}) for node in memories),
            scope=scope,
        )

    def seed(store):
        seed_user(store, live.context().user_id, 11)
        seed_user(store, BOB, 2)
        shared_scope = live.write_scope(owner=WriteOwner.WORKSPACE, zone="knowledge")
        store.create_nodes(
            ENTITY,
            (
                stored_properties(
                    ENTITY,
                    {
                        "identity_hash": "shared-retail",
                        "type": "product",
                        "name": "共享经营看板",
                        "description": "整个工作区可见的零售经营看板。",
                    },
                ),
            ),
            scope=shared_scope,
        )

    live.store.atomic(seed)


@pytest_asyncio.fixture
async def existing_graph(live_graph):  # noqa: F811 - imported pytest fixture
    with sqlite3.connect(live_graph.database_path) as connection:
        connection.execute(
            "UPDATE role_bindings SET role = ? WHERE user_id = ?",
            ("ws_admin", live_graph.context().user_id),
        )
    await asyncio.to_thread(seed_existing_graph, live_graph)
    return live_graph


def nodes(live, user=None):
    user = user or live.context().user_id
    return {
        (node.spec, node.key): node
        for zone in (Zone.KNOWLEDGE, Zone.USER)
        for node in graph_nodes(live, zone, user)
    }


def edges(live, user=None):
    user = user or live.context().user_id
    return tuple(
        edge
        for zone in (Zone.KNOWLEDGE, Zone.USER)
        for edge in graph_edges(live, zone, user)
    )


async def wait_job(live, job_id, *, phase=None):
    async with asyncio.timeout(120):
        while True:
            response = await live.request("GET", f"{CURRENT}/jobs/{job_id}")
            assert response.status_code == 200, response.text
            job = response.json()
            if phase is not None and job["progress"]["phase"] == phase:
                return job
            if job["status"] in {"success", "failed"}:
                assert phase is None, job
                return job
            await asyncio.sleep(0.05)


async def start_job(live):
    response = await live.request("POST", f"{LEGACY}/rebuild")
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def assert_refreshed(live, before, user=None):
    after = nodes(live, user)
    assert set(after) == set(before)
    refreshed = []
    for identity, original in before.items():
        current = after[identity]
        if not original.spec.embedding_fields:
            assert current == original
            continue
        assert {
            key: value
            for key, value in current.properties.items()
            if key not in {"embedding", "embedding_hash"}
        } == {
            key: value
            for key, value in original.properties.items()
            if key not in {"embedding", "embedding_hash"}
        }
        assert current.properties["embedding"] != original.properties["embedding"]
        # A model rebuild must run even when the content hash is already current.
        assert (
            current.properties["embedding_hash"]
            == original.properties["embedding_hash"]
        )
        refreshed.append(current)
    assert Counter(node.properties["owner_id"] for node in refreshed) == (
        {BOB: 4}
        if user == BOB
        else {live.context().user_id: 13, BOB: 4, "__global__": 1}
    )
    assert_embeddings(refreshed)


@pytest.mark.asyncio
async def test_real_workspace_rebuild_changes_all_owners_vectors_and_persists_job(
    existing_graph, tmp_path
):
    live = existing_graph
    before, bob_before = nodes(live), nodes(live, BOB)
    before_edges, bob_edges = edges(live), edges(live, BOB)
    latest = await live.request("GET", f"{CURRENT}/jobs/latest")
    assert latest.status_code == 200 and latest.json() is None
    job_id = await start_job(live)
    job = await wait_job(live, job_id)
    assert job["status"] == "success", job
    assert job["progress"]["phase"] == "done"
    assert job["progress"]["labels_done"] == job["progress"]["labels_total"] > 0
    assert job["error"] is None
    assert job["started_at"] and job["finished_at"]
    assert set(job["config_snapshot"]) == {"model", "base_url", "dim"}
    assert job["config_snapshot"]["dim"] == 1024
    assert_refreshed(live, before)
    assert_refreshed(live, bob_before, BOB)
    assert edges(live) == before_edges and edges(live, BOB) == bob_edges
    latest = await live.request("GET", f"{LEGACY}/jobs/latest")
    assert latest.status_code == 200 and latest.json() == job
    owner = (live.tenant, live.workspace)
    reader = EmbeddingJobManager()
    assert public_job(await reader.get(owner, job_id)) == job
    await reader.aclose()
    for method, endpoint, status in (
        ("GET", f"{CURRENT}/jobs/{job_id}", 200),
        ("POST", f"{LEGACY}/rebuild", 403),
    ):
        denied = await live.request(method, endpoint, user=BOB)
        assert denied.status_code == status, denied.text
    bob_latest = await live.request("GET", f"{CURRENT}/jobs/latest", user=BOB)
    assert bob_latest.status_code == 200 and bob_latest.json()["job_id"] == job_id
    print(
        "Embedding readback: 18 workspace vectors refreshed across shared and two private owners, matching hashes/keys/facts/edges/job persistence=ok"
    )


@pytest.mark.asyncio
async def test_real_provider_failure_preserves_vectors_and_retries_same_job_with_current_config(
    existing_graph,
):
    live = existing_graph
    before, bob_before, before_edges = nodes(live), nodes(live, BOB), edges(live)
    config = await live.request("GET", "/api/v1/config")
    assert config.status_code == 200, config.text
    original_model = config.json()["embedding"]["model"]
    invalid_model = "cm-integration-missing-embedding-model"
    changed = await live.request(
        "PUT", "/api/v1/config/embed", json={"model": invalid_model}
    )
    assert changed.status_code == 200, changed.text
    try:
        job_id = await start_job(live)
        failed = await wait_job(live, job_id)
        assert failed["status"] == "failed", failed
        assert failed["progress"]["phase"] == "failed"
        assert (
            failed["error"]
            == "Embedding rebuild was rolled back; retry to rebuild from the beginning."
        )
        assert failed["config_snapshot"]["model"] == invalid_model
        denied = await live.request("POST", f"{LEGACY}/jobs/{job_id}/retry", user=BOB)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["error"]["code"] == "rebuild_forbidden"
        assert nodes(live) == before and nodes(live, BOB) == bob_before
        assert edges(live) == before_edges
    finally:
        restored = await live.request(
            "PUT", "/api/v1/config/embed", json={"model": original_model}
        )
        assert restored.status_code == 200, restored.text

    response = await live.request("POST", f"{LEGACY}/jobs/{job_id}/retry")
    assert response.status_code == 200 and response.json() == {
        "status": "retrying",
        "job_id": job_id,
    }
    succeeded = await wait_job(live, job_id)
    assert succeeded["status"] == "success", succeeded
    assert (
        succeeded["job_id"] == job_id
        and succeeded["created_at"] == failed["created_at"]
    )
    assert succeeded["started_at"] != failed["started_at"]
    assert succeeded["config_snapshot"]["model"] == original_model
    assert succeeded["error"] is None
    assert_refreshed(live, before)
    assert_refreshed(live, bob_before, BOB)
    assert edges(live) == before_edges
    print(
        "Embedding retry readback: real provider failure preserved graph, same job_id/new config recovery=ok"
    )
