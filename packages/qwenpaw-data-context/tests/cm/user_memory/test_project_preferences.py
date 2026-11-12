"""Port develop's ODPS evidence cases and verify the reconstructed ingest path."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from context_manager.auth import WriteOwner
from context_manager.runtime import create_context_runtime
from context_manager.schema import HAS_MEMORY, USER_MEMORY
from context_manager.user import MemoryIngestService
from context_manager.user.loader import ChatLoader, ChatLoaderCheckpointRepository
from context_manager.user.loader.observations import odps_project_observations

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.lifecycle.test_startup import (
    CapturingDriverFactory,
    _settings,
)
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.support import MemoryGraphStore
from cm.user_memory.loader.test_loader import SCOPE
from cm.user_memory.support import history_adapter
from cm.user_memory.test_history_ingest import _seed_unified_database
from cm.user_memory.test_reconciliation import _EmbeddingCaller

CUTOFF = datetime(2026, 8, 31, 23, tzinfo=UTC)


def _timestamp(minute):
    return datetime(2026, 8, 31, 14, tzinfo=UTC) + timedelta(minutes=minute)


def _call(*, name="context-manager__execute_read", arguments=None, sequence=3):
    return {
        "sequence_number": sequence,
        "created_at": _timestamp(2),
        "payload": {
            "type": "plugin_call",
            "status": "completed",
            "source_id": "call-1",
            "content": [
                {
                    "type": "data",
                    "data": {
                        "call_id": "call-1",
                        "name": name,
                        "arguments": json.dumps(
                            arguments
                            or {
                                "sql": "SELECT * FROM table_project.orders LIMIT 10",
                                "odps_execution_project": "sales_prod",
                                "metadata": json.dumps({"datasource_id": "sales_ds"}),
                            }
                        ),
                    },
                }
            ],
        },
    }


def _result(
    *,
    name="context-manager__execute_read",
    project="sales_prod_dev",
    sequence=4,
    minute=3,
    **output,
):
    return {
        "sequence_number": sequence,
        "created_at": _timestamp(minute),
        "payload": {
            "type": "plugin_call_output",
            "status": "completed",
            "source_id": "call-1",
            "content": [
                {
                    "type": "data",
                    "data": {
                        "call_id": "call-1",
                        "name": name,
                        "state": "success",
                        "output": json.dumps(
                            {
                                "exec_status": "success",
                                "error": None,
                                "requested_odps_execution_project": "sales_prod",
                                "odps_execution_project": project,
                                "odps_execution_project_fallback": True,
                                **output,
                            }
                        ),
                    },
                }
            ],
        },
    }


def _observations(events, *, chat_ds="sales_ds", types=None):
    return odps_project_observations(
        events,
        chat_id="chat-1",
        chat_datasource_id=chat_ds,
        datasource_types=types if types is not None else {"sales_ds": "odps"},
    )


def _insert_event(connection, event, **scope):
    connection.execute(
        "INSERT INTO chat_events (chat_id, session_id, tenant_id, workspace_id, "
        "user_id, sequence_number, object, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.get("chat_id", "chat-1"),
            scope.get("session_id", "session-1"),
            scope.get("tenant_id", "tenant-1"),
            scope.get("workspace_id", "workspace-1"),
            scope.get("user_id", "user-1"),
            event["sequence_number"],
            scope.get("object", "message"),
            json.dumps(event["payload"]),
            event["created_at"].isoformat(),
        ),
    )


def _datasource(connection):
    connection.execute("UPDATE chats SET datasource_id='sales_ds' WHERE id='chat-1'")
    connection.execute(
        "INSERT INTO datasource (datasource_id, datasource_type, tenant_id, workspace_id, is_deleted) "
        "VALUES ('sales_ds', 'odps', 'tenant-1', 'workspace-1', 0)"
    )


@pytest.mark.asyncio
async def test_history_to_store_checkpoint_replace_replay_and_isolation(
    tmp_path, monkeypatch
):
    path = tmp_path / "unified.db"
    _dsn = _seed_unified_database(path)
    with sqlite3.connect(path) as connection:
        _datasource(connection)
        _insert_event(connection, _call())
        _insert_event(connection, _result())
    loader = ChatLoader(ChatLoaderCheckpointRepository())
    runtime = create_context_runtime(
        _settings(), driver_factory=CapturingDriverFactory()
    )
    store = MemoryGraphStore()
    caller = _EmbeddingCaller()
    manager = StaticModelService(caller)
    monkeypatch.setattr(runtime, "graph_store", lambda scope: store)
    monkeypatch.setattr(
        runtime, "issue_graph_read_scope", store.access.issue_read_scope
    )
    monkeypatch.setattr(
        runtime, "issue_graph_write_scope", store.access.issue_write_scope
    )
    monkeypatch.setattr(runtime, "models", lambda context: manager)
    read = store.access.issue_read_scope(authorization())
    write = store.access.issue_write_scope(
        authorization(), owner=WriteOwner.USER, zone="user"
    )

    async def run():
        caller.completions.append('{"profile":null,"memories":[]}')
        return await history_adapter(runtime, loader=loader).ingest(
            cutoff_at=CUTOFF,
            target_scope=SCOPE,
        )

    def memories():
        return store.list_nodes(USER_MEMORY, scope=read)

    try:
        # The real ingest service must receive the loader's observations; rollback
        # must leave this exact source pending until a successful Store commit.
        store.fail_edge_write = True
        assert (await run())["sources"]["failed"] == 1
        assert memories() == ()
        assert loader.load_pending(cutoff_at=CUTOFF, target_scope=SCOPE).batches
        store.fail_edge_write = False
        assert (await run())["sources"]["succeeded"] == 1
        first = memories()[0]
        assert '"sales_prod_dev"' in first.properties["description"]
        assert first.properties["source_type"] == "user_implicit"
        assert len([edge for edge in store.edges if edge[0] is HAS_MEMORY]) == 1
        assert (await run())["sources"]["skipped"] == 1
        old_observations = _observations([_result()])
        with sqlite3.connect(path) as connection:
            _insert_event(
                connection, _result(sequence=5, project="bad", error="denied")
            )
        assert (await run())["sources"]["skipped"] == 1
        with sqlite3.connect(path) as connection:
            _insert_event(connection, _result(sequence=6, project="new_project"))
        assert (await run())["sources"]["succeeded"] == 1
        assert len(memories()) == 1
        assert memories()[0].key == first.key
        assert memories()[0].properties["id"] == first.properties["id"]
        assert '"new_project"' in memories()[0].properties["description"]
        caller.completions.append('{"profile":null,"memories":[]}')
        await MemoryIngestService(store, manager, executor=executor).ingest(
            (
                {
                    "id": "target",
                    "text": "查订单",
                    "timestamp": _timestamp(0).isoformat(),
                },
            ),
            read,
            write,
            target_message_id="target",
            tool_observations=old_observations,
        )
        assert '"new_project"' in memories()[0].properties["description"]
        with sqlite3.connect(path) as connection:
            _insert_event(
                connection, _result(sequence=7, minute=4, project="new_project")
            )
        assert (await run())["sources"]["succeeded"] == 1
        assert memories()[0].properties["source_ref"].endswith(":event:7")
        assert len(memories()[0].properties["source_refs"]) == 3
        assert (
            store.list_nodes(
                USER_MEMORY,
                scope=store.access.issue_read_scope(authorization("user-2")),
            )
            == ()
        )
        assert (
            len(caller.complete_calls) == 5
        )  # User text extraction only; no preference LLM.
    finally:
        await runtime.aclose()
