"""Verify concurrent callers share Workspace initialization and its outcome."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from context_manager.runtime import (
    ContextRuntimeUnavailable,
    WorkspaceRuntimePool,
    create_workspace_runtime,
)
from context_manager.runtime import workspace as workspace_module

from cm.infrastructure.execution.support import executor
from cm.infrastructure.lifecycle.test_startup import CapturingDriverFactory, _settings


@pytest.mark.parametrize("outcome", ["success", "failure", "shutdown"])
def test_concurrent_workspace_initialization(outcome, monkeypatch, caplog):
    connection = _settings()
    driver = CapturingDriverFactory()
    runtime = create_workspace_runtime(
        connection,
        driver_factory=driver,
        executor=executor,
        graph_read_executor=executor,
    )
    entered, waiting, release = Event(), Event(), Event()

    class WaitingFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(workspace_module, "Future", WaitingFuture)
    failure = OSError("graph initialization failed")
    failure.__cause__ = RuntimeError("backend diagnostic")

    def create(_connection):
        entered.set()
        assert release.wait(5)
        if outcome == "failure":
            raise failure
        return runtime

    factory = Mock(side_effect=create)
    pool = WorkspaceRuntimePool(factory)
    try:
        with ThreadPoolExecutor(max_workers=2) as threads:
            first = threads.submit(pool.resolve, connection)
            try:
                assert entered.wait(5)
                second = threads.submit(pool.resolve, connection)
                assert waiting.wait(5)
                if outcome == "shutdown":
                    pool.close()
            finally:
                release.set()
            if outcome == "success":
                assert first.result(5) is second.result(5) is runtime
                assert pool.resolve(connection) is runtime
            else:
                errors = [first.exception(5), second.exception(5)]
                assert isinstance(errors[0], ContextRuntimeUnavailable)
                assert errors[0] is errors[1]
                if outcome == "failure":
                    assert errors[0].__cause__ is failure
                    assert "backend diagnostic" in caplog.text
                else:
                    assert "pool is closed" in str(errors[0])
                    assert runtime.closed and driver.driver.close_count == 1
        factory.assert_called_once_with(connection)
        if outcome == "failure":
            assert len(caplog.records) == 1
            factory.side_effect = None
            factory.return_value = runtime
            assert pool.resolve(connection) is runtime
            assert factory.call_count == 2
    finally:
        pool.close()
        runtime.close()
