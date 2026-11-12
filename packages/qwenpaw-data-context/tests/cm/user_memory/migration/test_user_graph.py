"""Verify UG migration preflight, in-place updates and atomic replay."""

from copy import deepcopy

import pytest
from context_manager.migration import UserGraphMigrationError, migrate_user_graph
from context_manager.schema import USER, USER_MEMORY
from context_manager.store import NodeRecord
from context_manager.store.neo4j import GraphTarget


class MigrationRuntime:
    """Keep physical IDs stable while applying the migration's property patches."""

    def __init__(self):
        self.target = GraphTarget("tenant-1", "workspace-1", "workspace_1")
        self.nodes = {
            "u": (
                "User",
                {
                    "workspace_id": "workspace-1",
                    "owner_id": "alice",
                    "zone": "user",
                    "user_id": "alice",
                    "status": "active",
                    "profile": '{"display_name":"Alice"}',
                },
            ),
            "m": (
                "UserMemory",
                {
                    "workspace_id": "workspace-1",
                    "owner_id": "alice",
                    "zone": "user",
                    "id": "old-memory",
                    "type": "preference",
                    "status": "active",
                    "description": "Prefer concise answers in Chinese.",
                    "source_type": "user_explicit",
                    "created_at": "2026-09-01T00:00:00+00:00",
                },
            ),
        }
        self.edges = {"r": ("HAS", "u", "m", {})}
        self.calls = []
        self.transactions = 0
        self.fail_on = None
        self.short_write = False

    def read(self, query, parameters, *, operation):
        self.calls.append(("read", operation))
        assert parameters["workspace_id"] == self.target.workspace_id
        assert "$workspace_id" in query
        name = operation.rsplit(".", 1)[1]
        if name in {"User", "UserMemory"}:
            return [
                {"element_id": identity, "properties": deepcopy(values)}
                for identity, (label, values) in self.nodes.items()
                if label == name
                and values.get("workspace_id") in {None, self.target.workspace_id}
            ]
        return [
            {
                "element_id": identity,
                "properties": deepcopy(properties),
                "source": deepcopy(self.nodes[source][1]),
                "target": deepcopy(self.nodes[target][1]),
                "target_labels": [self.nodes[target][0]],
            }
            for identity, (
                relationship,
                source,
                target,
                properties,
            ) in self.edges.items()
            if relationship == name
            and self.nodes[source][1].get("workspace_id") == self.target.workspace_id
        ]

    def write(self, query, parameters, *, operation):
        self.calls.append(("write", operation))
        assert "$workspace_id" in query and "elementId(" in query
        name = operation.rsplit(".", 1)[1]
        if name == self.fail_on:
            raise RuntimeError("injected write failure")
        for patch in parameters["rows"]:
            records = self.nodes if name in {"User", "UserMemory"} else self.edges
            records[patch["element_id"]][-1].update(patch["properties"])
        return [{"updated": len(parameters["rows"]) - int(self.short_write)}]

    def write_work(self, work, *, operation):
        self.transactions += 1
        before = deepcopy((self.nodes, self.edges))
        try:
            return work(self)
        except Exception:
            self.nodes, self.edges = before
            raise


def test_apply_preserves_records_and_relations_and_replay_is_empty():
    runtime = MigrationRuntime()
    before = deepcopy(runtime.nodes)
    migrate_user_graph(runtime, dry_run=False)

    assert runtime.transactions == 1
    assert runtime.nodes["u"][1] == {**before["u"][1], "key": "usr:workspace-1:alice"}
    assert runtime.nodes["m"][1] == {**before["m"][1], "key": "mem:alice:old-memory"}
    assert runtime.edges["r"] == (
        "HAS",
        "u",
        "m",
        {
            "workspace_id": "workspace-1",
            "owner_id": "alice",
            "zone": "user",
        },
    )
    for identity, spec in (("u", USER), ("m", USER_MEMORY)):
        assert NodeRecord(spec, runtime.nodes[identity][1]).key
    runtime.calls.clear()
    replay = migrate_user_graph(runtime, dry_run=False)
    assert replay["applied"]
    assert all(
        stats["pending"] == stats["updated"] == 0
        for group in ("nodes", "edges")
        for stats in replay[group].values()
    )
    assert all(kind == "read" for kind, _ in runtime.calls)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda r: r.nodes["m"][1].pop("id"), "id must be text"),
        (lambda r: r.nodes["u"][1].update(key="existing-other-key"), "existing key"),
        (lambda r: r.nodes["m"][1].update(key="mem:bob:old-memory"), "existing key"),
        (lambda r: r.nodes.update(duplicate=deepcopy(r.nodes["m"])), "duplicate key"),
        (lambda r: r.edges["r"][-1].update(owner_id="bob"), "existing owner_id"),
        (lambda r: r.nodes["m"][1].update(owner_id="bob"), "different owners"),
        (lambda r: r.nodes["u"][1].pop("workspace_id"), "workspace_id or zone"),
        (lambda r: r.nodes["m"][1].update(zone="knowledge"), "workspace_id or zone"),
    ],
)
def test_preflight_issues_block_all_writes(change, message):
    runtime = MigrationRuntime()
    change(runtime)
    before = deepcopy((runtime.nodes, runtime.edges))
    preview = migrate_user_graph(runtime)
    assert not preview["ready"]
    assert any(message in issue["message"] for issue in preview["issues"])
    with pytest.raises(UserGraphMigrationError) as raised:
        migrate_user_graph(runtime, dry_run=False)
    assert raised.value.report["issues"] == preview["issues"]
    assert not raised.value.report["applied"]
    assert (runtime.nodes, runtime.edges) == before
    assert all(kind == "read" for kind, _ in runtime.calls)
