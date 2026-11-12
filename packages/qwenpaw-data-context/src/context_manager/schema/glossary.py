"""Persist workspace terms in SQL, seeded once from the bundled JSON."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from ..utils.parsing import require_json_array, require_json_object
from ..utils.validation import require_trimmed_text
from .model import GraphSchema, NodeSpec, ValueKind
from .relational import workspace_glossaries as _workspace_glossaries
from .term import (
    Term,
    UnknownTermError,
    graph_label,
)

DEFAULT_GLOSSARY_FILE = Path(__file__).with_name("default_glossary.json")


class GlossaryRepository:
    """Read and atomically update one workspace's SQL term document."""

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> None:
        self._engine = engine
        self._identity = {
            "tenant_id": require_trimmed_text(tenant_id, "tenant_id"),
            "workspace_id": require_trimmed_text(workspace_id, "workspace_id"),
        }
        self._where = (
            _workspace_glossaries.c.tenant_id == tenant_id,
            _workspace_glossaries.c.workspace_id == workspace_id,
        )

    def get(self) -> dict[str, object] | None:
        """Read the persisted document, if the workspace has been initialized."""
        with self._engine.connect() as conn:
            payload = conn.execute(
                select(_workspace_glossaries.c.document).where(*self._where)
            ).scalar_one_or_none()
        return json.loads(payload) if payload is not None else None

    def create_if_absent(self, document: dict[str, object]) -> None:
        """Insert an initial document while preserving any existing workspace row."""
        insert = sqlite_insert if self._engine.dialect.name == "sqlite" else pg_insert
        with self._engine.begin() as conn:
            conn.execute(
                insert(_workspace_glossaries)
                .values(
                    **self._identity,
                    document=json.dumps(document, ensure_ascii=False),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "workspace_id"])
            )

    @contextmanager
    def edit(self) -> Iterator[dict[str, object]]:
        """Lock, read and save the workspace document in one transaction."""
        with self._engine.begin() as conn:
            statement = update(_workspace_glossaries).where(*self._where)
            # Take a write lock before reading, on both PostgreSQL and SQLite.
            conn.execute(statement.values(document=_workspace_glossaries.c.document))
            payload = conn.execute(
                select(_workspace_glossaries.c.document).where(*self._where)
            ).scalar_one()
            document = json.loads(payload)
            yield document
            conn.execute(
                statement.values(
                    document=json.dumps(document, ensure_ascii=False),
                )
            )


class Glossary:
    """Validate and manage terms and graph-label memberships within one workspace."""

    def __init__(
        self,
        schema: GraphSchema,
        engine: Engine,
        *,
        tenant_id: str,
        workspace_id: str,
        seed_file: str | Path = DEFAULT_GLOSSARY_FILE,
    ) -> None:
        """Initialize an absent workspace from JSON; never overwrite existing terms."""
        self._schema = schema
        self._repository = GlossaryRepository(
            engine,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        existing = self._repository.get()
        if existing is not None:
            self._validate_document(existing)
            return
        seed = Path(seed_file)
        try:
            document = json.loads(seed.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load glossary JSON: {seed}") from exc
        self._repository.create_if_absent(self._validate_document(document))

    @property
    def schema(self) -> GraphSchema:
        """Return the exact canonical Schema governed by this glossary."""
        return self._schema

    def upsert_term(
        self,
        node: NodeSpec,
        property_name: str,
        value: str,
        *,
        description: str,
    ) -> Term:
        """Add or update a workspace term while preserving its label memberships."""
        node, property_name = self._managed_property(node, property_name)
        value = require_trimmed_text(value, "term value")
        description = require_trimmed_text(description, "term description")
        with self._repository.edit() as document:
            term_group = self._find_term_group(document, node, property_name)
            if term_group is None:
                term_group = {
                    "node": node.name,
                    "property": property_name,
                    "terms": [],
                    "graph_labels": {},
                }
                self._term_groups(document).append(term_group)
            terms = cast(list[dict[str, object]], term_group["terms"])
            term = next(
                (item for item in terms if item["value"] == value),
                None,
            )
            if term is None:
                terms.append({"value": value, "description": description})
            else:
                term["description"] = description
            return self._term(term_group, value)

    def remove_term(
        self,
        node: NodeSpec,
        property_name: str,
        value: str,
    ) -> bool:
        """Remove one workspace term and its graph-label memberships."""
        node, property_name = self._managed_property(node, property_name)
        value = require_trimmed_text(value, "term value")
        with self._repository.edit() as document:
            term_group = self._find_term_group(document, node, property_name)
            if term_group is None:
                return False
            terms = cast(list[dict[str, object]], term_group["terms"])
            retained = [item for item in terms if item["value"] != value]
            if len(retained) == len(terms):
                return False
            term_group["terms"] = retained
            graph_labels = cast(dict[str, list[str]], term_group["graph_labels"])
            for label in tuple(graph_labels):
                graph_labels[label] = [
                    item for item in graph_labels[label] if item != value
                ]
                if not graph_labels[label]:
                    del graph_labels[label]
            if not retained:
                self._term_groups(document).remove(term_group)
            return True

    def set_graph_labels(
        self,
        node: NodeSpec,
        property_name: str,
        value: str,
        graph_labels: frozenset[str],
    ) -> Term:
        """Replace one workspace term's graph-label memberships."""
        node, property_name = self._managed_property(node, property_name)
        value = require_trimmed_text(value, "term value")
        graph_labels = frozenset(graph_label(label) for label in graph_labels)
        unsupported = graph_labels.difference({"Core"})
        if unsupported:
            raise ValueError(f"unsupported graph labels: {sorted(unsupported)!r}")
        if "Core" in graph_labels and (node.name, property_name) != (
            "Entity",
            "type",
        ):
            raise ValueError("Core is only valid for Entity.type terms")
        with self._repository.edit() as document:
            term_group = self._find_term_group(document, node, property_name)
            if term_group is None or not self._contains(term_group, value):
                raise self._unknown(node, property_name, value)
            memberships = cast(dict[str, list[str]], term_group["graph_labels"])
            for label in tuple(memberships):
                memberships[label] = [
                    item for item in memberships[label] if item != value
                ]
                if not memberships[label]:
                    del memberships[label]
            for label in sorted(graph_labels):
                memberships.setdefault(label, []).append(value)
            return self._term(term_group, value)

    def managed_properties(self, node: NodeSpec) -> tuple[str, ...]:
        """List properties with terms in this workspace for one canonical node."""
        node = self._schema.require_node(node)
        document = cast(dict[str, object], self._repository.get())
        return tuple(
            cast(str, item["property"])
            for item in self._term_groups(document)
            if item["node"] == node.name
        )

    def list_terms(
        self,
        node: NodeSpec,
        property_name: str,
    ) -> tuple[Term, ...]:
        """List current workspace terms in insertion order."""
        node, property_name = self._managed_property(node, property_name)
        document = cast(dict[str, object], self._repository.get())
        term_group = self._find_term_group(document, node, property_name)
        if term_group is None:
            return ()
        terms = cast(list[dict[str, object]], term_group["terms"])
        return tuple(self._term(term_group, cast(str, item["value"])) for item in terms)

    def resolve_term(
        self,
        node: NodeSpec,
        property_name: str,
        value: str,
    ) -> Term:
        """Resolve one exact term from a selected node property."""
        node, property_name = self._managed_property(node, property_name)
        value = require_trimmed_text(value, "term value")
        document = cast(dict[str, object], self._repository.get())
        term_group = self._find_term_group(document, node, property_name)
        if term_group is None or not self._contains(term_group, value):
            raise self._unknown(node, property_name, value)
        return self._term(term_group, value)

    def _managed_property(
        self,
        node: NodeSpec,
        property_name: str,
    ) -> tuple[NodeSpec, str]:
        """Require one canonical node and one text-valued property."""
        node = self._schema.require_node(node)
        property_name = require_trimmed_text(property_name, "property_name")
        if node.property(property_name).kind is not ValueKind.TEXT:
            raise ValueError("term group requires a text property")
        return node, property_name

    def _validate_document(self, document: object) -> dict[str, object]:
        """Validate the seed or persisted term document against the canonical Schema."""
        document = require_json_object(document, "glossary JSON")
        if set(document) != {"version", "term_groups"}:
            raise ValueError("glossary JSON must contain only version and term_groups")
        if document["version"] != 1:
            raise ValueError("glossary JSON version must be 1")
        term_groups = document["term_groups"]
        term_groups = require_json_array(term_groups, "glossary JSON term_groups")
        seen: set[tuple[str, str]] = set()
        for item in term_groups:
            self._validate_term_group(item, seen)
        return cast(dict[str, object], document)

    def _validate_term_group(
        self,
        value: object,
        seen: set[tuple[str, str]],
    ) -> None:
        """Validate one node/property term group and its graph-label memberships."""
        item = require_json_object(value, "term group")
        if set(item) != {
            "node",
            "property",
            "terms",
            "graph_labels",
        }:
            raise ValueError(
                "each term group must contain only node, property, terms, and "
                "graph_labels"
            )
        node = self._schema.node(require_trimmed_text(cast(str, item["node"]), "node"))
        property_name = require_trimmed_text(
            cast(str, item["property"]),
            "property",
        )
        if node.property(property_name).kind is not ValueKind.TEXT:
            raise ValueError("term group requires a text property")
        key = (node.name, property_name)
        if key in seen:
            raise ValueError(f"duplicate term group declaration: {key!r}")
        seen.add(key)
        terms = item["terms"]
        terms = require_json_array(terms, "glossary JSON terms")
        if not terms:
            raise ValueError("glossary JSON terms must be a non-empty list")
        term_values: set[str] = set()
        for raw_term in terms:
            term_item = require_json_object(
                raw_term,
                "term",
            )
            if set(term_item) != {
                "value",
                "description",
            }:
                raise ValueError("each term must contain only value and description")
            term = require_trimmed_text(
                cast(str, term_item["value"]),
                "term value",
            )
            require_trimmed_text(
                cast(str, term_item["description"]),
                "term description",
            )
            if term in term_values:
                raise ValueError(f"duplicate term value: {term!r}")
            term_values.add(term)
        graph_labels = item["graph_labels"]
        graph_labels = require_json_object(graph_labels, "glossary JSON graph_labels")
        for raw_label, raw_members in graph_labels.items():
            label = graph_label(raw_label)
            if label != "Core":
                raise ValueError(f"unsupported graph label: {label!r}")
            if key != ("Entity", "type"):
                raise ValueError("Core is only valid for Entity.type terms")
            members = tuple(
                require_trimmed_text(member, label)
                for member in require_json_array(
                    raw_members,
                    f"graph label {label} terms",
                )
            )
            if len(set(members)) != len(members):
                raise ValueError(f"graph label {label} contains duplicate terms")
            unknown = set(members).difference(term_values)
            if unknown:
                raise ValueError(
                    f"graph label {label} references unknown terms: {sorted(unknown)!r}"
                )

    @staticmethod
    def _term_groups(document: dict[str, object]) -> list[dict[str, object]]:
        """Return the already validated mutable term-group list."""
        return cast(list[dict[str, object]], document["term_groups"])

    def _find_term_group(
        self,
        document: dict[str, object],
        node: NodeSpec,
        property_name: str,
    ) -> dict[str, object] | None:
        """Find one exact canonical node/property declaration."""
        return next(
            (
                item
                for item in self._term_groups(document)
                if item["node"] == node.name and item["property"] == property_name
            ),
            None,
        )

    @staticmethod
    def _contains(term_group: dict[str, object], value: str) -> bool:
        """Return whether one term group contains an exact value."""
        terms = cast(list[dict[str, object]], term_group["terms"])
        return any(item["value"] == value for item in terms)

    @staticmethod
    def _unknown(
        node: NodeSpec,
        property_name: str,
        value: str,
    ) -> UnknownTermError:
        """Build the stable unknown-term failure."""
        return UnknownTermError(
            f"unknown term for {node.name}.{property_name}: {value!r}"
        )

    @staticmethod
    def _term(term_group: dict[str, object], value: str) -> Term:
        """Detach one JSON term and its graph-label memberships."""
        terms = cast(list[dict[str, object]], term_group["terms"])
        term = next(item for item in terms if item["value"] == value)
        memberships = cast(dict[str, list[str]], term_group["graph_labels"])
        graph_labels = frozenset(
            label for label, members in memberships.items() if value in members
        )
        return Term(
            node_name=cast(str, term_group["node"]),
            property_name=cast(str, term_group["property"]),
            value=value,
            description=cast(str, term["description"]),
            graph_labels=graph_labels,
        )


__all__ = ["DEFAULT_GLOSSARY_FILE", "Glossary", "GlossaryRepository"]
