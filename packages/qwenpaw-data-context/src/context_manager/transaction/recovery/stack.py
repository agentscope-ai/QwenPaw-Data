"""Persist graph-stack transitions and apply one atomic batch of Undo."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from ...auth import ScopeBinding
from ...schema import RECOVERY_STACK_HEAD, UNDO_BATCH
from ...settings import get_settings
from ...store import GraphStore, NodeRecord, StoreError
from .codec import decode


class RecoveryStack:
    """Own graph state only; callers coordinate SQL and asynchronous execution.

    Runtime supplies system capabilities for this exact Workspace. Undo records
    select an owner and datasource within that authorization; they grant none.
    """

    def __init__(
        self,
        store: GraphStore,
        rid: str,
        *,
        control_scope: ScopeBinding,
        read_scope: ScopeBinding,
        scope_for: Callable[[str, str | None], ScopeBinding],
    ) -> None:
        self.store = store
        self.rid = rid
        self.control_scope = control_scope
        self.read_scope = read_scope
        self.scope_for = scope_for
        self.key = RECOVERY_STACK_HEAD.property("key").generate(
            {
                "workspace_id": control_scope.workspace_id,
                "owner_id": control_scope.write_owner_id,
                "rid": rid,
            }
        )

    def create(
        self, *, state: str = "ACTIVE", result_status: str | None = None
    ) -> None:
        self.store.atomic(
            lambda tx: tx.create_nodes(
                RECOVERY_STACK_HEAD,
                (
                    {
                        "rid": self.rid,
                        "recovery_state": state,
                        "head_seq": 0,
                        "result_status": result_status,
                        "message": None,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                ),
                scope=self.control_scope,
            )
        )

    def head(self, store: GraphStore | None = None) -> NodeRecord | None:
        return (store or self.store).get_nodes(
            RECOVERY_STACK_HEAD, (self.key,), scope=self.control_scope
        )[0]

    def _lock(self, tx: GraphStore) -> NodeRecord:
        # The write obtains the node lock before reading mutable state.
        tx.patch_nodes(
            RECOVERY_STACK_HEAD,
            ((self.key, {"updated_at": datetime.now(UTC).isoformat()}),),
            scope=self.control_scope,
        )
        head = self.head(tx)
        if head is None:
            raise StoreError(
                "recovery head is missing",
                operation="recovery",
                code="recovery_head_missing",
            )
        return head

    def begin_batch(self, tx: GraphStore, sequence: int) -> None:
        head = self._lock(tx).properties
        if head["recovery_state"] != "ACTIVE":
            raise RuntimeError("only an active stack accepts a new batch")
        if sequence <= head["head_seq"]:
            raise StoreError(
                "this batch already committed; its original result is unavailable",
                operation="recovery",
                code="recovery_batch_committed",
            )
        if sequence != head["head_seq"] + 1:
            raise RuntimeError("recovery batch sequence is not contiguous")

    def append(self, tx: GraphStore, sequence: int, payload: str) -> None:
        """Record Undo and advance the head inside the locked business transaction."""
        tx.create_nodes(
            UNDO_BATCH,
            ({"stack_key": self.key, "sequence": sequence, "payload": payload},),
            scope=self.control_scope,
        )
        tx.patch_nodes(
            RECOVERY_STACK_HEAD,
            ((self.key, {"head_seq": sequence}),),
            scope=self.control_scope,
        )

    def decide(self, result_status: str, message: str | None) -> Mapping[str, object]:
        """Persist one immutable decision, including after a lost commit response."""

        def work(tx):
            head = self._lock(tx).properties
            if head["recovery_state"] != "ACTIVE":
                return head
            state = (
                "COMMITTED"
                if result_status in {"SUCCESS", "DEGRADED"}
                else "RECOVERING"
                if head["head_seq"]
                else "ROLLED_BACK"
            )
            changes = {
                "recovery_state": state,
                "result_status": result_status,
                "message": message,
            }
            tx.patch_nodes(
                RECOVERY_STACK_HEAD, ((self.key, changes),), scope=self.control_scope
            )
            return {**head, **changes}

        return self.store.atomic(work)

    def undo_batch(self, sequence: int) -> None:
        """Reverse a fixed batch and advance its cursor in the same transaction."""

        def work(tx):
            head = self._lock(tx).properties
            if sequence > head["head_seq"]:
                return  # The preceding attempt committed but lost its response.
            if (head["recovery_state"], head["head_seq"]) != ("RECOVERING", sequence):
                raise RuntimeError("recovery must process the current stack head")
            key = UNDO_BATCH.property("key").generate(
                {
                    "workspace_id": self.control_scope.workspace_id,
                    "owner_id": self.control_scope.write_owner_id,
                    "stack_key": self.key,
                    "sequence": sequence,
                }
            )
            batch = tx.get_nodes(UNDO_BATCH, (key,), scope=self.control_scope)[0]
            if batch is None:
                raise StoreError(
                    "recovery batch is missing",
                    operation="recovery",
                    code="recovery_batch_missing",
                )
            for entry in reversed(decode(batch.properties["payload"])):
                self._apply_undo(tx, entry)
            tx.patch_nodes(
                RECOVERY_STACK_HEAD,
                (
                    (
                        self.key,
                        {
                            "head_seq": sequence - 1,
                            "recovery_state": "ROLLED_BACK"
                            if sequence == 1
                            else "RECOVERING",
                        },
                    ),
                ),
                scope=self.control_scope,
            )

        self.store.atomic(work)

    def _apply_undo(self, tx: GraphStore, entry: Mapping[str, object]) -> None:
        name = entry["name"]
        if name not in {
            "create_nodes",
            "patch_nodes",
            "delete_nodes",
            "create_edges",
            "patch_edges",
            "delete_edges",
        }:
            raise ValueError("unsupported Undo operation")
        node = name.endswith("_nodes")
        spec = (
            tx.schema.node(entry["spec"])
            if node
            else tx.schema.edge(tuple(entry["spec"]))
        )
        description = entry["scope"]
        scope = self.scope_for(description["owner_id"], description["datasource_id"])
        identity = entry["identity"]
        if name == "create_edges":
            # Do not restore a duplicate endpoint identity after concurrent writes.
            record = tx.get_edges(
                spec, (tuple(identity),), fields=(), scope=self.read_scope
            )[0]
            if record is not None:
                raise ValueError("Undo cannot recreate an existing target")
        if name == "create_nodes":
            tx.create_nodes(spec, (entry["properties"],), scope=scope)
        elif name == "create_edges":
            tx.create_edges(spec, ((*identity, entry["properties"]),), scope=scope)
        else:
            record = (
                tx.get_nodes(spec, (identity,), fields=(), scope=scope)[0]
                if node
                else tx.get_edges(spec, (tuple(identity),), fields=(), scope=scope)[0]
            )
            if record is None:
                raise ValueError("Undo target no longer exists")
            if name.startswith("patch_"):
                items = (
                    ((identity, entry["properties"]),)
                    if node
                    else ((*identity, entry["properties"]),)
                )
                arguments = {"patches": items}
            else:
                arguments = (
                    {"keys": (identity,)} if node else {"endpoints": (tuple(identity),)}
                )
            getattr(tx, name)(spec, **arguments, scope=scope)

    def cleanup_batch(self) -> bool:
        """Delete a bounded log page and remove the terminal head last."""
        limit = min(100, get_settings().graph.write_batch_max_rows)

        def work(tx):
            head = self.head(tx)
            if head is not None and head.properties["recovery_state"] not in {
                "COMMITTED",
                "ROLLED_BACK",
            }:
                raise RuntimeError("only a terminal stack can be cleaned")
            batches = tx.list_nodes(
                UNDO_BATCH,
                filters={"stack_key": self.key},
                fields=(),
                limit=limit,
                scope=self.control_scope,
            )
            if batches:
                if head is None:
                    raise ValueError("recovery head disappeared before its logs")
                tx.delete_nodes(
                    UNDO_BATCH,
                    tuple(batch.key for batch in batches),
                    scope=self.control_scope,
                )
                return False
            if head is not None:
                tx.delete_nodes(
                    RECOVERY_STACK_HEAD, (self.key,), scope=self.control_scope
                )
            return True

        return self.store.atomic(work)
