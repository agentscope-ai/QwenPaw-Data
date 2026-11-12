"""Exercise embedding orchestration with scoped reads and transactional writes."""

from context_manager.auth import AccessMode, GraphScopeAuthority, WriteOwner
from context_manager.schema import ENTITY, GRAPH_SCHEMA
from context_manager.store import NodeRecord

from cm.infrastructure.access.support import authorization


def node(
    key="ent-1", *, spec=ENTITY, owner="user-1", workspace="workspace-1", **values
):
    return NodeRecord(
        spec,
        {
            "key": key,
            "owner_id": owner,
            "workspace_id": workspace,
            "zone": spec.zone.value,
            **values,
        },
    )


class MemoryStore:
    schema = GRAPH_SCHEMA

    def __init__(self, *nodes):
        self.access = GraphScopeAuthority()
        self.nodes = {(item.spec, item.key): item for item in nodes}
        self.scopes = []
        self.pages = []
        self.writes = []
        self.in_transaction = False
        self.before_atomic = None
        self.fail_key = None

    def scope(self, user="user-1", *, workspace="workspace-1"):
        return self.access.issue_write_scope(
            authorization(user_id=user, workspace_id=workspace),
            owner=WriteOwner.USER,
        )

    def list_nodes(self, spec, *, filters, limit, offset, fields=None, scope):
        self.access.require(scope, AccessMode.READ)
        self.scopes.append(scope)
        self.pages.append((spec, filters, limit, offset))
        assert not self.in_transaction
        records = sorted(
            (
                item
                for item in self.nodes.values()
                if item.spec is spec
                and item.properties["workspace_id"] == scope.workspace_id
                and all(item.properties.get(k) == v for k, v in filters.items())
            ),
            key=lambda item: item.key,
        )
        return [
            item
            if fields is None
            else NodeRecord(
                item.spec,
                {name: item.properties.get(name) for name in ("key", *fields)},
            )
            for item in records[offset : offset + limit]
        ]

    def get_nodes(self, spec, keys, *, scope):
        results = []
        for key in keys:
            self.access.require(scope, AccessMode.READ)
            results.append(self.nodes.get((spec, key)))
        return tuple(results)

    def patch_nodes(self, spec, patches, *, scope):
        for key, changes in patches:
            assert self.in_transaction
            self.access.require(scope, AccessMode.WRITE)
            assert set(changes) == {"embedding", "embedding_hash"}
            self.schema.validate_operation(spec, "update", changes)
            self.schema.validate_values(spec, changes)
            if key == self.fail_key:
                raise RuntimeError("database error")
            current = self.nodes.get((spec, key))
            if current is None:
                continue
            assert current.properties["owner_id"] in scope.owner_ids
            self.writes.append((spec, key, changes))
            self.nodes[spec, key] = NodeRecord(spec, {**current.properties, **changes})

    def atomic(self, work):
        if self.before_atomic:
            self.before_atomic()
        before = dict(self.nodes)
        self.in_transaction = True
        try:
            return work(self)
        except BaseException:
            self.nodes = before
            raise
        finally:
            self.in_transaction = False


from cm.knowledge.support import MemoryGraphStore


class RecoveryMemoryStore(MemoryGraphStore):
    """Full finite CRUD for recovery, seeded with embedding fixtures."""

    def __init__(self, *nodes):
        super().__init__()
        self.nodes.update(
            {(node.spec, node.key): dict(node.properties) for node in nodes}
        )
        self.scopes = []

    def list_nodes(self, spec, *, scope, **kwargs):
        if spec.embedding_fields:
            self.scopes.append(scope)
        return super().list_nodes(spec, scope=scope, **kwargs)
