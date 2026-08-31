"""Regression tests for JSONSessionStore atomic writes and concurrency."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path

from qwenpaw_data.host.core.session import JSONSessionStore


def _trace_entry(index: int) -> dict:
    return {
        "graph_id": "g1",
        "node_id": f"n{index}",
        "event": {
            "role": "user",
            "id": f"evt_{index}",
            "content": f"message {index}",
        },
    }


def _process_updates(root: str, start: int, count: int, barrier) -> None:
    async def run() -> None:
        store = JSONSessionStore(Path(root))
        await asyncio.gather(
            *[
                store.update_session_state(
                    "multiprocess",
                    f"metadata.key_{i}",
                    i,
                    create_if_not_exist=True,
                )
                for i in range(start, start + count)
            ]
        )

    barrier.wait()
    asyncio.run(run())


async def test_concurrent_update_session_state_no_lost_updates(tmp_path):
    store = JSONSessionStore(tmp_path)
    total = 50

    await asyncio.gather(
        *[
            store.update_session_state(
                "sess",
                f"metadata.key_{i}",
                i,
                create_if_not_exist=True,
            )
            for i in range(total)
        ]
    )

    state = await store.get_session_state_dict("sess")
    assert len(state["metadata"]) == total
    for i in range(total):
        assert state["metadata"][f"key_{i}"] == i


async def test_two_store_instances_share_process_lock(tmp_path):
    stores = [JSONSessionStore(tmp_path), JSONSessionStore(tmp_path)]
    total = 40

    await asyncio.gather(
        *[
            stores[i % 2].update_session_state(
                "sess",
                f"metadata.key_{i}",
                i,
                create_if_not_exist=True,
            )
            for i in range(total)
        ]
    )

    state = await stores[0].get_session_state_dict("sess")
    assert len(state["metadata"]) == total


def test_two_worker_processes_do_not_overwrite_each_other(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    workers = [
        context.Process(
            target=_process_updates,
            args=(str(tmp_path), offset, 20, barrier),
        )
        for offset in (0, 20)
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    state = asyncio.run(
        JSONSessionStore(tmp_path).get_session_state_dict("multiprocess")
    )
    assert state["metadata"] == {f"key_{i}": i for i in range(40)}


async def test_concurrent_append_trace_event_no_lost_messages(tmp_path):
    store = JSONSessionStore(tmp_path)
    total = 50

    await asyncio.gather(
        *[store.append_trace_event("sess", _trace_entry(i)) for i in range(total)]
    )

    state = await store.get_session_state_dict("sess")
    content = state["agent"]["memory"]["content"]
    assert len(content) == total
    ids = {pair[0]["id"] for pair in content}
    assert ids == {f"evt_{i}" for i in range(total)}


async def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = JSONSessionStore(tmp_path)
    await store.update_session_state(
        "sess",
        "agent.memory.note",
        "x" * 10_000,
        create_if_not_exist=True,
    )

    path = store.get_path("sess")
    # File parses as complete JSON and no orphan temp files remain.
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["agent"]["memory"]["note"] == "x" * 10_000
    leftovers = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


async def test_atomic_write_fsyncs_file_and_directory(tmp_path, monkeypatch):
    calls: list[int] = []
    real_fsync = __import__("os").fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(
        "qwenpaw_data.host.core.session._locking.os.fsync",
        recording_fsync,
    )
    await JSONSessionStore(tmp_path).update_session_state(
        "durable",
        "metadata.key",
        "value",
        create_if_not_exist=True,
    )

    assert len(calls) == (1 if os.name == "nt" else 2)


async def test_mixed_readers_and_writers_never_see_partial_json(tmp_path):
    store = JSONSessionStore(tmp_path)
    await store.update_session_state(
        "sess",
        "metadata.seed",
        0,
        create_if_not_exist=True,
    )

    async def writer(i: int) -> None:
        await store.update_session_state(
            "sess",
            f"metadata.k{i}",
            "v" * 500,
            create_if_not_exist=True,
        )

    async def reader() -> None:
        # Would raise json.JSONDecodeError on a half-written file.
        state = await store.get_session_state_dict("sess")
        assert isinstance(state, dict)

    tasks = []
    for i in range(30):
        tasks.append(writer(i))
        tasks.append(reader())
    await asyncio.gather(*tasks)
