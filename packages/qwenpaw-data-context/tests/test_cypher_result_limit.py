"""Cypher result consumption must stay bounded by the response limit."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from neo4j import Query

from context_manager.api import cypher_api


class _Record:
    def __init__(self, value: int) -> None:
        self.value = value

    def values(self):
        return [self.value]

    def data(self):
        return {"value": self.value}


class _Result:
    def __init__(self, total: int) -> None:
        self.total = total
        self.consumed = 0

    def keys(self):
        return ["value"]

    def __iter__(self):
        for value in range(self.total):
            self.consumed += 1
            yield _Record(value)


class _Session:
    def __init__(self, result: _Result) -> None:
        self.result = result
        self.query = None

    def run(self, query, **_params):
        self.query = query
        return self.result


def test_cypher_consumes_only_limit_plus_lookahead(monkeypatch):
    result = _Result(total=10_000)
    session = _Session(result)

    @contextmanager
    def fake_graph_session(_driver):
        yield session

    monkeypatch.setattr(cypher_api, "graph_session", fake_graph_session)
    monkeypatch.setenv("QWENPAW_DATA_CYPHER_TIMEOUT_SECONDS", "12")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(driver=object())),
    )

    response = cypher_api.execute_cypher(
        cypher_api.CypherRequest(
            cypher="MATCH (n) RETURN n.value AS value",
            limit=2,
            response_format="table",
        ),
        request,
    )

    data = response["data"]
    assert result.consumed == 3
    assert data["rows"] == [{"value": 0}, {"value": 1}]
    assert data["count"] == 2
    assert data["truncated"] is True
    assert data["summary"]["truncated"] is True
    assert isinstance(session.query, Query)
    assert session.query.timeout == 12
