"""Read User Graph views and compile user-authored edits into Store transactions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from ..schema import HAS_MEMORY, USER, USER_MEMORY, EdgeSpec, NodeSpec
from ..auth import AccessMode, ScopeBinding
from ..store import (
    EdgeRecord,
    GraphStore,
    NeighborDirection,
    NodeDraft,
    NodeRecord,
)
from ..transaction import (
    StoreOperation,
    TransactionPlan,
    TransactionResult,
    execute_transaction_plan,
)
from ..utils.batches import grouped
from ..utils.values import normalize_text
from .algo.deduplicator import user_memory_id

_PAGE_SIZE = 500
_PROFILE_FIELDS = ("display_name", "description", "locale", "timezone")
_MEMORY_VIEW_FIELDS = (
    "id",
    "workspace_id",
    "owner_id",
    "type",
    "description",
    "source_type",
    "status",
    "created_at",
    "updated_at",
    "source_ref",
    "source_refs",
    "triggers",
)
_TYPE_DESCRIPTIONS = {
    "profile": "当前用户的稳定身份、角色、职责、语言和时区信息。",
    "principle": "Agent 在所有协作中必须遵守的用户长期硬性规则。",
    "preference": "Agent 默认遵循、但可被当前明确指令覆盖的用户长期软偏好。",
}


class UserGraphError(Exception):
    """A public business failure whose code is translated by the HTTP adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MemoryCrudService:
    """Manage profiles and explicit memories without coupling business logic to HTTP."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._reference_edges = {
            edge.target.name: edge
            for edge in store.schema.edges
            if edge.source is USER_MEMORY and edge.relationship == "REFERS_TO"
        }

    def get_user(self, user_id: str, scope: ScopeBinding) -> dict[str, object] | None:
        user = self._active_user(user_id, scope)
        return self._user_view(user) if user is not None else None

    def get_global_memories(self, scope: ScopeBinding) -> dict[str, object]:
        """Read the caller's active profile and attached long-term memories."""
        result = {
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
            "profile": None,
            "principles": [],
            "preferences": [],
            "type_descriptions": dict(_TYPE_DESCRIPTIONS),
        }
        user = self._find(
            USER, {"user_id": scope.user_id, "owner_id": scope.user_id}, scope
        )
        if user is None or user.properties.get("status") != "active":
            return result
        profile = self._user_view(user)["profile"]
        result["profile"] = {
            key: value for key, value in (profile or {}).items() if value
        } or None
        memories = []
        offset = 0
        while True:
            batch = self._store.expand_neighbors(
                USER,
                user.key,
                direction=NeighborDirection.OUTGOING,
                limit=_PAGE_SIZE,
                offset=offset,
                node_fields=(
                    "id",
                    "owner_id",
                    "status",
                    "type",
                    "description",
                    "created_at",
                ),
                edge_fields=(),
                scope=scope,
            )
            memories.extend(
                node
                for node, edge in batch
                if edge.spec.relationship == HAS_MEMORY.relationship
                and node.spec is USER_MEMORY
                and node.properties["owner_id"] == scope.user_id
                and node.properties.get("status") == "active"
                and node.properties.get("type") in {"principle", "preference"}
            )
            offset += len(batch)
            if len(batch) < _PAGE_SIZE:
                break
        memories.sort(key=lambda node: str(node.properties["id"]))
        memories.sort(key=lambda node: self._timestamp_order(node, "created_at"))
        for memory_type, field in (
            ("principle", "principles"),
            ("preference", "preferences"),
        ):
            selected = [
                node for node in memories if node.properties["type"] == memory_type
            ]
            result[field] = [
                {name: node.properties[name] for name in ("id", "type", "description")}
                for node in selected
            ]
        return result

    def list_users(
        self, scope: ScopeBinding, *, page: int, page_size: int
    ) -> tuple[list[dict[str, object]], int]:
        users = [
            node
            for node in self._nodes(USER, {}, scope)
            if node.properties.get("user_id") == node.properties.get("owner_id")
            and node.properties.get("status") == "active"
        ]
        users.sort(key=lambda node: (str(node.properties["user_id"]), node.key))
        users.sort(
            key=lambda node: self._timestamp_order(node, "updated_at"), reverse=True
        )
        offset = (page - 1) * page_size
        return [
            self._user_view(node) for node in users[offset : offset + page_size]
        ], len(users)

    def list_memories(
        self,
        user_id: str,
        scope: ScopeBinding,
        *,
        page: int,
        page_size: int,
        memory_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        memories = [
            node
            for node in self._owned_memories(user_id, scope, fields=_MEMORY_VIEW_FIELDS)
            if (memory_type is None or node.properties.get("type") == memory_type)
            and (
                node.properties.get("status") == status
                if status is not None
                else node.properties.get("status") in {"active", "needs_review"}
            )
        ]
        if q and q.strip():
            query = self._normalized(q)
            memories = [
                node
                for node in memories
                if any(
                    query in str(value).casefold()
                    for value in (
                        node.properties.get("description", ""),
                        *(node.properties.get("triggers") or ()),
                    )
                )
            ]
        memories.sort(key=lambda node: (str(node.properties["id"]), node.key))
        memories.sort(
            key=lambda node: self._timestamp_order(node, "updated_at"), reverse=True
        )
        offset = (page - 1) * page_size
        return [
            self._memory_view(node, scope)
            for node in memories[offset : offset + page_size]
        ], len(memories)

    def get_memory(
        self, user_id: str, memory_id: str, scope: ScopeBinding
    ) -> dict[str, object]:
        return self._memory_view(
            self._memory(user_id, memory_id, scope, fields=_MEMORY_VIEW_FIELDS),
            scope,
        )

    def patch_profile(
        self, patch: Mapping[str, object], scope: ScopeBinding
    ) -> dict[str, object]:
        self._require_write(scope)
        current = self._active_user(scope.user_id, scope)
        if current is None:
            raise UserGraphError("not_found", "User Graph user not found")
        raw = json.loads(current.properties.get("profile") or "{}")
        profile = {field: raw[field] for field in _PROFILE_FIELDS if raw.get(field)}
        timestamp = datetime.now(UTC)
        for field, value in patch.items():
            if value is None:
                profile.pop(field, None)
            else:
                profile[field] = value
        clocks = dict(raw.get("_evidence_at") or {})
        if clocks:
            clocks.update(
                {field: timestamp.isoformat(timespec="microseconds") for field in patch}
            )
            profile["_evidence_at"] = clocks
        properties = {
            "profile": json.dumps(profile, ensure_ascii=False) if profile else None,
            "updated_at": timestamp,
        }
        operations = [
            StoreOperation("get_nodes", {"spec": USER, "keys": (current.key,)}),
            StoreOperation(
                "patch_nodes", {"spec": USER, "patches": ((current.key, properties),)}
            ),
            StoreOperation("get_nodes", {"spec": USER, "keys": (current.key,)}),
        ]
        result = self._execute(operations, {0: (current,)}, scope)
        return self._user_view(result.operation_results[-1][0])

    def create_memory(
        self,
        memory: Mapping[str, object],
        read_scope: ScopeBinding,
        write_scope: ScopeBinding,
        *,
        references: Sequence[tuple[str, str]] = (),
    ) -> dict[str, object]:
        """Create memory properties and references addressed by (label, key)."""
        self._require_write(write_scope, read_scope)
        memory = self._memory_properties(memory)
        self._validate_memory(memory, references)
        user = self._active_user(write_scope.user_id, read_scope)
        if user is None:
            raise UserGraphError("not_found", "User Graph user not found")
        existing = self._owned_memories(write_scope.user_id, read_scope)
        current = self._equivalent(existing, memory["type"], memory["description"])
        memory_id = user_memory_id(
            write_scope.workspace_id,
            write_scope.user_id,
            memory["type"],
            memory["description"],
        )
        if current is None:
            current = next(
                (node for node in existing if node.properties["id"] == memory_id), None
            )
        if current is not None and self._signature(
            current.properties
        ) != self._signature(memory):
            raise UserGraphError(
                "conflict", "memory id already has different semantic content"
            )
        targets = self._resolve_references(references, read_scope, write_scope)
        old_edges = (
            [] if current is None else self._reference_records(current, read_scope)
        )
        # Repeated creates add evidence and references; PATCH replaces the supplied reference set.
        desired = {(edge, key) for edge, key in targets}
        desired.update((edge.spec, edge.target_key) for edge in old_edges)
        properties = {
            "id": str(current.properties["id"]) if current else memory_id,
            "type": memory["type"],
            "description": (
                current.properties["description"] if current else memory["description"]
            ),
            "triggers": tuple(
                self._normalize_triggers(
                    [
                        *(current.properties.get("triggers") or () if current else ()),
                        *memory["triggers"],
                    ]
                )
            ),
        }
        if len(properties["triggers"]) > 10:
            raise UserGraphError(
                "invalid_memory", "a memory can contain at most 10 triggers"
            )
        return self._save_memory(
            properties,
            current,
            old_edges,
            desired,
            read_scope,
            write_scope,
            action="merged" if current else "created",
        )

    def patch_memory(
        self,
        memory_id: str,
        patch: Mapping[str, object],
        read_scope: ScopeBinding,
        write_scope: ScopeBinding,
        *,
        references: Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        """Patch supplied properties; None preserves references, an empty list clears them."""
        self._require_write(write_scope, read_scope)
        current = self._memory(write_scope.user_id, memory_id, read_scope)
        if current.properties.get("status") == "revoked":
            raise UserGraphError("conflict", "revoked memories cannot be edited")
        old_edges = self._reference_records(current, read_scope)
        data = {
            "type": current.properties["type"],
            "description": current.properties["description"],
            "triggers": list(current.properties.get("triggers") or ()),
            **patch,
        }
        targets = (
            references
            if references is not None
            else [(edge.spec.target.name, edge.target_key) for edge in old_edges]
        )
        if "type" in patch:
            if (
                data["type"] != "trigger"
                and current.properties["type"] == "trigger"
                and "triggers" not in patch
            ):
                data["triggers"] = []
            if data["type"] in {"principle", "preference"} and references is None:
                targets = []
        memory = self._memory_properties(data)
        self._validate_memory(memory, targets)
        candidates = self._owned_memories(write_scope.user_id, read_scope)
        if (
            self._equivalent(
                [node for node in candidates if node.key != current.key],
                memory["type"],
                memory["description"],
            )
            is not None
        ):
            raise UserGraphError(
                "conflict", "another memory already has the same type and description"
            )
        targets = self._resolve_references(targets, read_scope, write_scope)
        properties = {
            "id": memory_id,
            "type": memory["type"],
            "description": memory["description"],
            "triggers": tuple(memory["triggers"]),
        }
        return self._save_memory(
            properties,
            current,
            old_edges,
            set(targets),
            read_scope,
            write_scope,
            action="updated",
        )

    def revoke_memory(
        self, memory_id: str, read_scope: ScopeBinding, write_scope: ScopeBinding
    ) -> dict[str, object]:
        self._require_write(write_scope, read_scope)
        current = self._memory(write_scope.user_id, memory_id, read_scope)
        revoked_at = datetime.now(UTC)
        operations = [
            StoreOperation("get_nodes", {"spec": USER_MEMORY, "keys": (current.key,)}),
            StoreOperation(
                "patch_nodes",
                {
                    "spec": USER_MEMORY,
                    "patches": (
                        (
                            current.key,
                            {
                                "status": "revoked",
                                "updated_at": revoked_at,
                                "evidence_at": revoked_at.isoformat(
                                    timespec="microseconds"
                                ),
                                **self._manual_evidence(
                                    current, write_scope.user_id, "revoke-memory"
                                ),
                            },
                        ),
                    ),
                },
            ),
            StoreOperation("get_nodes", {"spec": USER_MEMORY, "keys": (current.key,)}),
        ]
        result = self._execute(operations, {0: (current,)}, write_scope)
        return {
            **self._memory_view(result.operation_results[-1][0], read_scope),
            "action": "revoked",
        }

    def _save_memory(
        self,
        properties: dict[str, object],
        current: NodeRecord | None,
        old_edges: list[EdgeRecord],
        desired: set[tuple[EdgeSpec, str]],
        read_scope: ScopeBinding,
        write_scope: ScopeBinding,
        *,
        action: str,
    ) -> dict[str, object]:
        user_identity = {
            "user_id": write_scope.user_id,
            "owner_id": write_scope.user_id,
        }
        user = self._active_user(write_scope.user_id, read_scope)
        if user is None:
            raise UserGraphError("not_found", "User Graph user not found")
        ownership = None
        if current is not None:
            ownership = self._store.get_edges(
                HAS_MEMORY, ((user.key, current.key),), scope=read_scope
            )[0]
        timestamp = datetime.now(UTC)
        properties.update(
            **self._manual_evidence(
                current,
                write_scope.user_id,
                "patch-memory" if action == "updated" else "create-memory",
            ),
            status=current.properties["status"] if action == "updated" else "active",
            updated_at=timestamp,
        )
        if current is None:
            properties["created_at"] = timestamp
        draft = NodeDraft.build(self._store.schema, USER_MEMORY, "memory", properties)
        operations = [
            StoreOperation(
                "list_nodes", {"spec": USER, "filters": user_identity, "limit": 2}
            )
        ]
        checks: dict[int, object] = {0: (user,)}
        memory_key = current.key if current is not None else None
        if current is None:
            operations.append(StoreOperation.project_nodes((draft,)))
        else:
            checks[len(operations)] = (current,)
            operations.append(
                StoreOperation(
                    "get_nodes",
                    {"spec": USER_MEMORY, "keys": (current.key,)},
                )
            )
            operations.append(
                StoreOperation(
                    "patch_nodes",
                    {
                        "spec": USER_MEMORY,
                        "patches": (
                            (
                                current.key,
                                {
                                    name: value
                                    for name, value in draft.output().items()
                                    if USER_MEMORY.property(name).editable
                                },
                            ),
                        ),
                    },
                )
            )
        if current is not None:
            checks[len(operations)] = (ownership,)
            operations.append(
                StoreOperation(
                    "get_edges",
                    {"spec": HAS_MEMORY, "endpoints": ((user.key, current.key),)},
                )
            )
        if ownership is None:
            operations.append(
                StoreOperation(
                    "create_edges",
                    {
                        "fields": (),
                        "spec": HAS_MEMORY,
                        "edges": ((user.key, memory_key, {}),),
                    },
                    node_key_refs={("edges", 0, 1): "memory"}
                    if current is None
                    else {},
                )
            )
        previous = {(edge.spec, edge.target_key) for edge in old_edges}
        for edge in old_edges:
            checks[len(operations)] = (edge,)
            operations.append(
                StoreOperation(
                    "get_edges",
                    {
                        "spec": edge.spec,
                        "endpoints": ((edge.source_key, edge.target_key),),
                    },
                )
            )
        for spec, key in sorted(
            previous - desired, key=lambda item: (item[0].target.name, item[1])
        ):
            operations.append(
                StoreOperation(
                    "delete_edges",
                    {"spec": spec, "endpoints": ((memory_key, key),)},
                )
            )
        for spec, key in sorted(
            desired - previous, key=lambda item: (item[0].target.name, item[1])
        ):
            operations.append(
                StoreOperation(
                    "create_edges",
                    {"fields": (), "spec": spec, "edges": ((memory_key, key, {}),)},
                    node_key_refs={("edges", 0, 0): "memory"}
                    if current is None
                    else {},
                )
            )
        operations.append(
            StoreOperation(
                "get_nodes",
                {"spec": USER_MEMORY, "keys": (memory_key,)},
                node_key_refs={("keys", 0): "memory"} if current is None else {},
            )
        )
        result = self._execute(operations, checks, write_scope)
        view = self._memory_view(result.operation_results[-1][0], read_scope)
        return {**view, "action": action}

    def _execute(
        self,
        operations: list[StoreOperation],
        checks: Mapping[int, object],
        scope: ScopeBinding,
    ) -> TransactionResult:
        def validate(result: TransactionResult) -> None:
            for index, expected in checks.items():
                if result.operation_results[index] != expected:
                    raise UserGraphError(
                        "conflict", "User Graph changed; reload and retry"
                    )

        return execute_transaction_plan(
            self._store, TransactionPlan(tuple(operations)), scope, validate=validate
        )

    def _nodes(
        self, spec: NodeSpec, filters: Mapping[str, object], scope: ScopeBinding
    ) -> list[NodeRecord]:
        nodes: list[NodeRecord] = []
        while True:
            batch = self._store.list_nodes(
                spec, filters=filters, offset=len(nodes), limit=_PAGE_SIZE, scope=scope
            )
            nodes.extend(batch)
            if len(batch) < _PAGE_SIZE:
                return nodes

    def _find(
        self,
        spec: NodeSpec,
        identity: Mapping[str, object],
        scope: ScopeBinding,
        *,
        fields: Sequence[str] | None = None,
    ) -> NodeRecord | None:
        nodes = self._store.list_nodes(
            spec, filters=identity, limit=2, fields=fields, scope=scope
        )
        if len(nodes) > 1:
            raise UserGraphError("conflict", "User Graph identity is not unique")
        return nodes[0] if nodes else None

    def _memory(
        self,
        user_id: str,
        memory_id: str,
        scope: ScopeBinding,
        *,
        fields: Sequence[str] | None = None,
    ) -> NodeRecord:
        user = self._active_user(user_id, scope)
        node = self._find(
            USER_MEMORY, {"id": memory_id, "owner_id": user_id}, scope, fields=fields
        )
        if (
            user is None
            or node is None
            or self._store.get_edges(HAS_MEMORY, ((user.key, node.key),), scope=scope)[
                0
            ]
            is None
        ):
            raise UserGraphError("not_found", "memory not found")
        return node

    def _active_user(self, user_id: str, scope: ScopeBinding) -> NodeRecord | None:
        node = self._find(USER, {"user_id": user_id, "owner_id": user_id}, scope)
        return (
            node
            if node is not None and node.properties.get("status") == "active"
            else None
        )

    def _owned_memories(
        self,
        user_id: str,
        scope: ScopeBinding,
        *,
        fields: Sequence[str] | None = None,
    ) -> list[NodeRecord]:
        user = self._active_user(user_id, scope)
        if user is None:
            return []
        result = []
        offset = 0
        while True:
            batch = self._store.expand_neighbors(
                USER,
                user.key,
                direction=NeighborDirection.OUTGOING,
                limit=_PAGE_SIZE,
                offset=offset,
                node_fields=fields,
                edge_fields=(),
                scope=scope,
            )
            result.extend(
                node
                for node, edge in batch
                if edge.spec.relationship == HAS_MEMORY.relationship
                and node.spec is USER_MEMORY
                and node.properties.get("owner_id") == user_id
            )
            offset += len(batch)
            if len(batch) < _PAGE_SIZE:
                return result

    @classmethod
    def _equivalent(
        cls, nodes: list[NodeRecord], memory_type: str, description: str
    ) -> NodeRecord | None:
        signature = cls._signature({"type": memory_type, "description": description})
        matches = [
            node
            for node in nodes
            if node.properties.get("status") in {"active", "needs_review"}
            and cls._signature(node.properties) == signature
        ]
        matches.sort(key=lambda node: str(node.properties["id"]))
        matches.sort(
            key=lambda node: cls._timestamp_order(node, "updated_at"), reverse=True
        )
        return matches[0] if matches else None

    @staticmethod
    def _timestamp_order(node: NodeRecord, field: str) -> tuple[bool, str]:
        value = node.properties.get(field)
        if value is None:
            return True, ""
        text = value.isoformat() if hasattr(value, "isoformat") else str(value)
        return False, datetime.fromisoformat(text).astimezone(UTC).isoformat()

    @staticmethod
    def _manual_evidence(
        current: NodeRecord | None, user_id: str, action: str
    ) -> dict[str, object]:
        properties = current.properties if current is not None else {}
        refs = properties.get("source_refs") or (
            (properties["source_ref"],) if properties.get("source_ref") else ()
        )
        source_ref = f"user-graph-api:{action}:{user_id}:{uuid4().hex}"
        return {
            "source_type": "user_explicit",
            "source_ref": source_ref,
            "source_refs": tuple(dict.fromkeys((*refs, source_ref))),
        }

    @staticmethod
    def _user_view(node: NodeRecord) -> dict[str, object]:
        raw = node.properties.get("profile")
        profile = json.loads(raw) if raw else {}
        profile = {name: profile[name] for name in _PROFILE_FIELDS if profile.get(name)}
        return {
            "workspace_id": node.properties["workspace_id"],
            "user_id": node.properties["user_id"],
            "profile": (
                {name: profile.get(name) for name in _PROFILE_FIELDS}
                if profile
                else None
            ),
            "status": node.properties.get("status") or "active",
            "created_at": node.properties.get("created_at"),
            "updated_at": node.properties.get("updated_at"),
        }

    def _memory_view(self, node: NodeRecord, scope: ScopeBinding) -> dict[str, object]:
        references: list[dict[str, object]] = []
        offset = 0
        while node.properties["type"] in {"anchored_rule", "trigger"}:
            batch = self._store.expand_neighbors(
                USER_MEMORY,
                node.key,
                direction=NeighborDirection.OUTGOING,
                limit=_PAGE_SIZE,
                offset=offset,
                node_fields=("name", "canonical_name"),
                edge_fields=(),
                scope=scope,
            )
            for target, edge in batch:
                if (
                    edge.spec.relationship == "REFERS_TO"
                    and target.spec.name in self._reference_edges
                ):
                    references.append(
                        {
                            "graph": target.spec.zone.value,
                            "label": target.spec.name,
                            "key": target.key,
                            "name": target.properties.get("name")
                            or target.properties.get("canonical_name")
                            or target.key,
                        }
                    )
            offset += len(batch)
            if len(batch) < _PAGE_SIZE:
                break
        refs = list(node.properties.get("source_refs") or ())
        return {
            **{
                name: node.properties.get(name)
                for name in (
                    "id",
                    "workspace_id",
                    "owner_id",
                    "type",
                    "description",
                    "source_type",
                    "status",
                    "created_at",
                    "updated_at",
                )
            },
            "source_ref": node.properties.get("source_ref")
            or (refs[0] if refs else ""),
            "source_refs": refs,
            "triggers": list(node.properties.get("triggers") or ()),
            "references": references,
        }

    def _reference_records(
        self, node: NodeRecord, scope: ScopeBinding
    ) -> list[EdgeRecord]:
        edges: list[EdgeRecord] = []
        for spec in self._reference_edges.values():
            offset = 0
            while True:
                batch = self._store.list_edges(
                    spec,
                    filters={"owner_id": node.properties["owner_id"]},
                    limit=_PAGE_SIZE,
                    offset=offset,
                    scope=scope,
                )
                edges.extend(edge for edge in batch if edge.source_key == node.key)
                offset += len(batch)
                if len(batch) < _PAGE_SIZE:
                    break
        return edges

    def _resolve_references(
        self,
        references: Sequence[tuple[str, str]],
        read_scope: ScopeBinding,
        write_scope: ScopeBinding,
    ) -> list[tuple[EdgeSpec, str]]:
        references = tuple(dict.fromkeys(references))
        targets = {}
        for label, group in grouped(references, key=lambda reference: reference[0]):
            edge = self._reference_edges.get(label)
            if edge is None:
                raise UserGraphError("invalid_reference", "unsupported reference label")
            nodes = self._store.get_nodes(
                edge.target,
                tuple(key for _, key in group),
                fields=("owner_id",),
                scope=read_scope,
            )
            for reference, target in zip(group, nodes, strict=True):
                if target is None or target.properties.get("owner_id") not in {
                    write_scope.user_id,
                    write_scope.workspace_id,
                }:
                    raise UserGraphError(
                        "invalid_reference",
                        "reference is unavailable in this user's workspace",
                    )
                targets[reference] = (edge, target.key)
        return [targets[reference] for reference in references]

    def _require_write(
        self, scope: ScopeBinding, read_scope: ScopeBinding | None = None
    ) -> None:
        self._store.access.require(scope, AccessMode.WRITE)
        if (
            scope.write_owner_id != scope.user_id
            or scope.zone != "user"
            or scope.datasource_id is not None
        ):
            raise PermissionError("User Graph writes require the current user's scope")
        if read_scope is not None:
            self._store.access.require(read_scope, AccessMode.READ)
            if (scope.tenant_id, scope.workspace_id, scope.user_id) != (
                read_scope.tenant_id,
                read_scope.workspace_id,
                read_scope.user_id,
            ):
                raise PermissionError(
                    "User Graph read and write scopes must have the same target"
                )

    @staticmethod
    def _normalize_triggers(values: Sequence[str]) -> list[str]:
        result = {}
        for value in values:
            text = normalize_text(value)
            result.setdefault(text.casefold(), text)
        return list(result.values())

    @classmethod
    def _memory_properties(cls, values: Mapping[str, object]) -> dict[str, object]:
        return {
            "type": values["type"],
            "description": normalize_text(values["description"]),
            "triggers": cls._normalize_triggers(values.get("triggers") or ()),
        }

    @staticmethod
    def _validate_memory(
        memory: Mapping[str, object],
        references: Sequence[tuple[str, str]],
    ) -> None:
        memory_type = memory["type"]
        triggers = memory["triggers"]
        if (
            memory_type not in {"principle", "preference", "anchored_rule", "trigger"}
            or (memory_type == "trigger") != bool(triggers)
            or (memory_type == "anchored_rule" and not references)
            or (memory_type in {"principle", "preference"} and references)
            or len(triggers) > 10
        ):
            raise UserGraphError(
                "invalid_memory_shape",
                "memory type, triggers and references must remain consistent",
            )

    @staticmethod
    def _normalized(value: str) -> str:
        return normalize_text(value).casefold()

    @classmethod
    def _signature(cls, values: Mapping[str, object]) -> tuple[object, ...]:
        return (
            values.get("type"),
            cls._normalized(str(values.get("description", ""))),
        )
