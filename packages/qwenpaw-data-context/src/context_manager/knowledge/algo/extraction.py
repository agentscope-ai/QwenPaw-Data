"""Convert one LLM chunk response directly into schema-bound graph drafts."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime
from threading import Lock

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from ...model import ChatMessage, LlmCaller, LlmError
from ...process import TextChunk, complete_json
from ...schema import (
    ENTITY,
    EVENT,
    EdgeSpec,
    Glossary,
    GraphSchema,
    NodeSpec,
    UnknownTermError,
    ValueKind,
)
from ...settings import get_settings
from ...store import EdgeDraft, NodeDraft
from ...utils.parsing import (
    require_json_array,
    require_json_object,
    require_json_object_fields,
)
from ...utils.validation import (
    normalize_nonempty_text,
    require_probability,
    require_trimmed_text,
)
from ...utils.values import is_empty_value
from .contract import KnowledgeImportError, KnowledgeImportErrorCode

_ROOT_FIELDS = frozenset({"entities", "events", "relationships"})
_NODE_FIELDS = frozenset({"ref", "properties", "confidence"})
_ENTITY_FIELDS = _NODE_FIELDS | {"type_description"}
_EDGE_FIELDS = frozenset(
    {"source_ref", "relationship", "target_ref", "properties", "confidence"}
)
_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_DATETIME = TypeAdapter(AwareDatetime)
_MAX_EXTRACTED_ENTITIES = 6
_MAX_EXTRACTED_EVENTS = 3
_MAX_EXTRACTED_RELATIONSHIPS = 8
_EXTRACTION_FIELDS = {
    ENTITY: frozenset(
        {
            "name",
            "canonical_name",
            "type",
            "aliases",
            "description",
            "lifecycle_state",
        }
    ),
    EVENT: frozenset(
        {
            "name",
            "type",
            "description",
            "scope",
            "date_from",
            "date_to",
        }
    ),
}
_EXTRACTION_REQUIRED = {
    ENTITY: frozenset({"name", "canonical_name", "type"}),
    EVENT: frozenset({"name", "type"}),
}
_EXTRACTION_DEFAULTS: Mapping[NodeSpec, Mapping[str, object]] = {
    ENTITY: {"aliases": (), "lifecycle_state": "active"},
    EVENT: {},
}


def _knowledge_edge_specs(schema: GraphSchema) -> tuple[EdgeSpec, ...]:
    """Bind one canonical Schema at the extraction boundary."""
    schema.require_node(ENTITY)
    schema.require_node(EVENT)
    node_specs = frozenset({ENTITY, EVENT})
    edges = tuple(
        edge
        for edge in schema.edges
        if edge.source in node_specs and edge.target in node_specs
    )
    if not edges:
        raise ValueError("Knowledge schema must declare relationship specs")
    return edges


def _build_node_draft(
    schema: GraphSchema,
    spec: NodeSpec,
    properties: Mapping[str, object],
    *,
    confidence: object,
    origin_ref: object,
) -> NodeDraft:
    """Validate one extracted node and cross into canonical Draft values."""
    ref = require_trimmed_text(origin_ref, "origin_ref")
    return NodeDraft.build(
        schema,
        spec,
        ref,
        _normalized_properties(spec, properties),
        transient={
            "confidence": require_probability(confidence, "confidence"),
            "origin_refs": (ref,),
        },
    )


def _build_edge_draft(
    schema: GraphSchema,
    source: NodeDraft,
    relationship: object,
    target: NodeDraft,
    properties: Mapping[str, object],
    *,
    confidence: object,
    source_ref: object,
    target_ref: object,
) -> EdgeDraft:
    """Validate one relationship and retain its plan-local endpoint refs."""
    relationship_name = require_trimmed_text(relationship, "relationship")
    spec = schema.edge((source.spec.name, relationship_name, target.spec.name))
    return EdgeDraft.build(
        schema,
        spec,
        require_trimmed_text(source_ref, "source_ref"),
        require_trimmed_text(target_ref, "target_ref"),
        _edge_properties(spec, properties),
        transient={
            "confidence": require_probability(confidence, "confidence"),
        },
    )


def _normalized_properties(
    spec: NodeSpec,
    raw: Mapping[str, object],
) -> dict[str, object]:
    """Apply the Knowledge extraction rules before Draft construction."""
    unknown = set(raw) - _EXTRACTION_FIELDS[spec]
    if unknown:
        raise ValueError(
            f"extraction returned unsupported properties: {sorted(unknown)}"
        )
    values = {**_EXTRACTION_DEFAULTS[spec], **raw}
    normalized = {
        name: _normalized_value(spec, name, value)
        for name, value in values.items()
        if value is not None
    }
    missing = {
        name
        for name in _EXTRACTION_REQUIRED[spec]
        if is_empty_value(normalized.get(name))
    }
    if missing:
        raise ValueError(f"extraction omitted required properties: {sorted(missing)}")
    if spec is EVENT:
        date_from = normalized.get("date_from")
        date_to = normalized.get("date_to")
        if isinstance(date_from, date) and isinstance(date_to, date):
            if not (isinstance(date_from, datetime) and isinstance(date_to, datetime)):
                # A date-only endpoint bounds a calendar day, not an invented instant.
                if isinstance(date_from, datetime):
                    date_from = date_from.date()
                if isinstance(date_to, datetime):
                    date_to = date_to.date()
            if date_to < date_from:
                raise ValueError("event date_to cannot precede date_from")
    return normalized


def _edge_properties(spec: EdgeSpec, raw: Mapping[str, object]) -> dict[str, object]:
    """Normalize the small Knowledge edge extraction contract."""
    unknown = set(raw) - {"description", "role"}
    if unknown:
        raise ValueError(
            f"extraction returned unsupported edge properties: {sorted(unknown)}"
        )
    values: dict[str, object] = {}
    for name, value in raw.items():
        spec.property(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"edge property {name!r} must be non-empty text")
        values[name] = value.strip()
    return values


def _normalized_value(spec: NodeSpec, name: str, value: object) -> object:
    """Normalize JSON according to one canonical PropertySpec kind."""
    kind = spec.property(name).kind
    if kind is ValueKind.TEXT:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"property {name!r} must be non-empty text")
        return value.strip()
    if kind is ValueKind.TEXT_LIST:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError(f"property {name!r} must be a text sequence")
        return tuple(
            dict.fromkeys(
                normalize_nonempty_text(item, f"property {name!r}") for item in value
            )
        )
    if kind is ValueKind.DATETIME:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        try:
            return _DATETIME.validate_python(value)
        except ValidationError as exc:
            raise ValueError(
                f"property {name!r} must be an ISO date or a timestamp with timezone"
            ) from exc
    return value


def _extraction_messages(
    chunk: TextChunk,
    entity_types: Sequence[tuple[str, str]],
    edge_specs: Sequence[EdgeSpec],
) -> tuple[ChatMessage, ...]:
    """Create one compact, bounded Entity/Event extraction request."""
    type_document = [
        {"value": value, "description": description}
        for value, description in entity_types
    ]
    edge_document = [
        {
            "source": edge.source.name,
            "relationship": edge.relationship,
            "target": edge.target.name,
            "role": any(prop.name == "role" for prop in edge.properties),
        }
        for edge in edge_specs
    ]
    contract = {
        "entities": [
            {
                "ref": "e1",
                "properties": {
                    "name": "",
                    "canonical_name": "",
                    "type": "",
                    "aliases": [],
                    "description": None,
                    "lifecycle_state": "active",
                },
                "confidence": 0.9,
                "type_description": None,
            }
        ],
        "events": [
            {
                "ref": "v1",
                "properties": {
                    "name": "",
                    "type": "",
                    "description": None,
                    "scope": None,
                    "date_from": None,
                    "date_to": None,
                },
                "confidence": 0.9,
            }
        ],
        "relationships": [
            {
                "source_ref": "e1",
                "relationship": "PARTICIPATION",
                "target_ref": "v1",
                "properties": {},
                "confidence": 0.9,
            }
        ],
    }
    system = f"""Extract only the most important explicit enterprise KG facts.
Return one JSON object only. Extract at most {_MAX_EXTRACTED_ENTITIES} entities,
{_MAX_EXTRACTED_EVENTS} events, and {_MAX_EXTRACTED_RELATIONSHIPS} relationships;
prefer fewer high-confidence facts. Do not invent facts, identifiers, types,
relationships, or dates. Descriptions are optional and at most 40 characters.
Each ref is chunk-local and every relationship must reference returned refs.
For each relationship, resolve source_ref and target_ref against the returned
entities and events arrays: refs in entities have node type Entity; refs in
events have node type Event. Select a relationship only when the complete
(source node type, relationship, target node type) triple matches an entry in
RELATIONSHIPS. An allowed relationship name alone is not sufficient.
Relationship meanings:
PARTICIPATION: an Entity participates in or has a role in an Event.
HIERARCHY: abstract category and concrete instance.
COMPOSITION: composition, containment, or belonging.
INFLUENCE: influence from source to target.
ASSOCIATION: fallback association when no other relationship applies.
Entity.type must reuse an allowed value
when adequate. Only if none applies, use one reusable lower_snake_case category
and set type_description; otherwise type_description is null. Follow CONTRACT
exactly, omit unsupported optional values, use YYYY-MM-DD for dates and include
an explicit timezone only when the source provides a timestamp. Return empty
arrays for empty sections. Every Entity/Event field belongs inside properties;
only ref, confidence, and Entity type_description belong in the envelope."""
    user = (
        "CONTRACT="
        f"{json.dumps(contract, ensure_ascii=False, separators=(',', ':'))}\n"
        "ENTITY_TYPES="
        f"{json.dumps(type_document, ensure_ascii=False, separators=(',', ':'))}\n"
        "RELATIONSHIPS="
        f"{json.dumps(edge_document, ensure_ascii=False, separators=(',', ':'))}\n"
        f"TEXT={chunk.text}"
    )
    return (ChatMessage("system", system), ChatMessage("user", user))


class KnowledgeChunkExtractor:
    """Extract Draft values while keeping every KG-specific rule in Knowledge."""

    def __init__(
        self,
        caller: LlmCaller,
        glossary: Glossary,
        *,
        run_sync: Callable[..., Awaitable],
    ) -> None:
        """Bind one caller and the terms governing canonical Draft creation."""
        if not isinstance(caller, LlmCaller):
            raise TypeError("caller must implement LlmCaller")
        if not isinstance(glossary, Glossary):
            raise TypeError("glossary must be a Glossary")
        self._caller = caller
        self._run_sync = run_sync
        self._glossary = glossary
        self._schema = glossary.schema
        self._edge_specs = _knowledge_edge_specs(self._schema)
        self._glossary_lock = Lock()

    async def extract(self, chunk: TextChunk) -> tuple[NodeDraft | EdgeDraft, ...]:
        """Call the model once and return no intermediate extraction objects."""
        messages = await self._run_sync(self._messages, chunk)
        try:
            response = await complete_json(
                self._caller,
                messages,
                attempts=1,
                max_tokens=1400,
                timeout_seconds=get_settings().knowledge.ingest_llm_timeout,
            )
            return await self._run_sync(self._drafts, response, chunk.index)
        except LlmError as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_FAILED,
                "knowledge extraction request failed",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_INVALID,
                "knowledge extraction returned an invalid structure",
            ) from exc

    def _messages(self, chunk):
        entity_types = tuple(
            (term.value, term.description)
            for term in self._glossary.list_terms(ENTITY, "type")
        )
        if not entity_types:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.EXTRACTION_INVALID,
                "Entity.type terms must not be empty",
            )
        return _extraction_messages(chunk, entity_types, self._edge_specs)

    def _drafts(
        self,
        document: Mapping[str, object],
        chunk_index: int,
    ) -> tuple[NodeDraft | EdgeDraft, ...]:
        """Validate raw envelopes locally and immediately replace them with Drafts."""
        unknown = set(document) - _ROOT_FIELDS
        if unknown:
            raise ValueError(f"extraction returned unknown sections: {sorted(unknown)}")
        entities = require_json_array(document.get("entities", []), "entities")
        events = require_json_array(document.get("events", []), "events")
        relationships = require_json_array(
            document.get("relationships", []),
            "relationships",
        )
        for name, values, maximum in (
            ("entities", entities, _MAX_EXTRACTED_ENTITIES),
            ("events", events, _MAX_EXTRACTED_EVENTS),
            (
                "relationships",
                relationships,
                _MAX_EXTRACTED_RELATIONSHIPS,
            ),
        ):
            if len(values) > maximum:
                raise ValueError(f"extraction returned too many {name}")
        nodes: list[NodeDraft] = []
        by_ref: dict[str, NodeDraft] = {}
        for section, spec, values in (
            ("entities", ENTITY, entities),
            ("events", EVENT, events),
        ):
            for raw in values:
                envelope = require_json_object_fields(
                    raw,
                    section,
                    allowed=_ENTITY_FIELDS if spec is ENTITY else _NODE_FIELDS,
                    required=_NODE_FIELDS,
                )
                ref = require_trimmed_text(envelope["ref"], "ref")
                if ref in by_ref:
                    raise ValueError("chunk extraction contains duplicate references")
                properties = require_json_object(
                    envelope["properties"],
                    "extracted properties",
                )
                if not properties:
                    raise ValueError("extracted node properties must not be empty")
                if spec is ENTITY:
                    properties = {
                        **properties,
                        "type": self._resolve_entity_type(
                            properties,
                            envelope.get("type_description"),
                        ),
                    }
                draft = _build_node_draft(
                    self._schema,
                    spec,
                    properties,
                    confidence=envelope["confidence"],
                    origin_ref=f"{chunk_index}:{ref}",
                )
                nodes.append(draft)
                by_ref[ref] = draft

        edges: list[EdgeDraft] = []
        required_edge_fields = _EDGE_FIELDS - {"properties"}
        for raw in relationships:
            envelope = require_json_object_fields(
                raw,
                "relationships",
                allowed=_EDGE_FIELDS,
                required=required_edge_fields,
            )
            source_ref = require_trimmed_text(
                envelope["source_ref"],
                "source_ref",
            )
            target_ref = require_trimmed_text(
                envelope["target_ref"],
                "target_ref",
            )
            try:
                source = by_ref[source_ref]
                target = by_ref[target_ref]
            except KeyError as exc:
                raise ValueError(
                    "relationship references an unknown extracted node"
                ) from exc
            edges.append(
                _build_edge_draft(
                    self._schema,
                    source,
                    envelope["relationship"],
                    target,
                    require_json_object(
                        envelope.get("properties", {}),
                        "extracted properties",
                    ),
                    confidence=envelope["confidence"],
                    source_ref=f"{chunk_index}:{source_ref}",
                    target_ref=f"{chunk_index}:{target_ref}",
                )
            )
        return (*nodes, *edges)

    def _resolve_entity_type(
        self,
        properties: Mapping[str, object],
        type_description: object,
    ) -> str:
        """Reuse one current term or add one extractor-proposed category."""
        proposed = require_trimmed_text(properties.get("type"), "Entity type")
        with self._glossary_lock:
            try:
                self._glossary.resolve_term(ENTITY, "type", proposed)
            except UnknownTermError:
                if _TYPE_PATTERN.fullmatch(proposed) is None:
                    raise ValueError(
                        "new Entity types must use lower_snake_case"
                    ) from None
                description = require_trimmed_text(
                    type_description,
                    "new Entity type description",
                )
                self._glossary.upsert_term(
                    ENTITY,
                    "type",
                    proposed,
                    description=description,
                )
        return proposed


__all__ = ["KnowledgeChunkExtractor"]
