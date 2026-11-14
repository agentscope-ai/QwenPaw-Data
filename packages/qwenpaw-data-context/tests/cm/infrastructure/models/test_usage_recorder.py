"""Verify bounded admission, retries and deadline-aware writer shutdown."""

from dataclasses import replace
from datetime import UTC, datetime
from threading import Condition, Event, Thread
from time import monotonic

import pytest
from context_manager.model import usage_recorder
from context_manager.model.usage import (
    ModelUsageEvent,
)
from context_manager.model.usage_recorder import ModelUsageRecorder


class Repository:
    """Expose gates at the database boundary, not inside the recorder queue."""

    def __init__(
        self, *, failures=0, probe_failure=False, block_probe=False, block_write=False
    ):
        self.failures = failures
        self.probe_failure = probe_failure
        self.probe_gate = Event()
        self.write_gate = Event()
        if not block_probe:
            self.probe_gate.set()
        if not block_write:
            self.write_gate.set()
        self.probe_entered = Event()
        self.write_entered = Event()
        self.written = Event()
        self.four_attempts = Event()
        self.probes = 0
        self.writes = []

    def probe(self):
        self.probes += 1
        self.probe_entered.set()
        assert self.probe_gate.wait(3)
        if self.probe_failure:
            raise RuntimeError("missing usage table")

    def write(self, events):
        self.writes.append(events)
        self.write_entered.set()
        assert self.write_gate.wait(3)
        if len(self.writes) == 4:
            self.four_attempts.set()
        if len(self.writes) <= self.failures:
            raise RuntimeError("write unavailable")
        self.written.set()


@pytest.fixture
def event():
    return ModelUsageEvent(
        id="usage-1",
        user_id="user-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        session_id="",
        chat_id="",
        source="其他",
        source_step="other",
        model_type="chat",
        provider_id="provider-a",
        model_id="model-a",
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        duration_ms=10,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def threads(monkeypatch):
    """Observe thread termination without inspecting recorder state."""
    threads = []

    def create(*, target, name, daemon):
        finished = Event()

        def run():
            try:
                target()
            finally:
                finished.set()

        thread = Thread(target=run, name=name, daemon=daemon)
        threads.append((thread, finished))
        return thread

    monkeypatch.setattr(usage_recorder, "Thread", create)
    yield threads
    for thread, _ in threads:
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture
def waiting(monkeypatch):
    waiting = Event()

    class ObservedCondition(Condition):
        def wait(self, timeout=None):
            if timeout is not None:
                waiting.set()
            return super().wait(timeout)

    monkeypatch.setattr(usage_recorder, "Condition", ObservedCondition)
    return waiting


def test_stop_flushes_partial_batch_without_waiting_for_interval(
    event, threads, waiting
):
    repository = Repository()
    recorder = ModelUsageRecorder(repository, flush_interval=60)
    recorder.start()
    try:
        assert recorder.enqueue(event)
        assert waiting.wait(1)
        assert recorder.stop(timeout=1)
        assert repository.writes == [(event,)]
        assert recorder.usage_dropped_total == 0
    finally:
        recorder.stop()


@pytest.mark.parametrize("failures", [0, 1])
def test_inflight_write_may_return_but_never_retry_or_start_next_batch(
    event, threads, failures, caplog
):
    repository = Repository(failures=failures, block_write=True)
    recorder = ModelUsageRecorder(repository, batch_size=1)
    recorder.start()
    try:
        assert recorder.enqueue(event)
        assert repository.write_entered.wait(1)
        assert recorder.enqueue(replace(event, id="usage-2"))
        assert recorder.enqueue(replace(event, id="usage-3"))
        started = monotonic()
        assert not recorder.stop(timeout=0.02)
        assert monotonic() - started < 0.5
        assert recorder.usage_dropped_total == 2
        assert "still running after the shutdown deadline" in caplog.text
        started = monotonic()
        assert not recorder.stop(timeout=1)
        assert monotonic() - started < 0.5  # Repeated stop cannot extend the deadline.
        repository.write_gate.set()
        assert threads[0][1].wait(1)
        assert recorder.stop()
        assert repository.writes == [(event,)]
        assert recorder.usage_dropped_total == 2 + failures
    finally:
        repository.write_gate.set()
        recorder.stop()
