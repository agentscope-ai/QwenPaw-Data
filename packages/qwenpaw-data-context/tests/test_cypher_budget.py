from __future__ import annotations

from types import SimpleNamespace

from context_manager.api import cypher_api


class _Record:
    def __init__(self, value: int) -> None:
        self._value = value

    def values(self):
        return [self._value]

    def data(self):
        return {"value": self._value}


class _Result:
    def __init__(self) -> None:
        self.requested = 0

    def keys(self):
        return ["value"]

    def fetch(self, amount: int):
        self.requested = amount
        return [_Record(index) for index in range(amount)]


class _Session:
    def __init__(self, result: _Result) -> None:
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def run(self, *_args, **_kwargs):
        return self.result


def test_cypher_materialization_is_bounded(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_MAX_CYPHER_ROWS", "2")
    result = _Result()
    monkeypatch.setattr(
        cypher_api,
        "graph_session",
        lambda _driver: _Session(result),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(driver=object()))
    )

    response = cypher_api.execute_cypher(
        cypher_api.CypherRequest(cypher="MATCH (n) RETURN n", limit=100),
        request,
    )

    assert result.requested == 3
    assert response["data"]["truncated"] is True
    assert len(response["data"]["rows"]) == 2
