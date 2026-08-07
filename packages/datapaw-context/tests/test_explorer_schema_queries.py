"""Explorer schema summary must not issue one query per label/type."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from context_manager.api import explorer_api


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows


class _Session:
    def __init__(self):
        self.queries: list[str] = []

    def run(self, query, **_params):
        self.queries.append(query)
        if "UNWIND labels" in query:
            return _Rows([{"label": "Metric", "count": 3}])
        return _Rows([
            {
                "relationshipType": "HAS_METRIC",
                "count": 2,
                "source_label": "Domain",
                "target_label": "Metric",
            }
        ])


def test_schema_summary_uses_two_aggregate_queries(monkeypatch):
    session = _Session()

    @contextmanager
    def fake_graph_session(_driver):
        yield session

    monkeypatch.setattr(explorer_api, "graph_session", fake_graph_session)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(driver=object())))

    response = explorer_api.schema_summary(request)

    assert len(session.queries) == 2
    assert response["data"]["node_labels"][0]["count"] == 3
    assert response["data"]["relationship_types"][0]["count"] == 2
