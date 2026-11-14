"""Expose develop's Graph Center queries through the current Store/Scope boundary."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from ...executor import BoundedExecutor
from ...schema import DOMAIN
from . import details, retrieval
from .access import ExplorerAccess
from .display import json_value
from .ds_ownership import node_in_datasource


class ExplorerService:
    """Select a scoped query context; query algorithms live in the copied modules."""

    def __init__(self, store, *, executor: BoundedExecutor):
        self.store = store
        self.executor = executor

    def access(self, scope):
        return ExplorerAccess(self.store, scope)

    def _project(self, scope, result):
        """Translate persisted domain IDs into the display names used by the UI."""
        result = deepcopy(result)
        nodes = result if isinstance(result, list) else result.get("nodes", [])
        if any(n.get("props", {}).get("domain") for n in nodes):
            domains = self.store.list_nodes(DOMAIN, scope=scope, limit=1000)
            names = {
                (d.properties["owner_id"], d.properties["domain_id"]): d.properties[
                    "name"
                ]
                for d in domains
            }
            for n in nodes:
                props = n.get("props", {})
                if props.get("zone") == "metadata" and props.get("domain"):
                    props["domain"] = names.get(
                        (props.get("owner_id"), props["domain"]), props["domain"]
                    )
        return json_value(result)

    def global_graph(self, scope, **parameters):
        datasource_id = (parameters.get("datasource_id") or "").strip()
        parameters["datasource_id"] = datasource_id
        result = retrieval.global_graph_snapshot(self.access(scope), **parameters)
        if datasource_id:
            # Develop also filters the materialized canvas, including the
            # Domain/DataSource entry branch that does not use Dataset closure.
            nodes = []
            surviving_keys = set()
            for node in result.get("nodes", []):
                label = node.get("group") or node.get("label") or ""
                if label in retrieval._METADATA_LAYER_LABELS:
                    props = dict(node.get("properties") or node.get("props") or node)
                    if not props.get("key"):
                        props["key"] = node.get("key") or node.get("id") or ""
                    if not node_in_datasource(props, datasource_id):
                        continue
                nodes.append(node)
                key = node.get("key") or node.get("id") or ""
                if key:
                    surviving_keys.add(key)
            edges = [
                edge
                for edge in result.get("edges", [])
                if (edge.get("source_key") or edge.get("from") or "") in surviving_keys
                and (edge.get("target_key") or edge.get("to") or "") in surviving_keys
            ]
            result = {"nodes": nodes, "edges": edges}
        return self._project(scope, result)

    def domain_graph(self, scope, domain, datasource_id=""):
        try:
            return self._project(
                scope, retrieval.domain_graph_snapshot(self.access(scope), domain)
            )
        except ValueError as exc:
            raise LookupError(str(exc)) from exc

    def expand(self, scope, key, *, limit=50, direction="both", zone=None, label=None):
        access = self.access(scope)
        try:
            if direction == "both":
                result = retrieval.expand_node_snapshot(access, key, max_edges=limit)
            else:
                result = retrieval.expand_node_layer(
                    access,
                    key,
                    direction="down" if direction in {"out", "down"} else "up",
                    max_edges=limit,
                )
        except ValueError as exc:
            raise LookupError(str(exc)) from exc
        result = details._filter_expand_result(result, zone=zone, label=label)
        return self._project(scope, result)

    def search_nodes(self, scope, query, limit=50):
        return self._project(
            scope,
            retrieval.search_explorer_nodes(self.access(scope), query, limit=limit),
        )

    def search_subgraph(self, scope, query, *, zones, exact=False, hops=1, limit=50):
        labels = tuple(
            n.name
            for n in self.store.schema.nodes
            if not n.internal and n.zone.value in zones
        )
        if not labels:
            return {"hit_nodes": [], "nodes": [], "edges": []}
        return retrieval.search_explorer_subgraph(
            self.access(scope),
            query,
            allowed_labels=labels,
            exact=exact,
            hops=hops,
            limit=limit,
        )

    def schema_summary(self, scope):
        return details._schema_summary_sync(self.access(scope))

    def cross_graph(self, scope, key, limit=50):
        return details._cross_graph_neighbors_sync(key, self.access(scope), limit)

    def node_detail(self, scope, key):
        result = details._node_detail_sync(key, self.access(scope))
        projected = self._project(scope, [{"props": result["properties"]}])
        result["properties"] = projected[0]["props"]
        return result

    def edge_detail(self, scope, source_key, target_key, rel_type):
        body = SimpleNamespace(
            source_key=source_key, target_key=target_key, rel_type=rel_type
        )
        return details._edge_detail_sync(body, self.access(scope))
