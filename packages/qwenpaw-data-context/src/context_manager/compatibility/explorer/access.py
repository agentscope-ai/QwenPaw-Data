"""Read canonical records in one authorized, request-local Explorer view."""

from __future__ import annotations

from ...store import NeighborDirection


class ExplorerAccess:
    def __init__(self, store, scope):
        self.store = store
        self.scope = scope
        self._nodes = {}

    def specs(self, labels=None):
        """Select public types belonging to this Explorer view."""
        return tuple(
            spec
            for spec in self.store.schema.nodes
            if not spec.internal
            and (not self.scope.zone or spec.zone.value == self.scope.zone)
            and (labels is None or spec.name in labels)
        )

    def nodes(self, labels=None, *, filters=None, fields=None):
        """Stream scoped pages instead of retaining a complete graph snapshot."""
        for spec in self.specs(labels):
            offset = 0
            while True:
                page = self.store.list_nodes(
                    spec,
                    filters=filters,
                    fields=fields,
                    limit=500,
                    offset=offset,
                    scope=self.scope,
                )
                yield from page
                if len(page) < 500:
                    break
                offset += len(page)

    def node(self, key, labels=None):
        """Resolve a canonical key inside the authorized public types."""
        specs = self.specs(labels)
        # Canonical keys include their declared prefix. Unknown legacy keys
        # still use bounded exact lookups, never a graph-wide materialization.
        preferred = tuple(
            spec
            for spec in specs
            if key.startswith(
                (spec.property("key").prefix + ":", spec.property("key").prefix + "_")
            )
        )
        for spec in preferred or specs:
            identity = (spec.name, key)
            if identity not in self._nodes:
                self._nodes[identity] = self.store.get_nodes(
                    spec, (key,), scope=self.scope
                )[0]
            node = self._nodes[identity]
            if node is not None:
                return node
        return None

    def neighbors(self, node, *, direction=NeighborDirection.BOTH):
        """Stream authorized neighbors with their persisted edge orientation."""
        offset = 0
        public = {spec.name for spec in self.specs()}
        while True:
            page = self.store.expand_neighbors(
                node.spec,
                node.key,
                direction=direction,
                limit=500,
                offset=offset,
                scope=self.scope,
            )
            for neighbor, edge in page:
                if neighbor.spec.name in public:
                    self._nodes[(neighbor.spec.name, neighbor.key)] = neighbor
                    yield neighbor, edge
            if len(page) < 500:
                break
            offset += len(page)

    def edges(self, relationships=None, *, labels=None):
        """Read public edges in pages; Store checks both endpoint scopes."""
        public = {spec.name for spec in self.specs(labels)}
        for spec in self.store.schema.edges:
            if spec.source.name not in public or spec.target.name not in public:
                continue
            if relationships is not None and spec.relationship not in relationships:
                continue
            offset = 0
            while True:
                page = self.store.list_edges(
                    spec, limit=500, offset=offset, scope=self.scope
                )
                yield from page
                if len(page) < 500:
                    break
                offset += len(page)
