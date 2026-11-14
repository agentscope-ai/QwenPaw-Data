"""Exercise stalled Bolt health checks against a disposable local server.

Set QPD_TEST_EXPECT_BOLT_HINT=1 when the server is explicitly configured with
a sufficiently short receive-timeout hint to require the established-read case
to pass rather than report the known default-server failure.
"""

import os
import select
import socket
import socketserver
import threading
import time
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
from context_manager.store import StoreError
from context_manager.store.neo4j import GraphTarget, Neo4jRuntime
from neo4j import GraphDatabase


class _EstablishedHealthStall(AssertionError):
    pass


def test_user_query_disables_retries_without_changing_normal_sessions() -> None:
    driver = Mock()
    runtime = Neo4jRuntime(driver, GraphTarget("tenant", "workspace"))
    runtime._session(no_retry=True)
    driver.session.assert_called_with(
        max_transaction_retry_time=0, connection_acquisition_timeout=5.0
    )
    runtime._session()
    driver.session.assert_called_with()


@pytest.mark.parametrize(("operation", "limit"), [("health", 4), ("execute_query", 7)])
def test_connection_acquisition_is_bounded(operation: str, limit: int) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]
    release = threading.Event()

    def hold_handshake() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                release.wait(limit + 2)
        finally:
            listener.close()

    server = threading.Thread(target=hold_handshake)
    server.start()
    driver = GraphDatabase.driver(f"bolt://127.0.0.1:{port}", auth=("neo4j", "test"))
    runtime = Neo4jRuntime(driver, GraphTarget("tenant", "workspace"))
    try:
        start = time.monotonic()
        with pytest.raises(StoreError):
            runtime.read("RETURN 1 AS ok", {}, operation=operation)
        assert time.monotonic() - start < limit
    finally:
        runtime.close()
        release.set()
        server.join(timeout=6)
    assert not server.is_alive()


@pytest.mark.skipif(
    not os.getenv("CM_MCP_NEO4J_URI"), reason="requires disposable local Neo4j"
)
@pytest.mark.xfail(
    os.getenv("QPD_TEST_EXPECT_BOLT_HINT") != "1",
    strict=True,
    raises=_EstablishedHealthStall,
    reason="established Bolt reads have no bounded client-side timeout",
)
def test_established_connection_stall_bounds_health() -> None:
    uri = urlparse(os.environ["CM_MCP_NEO4J_URI"])
    if uri.scheme != "bolt" or uri.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("a disposable direct loopback Bolt URI is required")
    pause = threading.Event()
    paused = threading.Event()
    sockets = []

    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with socket.create_connection((uri.hostname, uri.port or 7687), timeout=3) as peer:
                    peer.settimeout(None)
                    sockets.extend((self.request, peer))
                    connections = {self.request: peer, peer: self.request}
                    while True:
                        if pause.is_set():
                            paused.set()
                            pause.wait(0.02)
                            time.sleep(0.02)
                            continue
                        ready, _, _ = select.select(list(connections), [], [], 0.05)
                        for connection in ready:
                            data = connection.recv(65536)
                            if not data:
                                return
                            connections[connection].sendall(data)
            except OSError:
                return

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", 0), Proxy)
    proxy_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    proxy_thread.start()
    driver = GraphDatabase.driver(
        f"bolt://127.0.0.1:{server.server_address[1]}",
        auth=("neo4j", os.environ["CM_MCP_NEO4J_PASSWORD"]),
        max_connection_pool_size=1,
    )
    runtime = Neo4jRuntime(driver, GraphTarget("tenant", "workspace"))
    failures = []
    worker = None
    try:
        assert runtime.read("RETURN 1 AS ok", {}, operation="health")[0]["ok"] == 1
        pause.set()
        assert paused.wait(2)

        def health():
            try:
                runtime.read("RETURN 1 AS ok", {}, operation="health")
            except StoreError as exc:
                failures.append(exc)

        worker = threading.Thread(target=health, daemon=True)
        worker.start()
        worker.join(4)
        if worker.is_alive():
            raise _EstablishedHealthStall("health check remained blocked on a silent connection")
        assert failures, "a stalled graph must not report healthy"
    finally:
        pause.clear()
        if worker is not None:
            worker.join(5)
            if worker.is_alive():
                for connection in sockets:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                worker.join(5)
        runtime.close()
        server.shutdown()
        server.server_close()
        proxy_thread.join(5)
    assert not proxy_thread.is_alive()
