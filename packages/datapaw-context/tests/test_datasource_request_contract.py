from __future__ import annotations

import unittest

from context_manager.api.cypher_api import CypherRequest
from context_manager.api.datasource_filter import filter_graph_by_datasource
from context_manager.api.explorer_api import GlobalGraphRequest


class DatasourceRequestContractTest(unittest.TestCase):
    def test_datasource_id_is_accepted_by_graph_requests(self) -> None:
        explorer_request = GlobalGraphRequest(datasource_id="postgresql-new")
        cypher_request = CypherRequest(
            cypher="MATCH (n) RETURN n",
            datasource_id="postgresql-new",
        )

        self.assertEqual(
            explorer_request.resolved_datasource_id,
            "postgresql-new",
        )
        self.assertEqual(cypher_request.resolved_datasource_id, "postgresql-new")

    def test_explorer_props_are_filtered_by_datasource_id(self) -> None:
        graph = {
            "nodes": [
                {
                    "id": "domain-a",
                    "label": "Sales Domain",
                    "group": "Domain",
                    "props": {"datasource_id": "postgresql-a"},
                },
                {
                    "id": "domain-b",
                    "label": "Support Domain",
                    "group": "Domain",
                    "props": {"datasource_id": "postgresql-b"},
                },
                {
                    "id": "entity-global",
                    "group": "Entity",
                    "props": {},
                },
            ],
            "edges": [
                {
                    "from": "domain-a",
                    "to": "entity-global",
                    "type": "LINKS",
                },
                {
                    "from": "domain-b",
                    "to": "entity-global",
                    "type": "LINKS",
                },
            ],
        }

        filtered = filter_graph_by_datasource(graph, "postgresql-a")

        self.assertIsNotNone(filtered)
        assert filtered is not None
        self.assertEqual(
            {node["id"] for node in filtered["nodes"]},
            {"domain-a", "entity-global"},
        )
        self.assertEqual(len(filtered["edges"]), 1)
        self.assertEqual(filtered["edges"][0]["from"], "domain-a")


if __name__ == "__main__":
    unittest.main()
