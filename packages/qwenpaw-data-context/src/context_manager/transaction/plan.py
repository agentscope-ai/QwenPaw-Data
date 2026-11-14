"""Describe immutable graph operations, transaction plans and detached results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from inspect import Parameter, signature
from types import MappingProxyType
from typing import TypeAlias

from ..store.base import GraphStore, is_primitive
from ..store.models import EdgeDraft, NodeDraft
from ..utils.immutable import freeze_mapping, freeze_value
from ..utils.validation import require_trimmed_text

StorePrimitiveName: TypeAlias = str


@dataclass(frozen=True, slots=True)
class _StorePrimitiveSignature:
    """Hold the plan-visible inputs derived from one GraphStore method."""

    required: frozenset[str]
    optional: frozenset[str]


def _load_primitive_signatures() -> Mapping[str, _StorePrimitiveSignature]:
    """Build the primitive registry from decorated GraphStore methods."""
    primitives: dict[str, _StorePrimitiveSignature] = {}
    for name, method in vars(GraphStore).items():
        if not is_primitive(method):
            continue
        parameters = signature(method).parameters
        required: set[str] = set()
        optional: set[str] = set()
        for parameter in parameters.values():
            if parameter.name in {"self", "scope"}:
                continue
            target = required if parameter.default is Parameter.empty else optional
            target.add(parameter.name)
        primitives[name] = _StorePrimitiveSignature(
            required=frozenset(required),
            optional=frozenset(optional),
        )
    return MappingProxyType(primitives)


_PRIMITIVE_SIGNATURES = _load_primitive_signatures()


@dataclass(frozen=True, slots=True)
class StoreOperation:
    """Describe one prepared invocation of a finite GraphStore primitive.

    ``name`` selects a method on the transaction-bound GraphStore. The executor
    resolves node refs, supplies one authorized ScopeBinding, and invokes that
    method with the prepared arguments. Business decisions belong to planning.
    Batch key positions are (argument, index) or (argument, row, field); each
    referenced position holds None until execution resolves the earlier key.
    """

    name: StorePrimitiveName
    parameters: Mapping[str, object]  # Known Store arguments, e.g. spec and properties.
    node_key_refs: Mapping[str | tuple[str, int] | tuple[str, int, int], str] = field(
        default_factory=dict
    )  # Argument or batch position -> earlier ref; ("edges", 0, 0) is the first edge's source key.
    node_refs: tuple[str | None, ...] = field(
        default=(), kw_only=True
    )  # Names aligned with create_nodes/get_nodes inputs; None leaves that result unnamed.

    def __post_init__(self) -> None:
        """Freeze one invocation and validate its exact Store signature."""
        name = self.name
        primitive_signature = _PRIMITIVE_SIGNATURES.get(name)
        if primitive_signature is None:
            raise ValueError("unsupported Store primitive")
        parameters = freeze_mapping(
            self.parameters,
            field_name="Store operation parameters",
            key_field_name="Store operation parameter",
        )
        if not isinstance(self.node_key_refs, Mapping):
            raise TypeError("Store operation node key refs must be a mapping")
        refs = {}
        for parameter, ref in self.node_key_refs.items():
            if isinstance(parameter, str):
                if parameter in parameters:
                    raise ValueError(
                        "Store operation parameters and node key refs cannot overlap"
                    )
            elif isinstance(parameter, tuple) and len(parameter) in {2, 3}:
                value = parameters.get(parameter[0])
                for index in parameter[1:]:
                    if (
                        type(index) is not int
                        or not isinstance(value, tuple)
                        or not 0 <= index < len(value)
                    ):
                        raise ValueError(
                            "Store operation node key ref has an invalid batch position"
                        )
                    value = value[index]
                if value is not None:
                    raise ValueError(
                        "Store operation node key ref must point to a None placeholder"
                    )
            else:
                raise ValueError(
                    "Store operation node key ref requires an argument or batch position"
                )
            refs[parameter] = require_trimmed_text(ref, "Store operation node key ref")
        required = primitive_signature.required
        optional = primitive_signature.optional
        supplied = set(parameters).union(
            parameter if isinstance(parameter, str) else parameter[0]
            for parameter in refs
        )
        missing = required - supplied
        if missing:
            raise ValueError(
                f"Store operation is missing parameters: {sorted(missing)}"
            )
        unknown = supplied - required - optional
        if unknown:
            raise ValueError(
                f"Store operation has unsupported parameters: {sorted(unknown)}"
            )
        if isinstance(self.node_refs, (str, bytes)):
            raise TypeError(
                "Store operation node refs must be a sequence of result names"
            )
        node_refs = tuple(
            None
            if ref is None
            else require_trimmed_text(ref, "Store operation node ref")
            for ref in self.node_refs
        )
        if node_refs:
            if name not in {"create_nodes", "get_nodes"}:
                raise ValueError("only a batch node operation can declare node refs")
            inputs = parameters.get("properties" if name == "create_nodes" else "keys")
            if not isinstance(inputs, tuple) or len(node_refs) != len(inputs):
                raise ValueError(
                    "Store operation node refs must align with every batch input"
                )
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "node_key_refs", MappingProxyType(refs))
        object.__setattr__(self, "node_refs", node_refs)

    @classmethod
    def project_nodes(cls, drafts: Sequence[NodeDraft]) -> StoreOperation:
        """Project same-Spec Drafts into a create_nodes operation without Store access."""
        drafts = tuple(drafts)
        if not drafts:
            raise ValueError("node projection requires at least one NodeDraft")
        if any(type(draft) is not NodeDraft for draft in drafts):
            raise TypeError("node projection requires NodeDraft values")
        spec = drafts[0].spec
        if any(draft.spec is not spec for draft in drafts):
            raise ValueError(
                "node projection requires the same NodeSpec for every draft"
            )
        return cls(
            "create_nodes",
            {"spec": spec, "properties": tuple(draft.output() for draft in drafts)},
            node_refs=tuple(draft.ref for draft in drafts),
        )

    @classmethod
    def project_edges(cls, drafts: Sequence[EdgeDraft]) -> StoreOperation:
        """Project same-Spec Drafts into a create_edges operation without Store access."""
        drafts = tuple(drafts)
        if not drafts:
            raise ValueError("edge projection requires at least one EdgeDraft")
        if any(type(draft) is not EdgeDraft for draft in drafts):
            raise TypeError("edge projection requires EdgeDraft values")
        spec = drafts[0].spec
        if any(draft.spec is not spec for draft in drafts):
            raise ValueError(
                "edge projection requires the same EdgeSpec for every draft"
            )
        return cls(
            "create_edges",
            {
                "spec": spec,
                "edges": tuple((None, None, draft.output()) for draft in drafts),
            },
            node_key_refs={
                ("edges", index, endpoint): ref
                for index, draft in enumerate(drafts)
                for endpoint, ref in enumerate((draft.source_ref, draft.target_ref))
            },
        )


@dataclass(frozen=True, slots=True)
class TransactionPlan:
    """Hold the complete ordered operations for one atomic Store transaction.

    The Plan is immutable data prepared before ``GraphStore.atomic`` begins. A
    node key ref may only name a node result produced by an earlier
    operation, which makes generated-key data flow explicit and deterministic.
    The Plan owns no Store, Scope, driver, session, or active transaction.
    """

    operations: tuple[StoreOperation, ...]

    def __post_init__(self) -> None:
        """Freeze order and reject missing, forward, or duplicate node refs."""
        operations = tuple(self.operations)
        if any(type(operation) is not StoreOperation for operation in operations):
            raise TypeError("Store transaction plan must contain StoreOperation values")
        available: set[str] = set()
        for operation in operations:
            missing = set(operation.node_key_refs.values()) - available
            if missing:
                raise ValueError(
                    "Store transaction operation references an unavailable node ref: "
                    f"{sorted(missing)}"
                )
            for node_ref in operation.node_refs:
                if node_ref is None:
                    continue
                if node_ref in available:
                    raise ValueError("Store transaction node refs must be unique")
                available.add(node_ref)
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Return detached operation results, node keys, and CRUD totals after commit.

    ``stats`` groups created/read/updates_submitted/deletes_submitted totals.
    Updates and deletes acknowledge successful commands, not per-target matches.
    Their totals count submitted targets, including missing targets; reads and
    creates count returned entries. Only committed Plan operations contribute.
    """

    operation_results: tuple[object, ...]
    node_keys: Mapping[
        str, str
    ]  # Plan-local node name -> persisted key, e.g. entity-1 -> node-42.
    stats: Mapping[str, Mapping[str, int]]

    def __post_init__(self) -> None:
        """Detach the executor's results into immutable containers."""
        object.__setattr__(
            self,
            "operation_results",
            freeze_value(
                tuple(self.operation_results),
                key_field_name="Store transaction results property",
            ),
        )
        object.__setattr__(self, "node_keys", MappingProxyType(dict(self.node_keys)))
        object.__setattr__(
            self,
            "stats",
            MappingProxyType(
                {
                    kind: MappingProxyType(dict(counts))
                    for kind, counts in self.stats.items()
                }
            ),
        )
