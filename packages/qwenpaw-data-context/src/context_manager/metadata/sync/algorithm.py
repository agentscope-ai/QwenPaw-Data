"""Compare authoritative Metadata with one user's mirror, without graph I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from ...schema import KeySpec, PropertyOwner
from ...store import EdgeRecord, NodeRecord


# Vectors, ownership and provenance do not participate in userspace conflicts.
_CONTENT_EXCLUDES = frozenset(
    {
        "key",
        "workspace_id",
        "owner_id",
        "zone",
        "embedding",
        "embedding_hash",
        "signature_emb",
        "query_emb",
        "query_embedding",
        "strategy_vec",
        "mg_origin",
        "mg_keep_user_digest",
        "card_content_hash",
        "card_client_id",
        "feedback_source",
        "feedback_extra",
        "source",
    }
)

EdgeIdentity = tuple[str, str, str]


def logical_key(node: NodeRecord) -> str:
    """Match the same business node across owner-qualified Store keys."""
    key_spec = node.spec.property("key")
    assert isinstance(key_spec, KeySpec)
    parts = [
        node.properties[name]
        for name in key_spec.parts
        if name not in {"owner_id", "workspace_id"}
    ]
    return json.dumps(
        [node.spec.name, *parts], ensure_ascii=False, separators=(",", ":")
    )


def content(node: NodeRecord) -> dict[str, object]:
    """Return comparable business facts without ownership, vectors or provenance."""
    return {
        name: value
        for name, value in node.properties.items()
        if (
            name not in _CONTENT_EXCLUDES
            or (name == "source" and not node.spec.property(name).nullable)
        )
        and value is not None
    }


def content_digest(node: NodeRecord) -> str:
    return hashlib.sha256(
        json.dumps(
            content(node),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def mirror_properties(node: NodeRecord) -> dict[str, object]:
    """Copy schema facts without authoritative ownership, vectors or provenance."""
    return {
        prop.name: node.properties[prop.name]
        for prop in node.spec.properties
        if prop.owner is PropertyOwner.CALLER
        and (prop.name not in _CONTENT_EXCLUDES or not prop.nullable)
        and prop.name in node.properties
    }


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    """Keep the source facts used for both the digest and the sync decisions."""

    nodes: tuple[NodeRecord, ...]
    edges: tuple[EdgeRecord, ...]

    def indexed_nodes(self) -> dict[str, NodeRecord]:
        return {logical_key(node): node for node in self.nodes}

    def indexed_edges(self) -> dict[EdgeIdentity, EdgeRecord]:
        keys = {node.key: logical_key(node) for node in self.nodes}
        return {
            (keys[edge.source_key], edge.spec.relationship, keys[edge.target_key]): edge
            for edge in self.edges
        }

    def digest(self) -> str:
        facts = {
            "nodes": sorted((logical_key(node), content(node)) for node in self.nodes),
            "edges": sorted(self.indexed_edges()),
        }
        return hashlib.sha256(
            json.dumps(
                facts,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class MetadataSyncState:
    """Track adopted identities and deferred user decisions, not graph locks."""

    last_synced_digest: str | None = None
    last_synced_at: str | None = None
    mirrored_nodes: frozenset[str] = frozenset()
    mirrored_edges: frozenset[EdgeIdentity] = frozenset()
    kept_user_digests: Mapping[str, str] = field(default_factory=dict)

    def output(self) -> str:
        return json.dumps(
            {
                "last_synced_digest": self.last_synced_digest,
                "last_synced_at": self.last_synced_at,
                "mirrored_nodes": sorted(self.mirrored_nodes),
                "mirrored_edges": sorted(self.mirrored_edges),
                "kept_user_digests": dict(self.kept_user_digests),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def load(cls, value: str) -> MetadataSyncState:
        data = json.loads(value)
        return cls(
            last_synced_digest=data["last_synced_digest"],
            last_synced_at=data["last_synced_at"],
            mirrored_nodes=frozenset(data["mirrored_nodes"]),
            mirrored_edges=frozenset(tuple(edge) for edge in data["mirrored_edges"]),
            kept_user_digests=data["kept_user_digests"],
        )


@dataclass(frozen=True, slots=True)
class MetadataSyncDiff:
    """Describe business changes before compiling Store operations."""

    nodes_to_merge: tuple[tuple[NodeRecord, NodeRecord | None], ...]
    nodes_to_delete: tuple[NodeRecord, ...]
    nodes_to_adopt: tuple[NodeRecord, ...]
    edges_to_create: tuple[EdgeRecord, ...]
    edges_to_delete: tuple[EdgeRecord, ...]
    edges_to_adopt: tuple[EdgeRecord, ...]
    conflicts: tuple[tuple[NodeRecord, NodeRecord], ...]

    def conflict_details(self) -> list[dict[str, object]]:
        return [
            {
                "kind": "node",
                "label": source.spec.name,
                "key": source.key,
                "global_props": content(source),
                "user_props": content(personal),
            }
            for source, personal in self.conflicts
        ]


def compare_metadata(
    source: MetadataSnapshot,
    personal: MetadataSnapshot,
    state: MetadataSyncState,
) -> MetadataSyncDiff:
    """Preserve additions, replace mirrors, and defer conflicting user nodes."""
    source_nodes, personal_nodes = source.indexed_nodes(), personal.indexed_nodes()
    merges, adopts, conflicts = [], [], []
    conflict_keys = set()
    for identity, node in source_nodes.items():
        current = personal_nodes.get(identity)
        if current is None:
            merges.append((node, None))
        elif identity in state.mirrored_nodes:
            if content(current) != content(node):
                merges.append((node, current))
        elif content(current) == content(node):
            adopts.append(current)
        elif state.kept_user_digests.get(identity) != content_digest(node):
            conflicts.append((node, current))
            conflict_keys.add(identity)
    deleted_nodes = tuple(
        node
        for identity, node in personal_nodes.items()
        if identity in state.mirrored_nodes and identity not in source_nodes
    )

    source_edges, personal_edges = source.indexed_edges(), personal.indexed_edges()
    created_edges, adopted_edges = [], []
    for identity, edge in source_edges.items():
        if identity[0] in conflict_keys or identity[2] in conflict_keys:
            continue
        if identity not in personal_edges:
            created_edges.append(edge)
        elif identity not in state.mirrored_edges:
            adopted_edges.append(personal_edges[identity])
    deleted_edges = tuple(
        edge
        for identity, edge in personal_edges.items()
        if identity in state.mirrored_edges and identity not in source_edges
    )
    return MetadataSyncDiff(
        tuple(merges),
        deleted_nodes,
        tuple(adopts),
        tuple(created_edges),
        deleted_edges,
        tuple(adopted_edges),
        tuple(conflicts),
    )
