"""Fixed, scoped database queries for the Explorer canvas and its counts."""

from ...store import legay_cypher
from .ds_closure import closure_predicate


class ExplorerQueries:
    def __init__(self, access):
        self.access = access
        self.specs = access.specs()

    def read(self, queries, parameters, *, columns, order_by=(), limit=None):
        return legay_cypher.execute(
            self.access.store,
            queries,
            parameters,
            columns=columns,
            scope=self.access.scope,
            order_by=order_by,
            limit=limit,
        )

    @staticmethod
    def scope(*aliases):
        return " AND ".join(
            f"{a}.workspace_id = $scope.workspace "
            f"AND (size($scope.owners) = 0 OR {a}.owner_id IN $scope.owners) "
            f"AND ($scope.zone IS NULL OR {a}.zone = $scope.zone) "
            f"AND ($scope.datasource IS NULL OR {a}.datasource_id = $scope.datasource)"
            for a in aliases
        )

    def node_predicate(self, alias, spec):
        return f"{self.scope(alias)} AND {alias}.zone = '{spec.zone.value}'"

    def edge_specs(self, labels=None):
        public = {s.name for s in self.specs if labels is None or s.name in labels}
        return tuple(
            e
            for e in self.access.store.schema.edges
            if e.source.name in public and e.target.name in public
        )

    def edge_match(self, spec):
        return (
            f"MATCH (s:{spec.source.name})-[r:{spec.relationship}]->(t:{spec.target.name}) "
            f"WHERE {self.node_predicate('s', spec.source)} "
            f"AND {self.node_predicate('t', spec.target)} "
            f"AND {self.scope('r')} AND r.zone = '{spec.source.zone.value}'"
        )

    def nodes(self, labels, datasource_id, closure, limit):
        queries = [
            f"MATCH (n:{spec.name}) WHERE {self.node_predicate('n', spec)} "
            f"AND {closure_predicate('n', spec.name)} "
            f"RETURN n.key AS node_key, '{spec.name}' AS label, "
            "properties(n) AS props"
            for spec in self.specs
            if spec.name in labels
        ]
        return self.read(
            queries,
            {"datasource_id": datasource_id, "closure": closure},
            columns=("node_key", "label", "props"),
            order_by=("node_key",),
            limit=limit,
        )

    def edges(self, labels, datasource_id, closure, limit):
        queries = [
            f"{self.edge_match(spec)} "
            f"AND {closure_predicate('s', spec.source.name)} "
            f"AND {closure_predicate('t', spec.target.name)} "
            f"RETURN '{spec.source.name}' AS source_label, properties(s) AS source_props, "
            f"'{spec.target.name}' AS target_label, properties(t) AS target_props, "
            f"'{spec.relationship}' AS relationship, properties(r) AS props"
            for spec in self.edge_specs(labels)
        ]
        return self.read(
            queries,
            {"datasource_id": datasource_id, "closure": closure},
            columns=(
                "source_label",
                "source_props",
                "target_label",
                "target_props",
                "relationship",
                "props",
            ),
            limit=limit,
        )

    def roots(self, label, limit):
        specs = {s.name: s for s in self.specs}
        if label not in specs:
            return ()
        spec = specs[label]
        other = specs["Metric" if label == "Domain" else "Table"]
        count = "count(m)" if label == "Domain" else "count(DISTINCT m.key)"
        order = (
            "coalesce(n.name, '')"
            if label == "Domain"
            else "coalesce(n.datasource_name, n.name, '')"
        )
        return self.read(
            f"MATCH (n:{label}) WHERE {self.node_predicate('n', spec)} "
            f"OPTIONAL MATCH (n)-[r:HAS]->(m:{other.name}) "
            f"WHERE {self.node_predicate('m', other)} AND {self.scope('r')} "
            "AND r.zone = 'metadata' "
            f"RETURN properties(n) AS props, {count} AS count, "
            f"{order} AS sort_name, n.key AS node_key",
            {},
            columns=("props", "count", "sort_name", "node_key"),
            order_by=("sort_name", "node_key"),
            limit=limit,
        )

    def node_counts(self):
        return self.read(
            [
                f"MATCH (n:{s.name}) WHERE {self.node_predicate('n', s)} "
                f"RETURN '{s.name}' AS label, count(n) AS count"
                for s in self.specs
            ],
            {},
            columns=("label", "count"),
        )

    def edge_counts(self):
        return self.read(
            [
                f"{self.edge_match(s)} RETURN '{s.source.name}' AS source_label, "
                f"'{s.relationship}' AS relationship, '{s.target.name}' AS target_label, count(r) AS count"
                for s in self.edge_specs()
            ],
            {},
            columns=("source_label", "relationship", "target_label", "count"),
        )
