"""Declare the Context Manager 1.0 logical graph as canonical objects."""

from __future__ import annotations

from .model import (
    CrudOperation,
    DisplayPolicy,
    EdgeSpec,
    GraphSchema,
    KeySpec,
    KeyStrategy,
    NodeSpec,
    PropertyOwner,
    PropertySpec,
    ValueKind,
    Zone,
)

READ = frozenset({CrudOperation.READ})
CREATE_READ_UPDATE = frozenset(
    {CrudOperation.CREATE, CrudOperation.READ, CrudOperation.UPDATE}
)
CREATE_READ_DELETE = frozenset(
    {CrudOperation.CREATE, CrudOperation.READ, CrudOperation.DELETE}
)
CRUD = frozenset(CrudOperation)
_ALWAYS = DisplayPolicy.ALWAYS
_NEVER = DisplayPolicy.NEVER

OWNER_ID = PropertySpec(
    "owner_id",
    ValueKind.TEXT,
    editable=False,
    display_policy=_ALWAYS,
    nullable=False,
    owner=PropertyOwner.SCOPE,
)
WORKSPACE_ID = PropertySpec(
    "workspace_id",
    ValueKind.TEXT,
    editable=False,
    display_policy=_ALWAYS,
    nullable=False,
    owner=PropertyOwner.SCOPE,
)
ZONE_PROPERTY = PropertySpec(
    "zone",
    ValueKind.TEXT,
    editable=False,
    nullable=False,
    owner=PropertyOwner.SCOPE,
)
DATASOURCE_ID = PropertySpec(
    "datasource_id",
    ValueKind.TEXT,
    editable=False,
    display_policy=_ALWAYS,
    owner=PropertyOwner.SCOPE,
)
EMBEDDING = PropertySpec(
    "embedding",
    ValueKind.VECTOR,
    display_policy=_NEVER,
)
EMBEDDING_HASH = PropertySpec(
    "embedding_hash",
    ValueKind.TEXT,
    display_policy=_NEVER,
)

# Persist feedback provenance with the affected business fact (also after restart).
FEEDBACK_PROPERTIES = tuple(
    PropertySpec(name, ValueKind.TEXT, display_policy=_NEVER)
    for name in (
        "card_client_id",
        "card_content_hash",
        "feedback_source",
        "feedback_extra",
    )
)

DATA_SOURCE = NodeSpec(
    name="DataSource",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "dsrc", ("owner_id", "datasource_id")),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "datasource_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
            owner=PropertyOwner.SCOPE,
        ),
        PropertySpec(
            "datasource_name",
            ValueKind.TEXT,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "datasource_type",
            ValueKind.TEXT,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("datasource_name", "description"),
    embedding_fields=("datasource_name", "description"),
)

DOMAIN = NodeSpec(
    name="Domain",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        # ①a：domain_id 是身份（入 key）；name 仅为显示名（可编辑）
        KeySpec(KeyStrategy.DETERMINISTIC, "dom", ("owner_id", "domain_id")),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("datasource_scope", ValueKind.TEXT_LIST),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "description"),
    embedding_fields=("name", "description"),
)

METRIC = NodeSpec(
    name="Metric",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "met", ("owner_id", "domain", "name")),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("synonyms", ValueKind.TEXT_LIST),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("unit", ValueKind.TEXT),
        PropertySpec("priority", ValueKind.TEXT),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        *FEEDBACK_PROPERTIES,
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "synonyms", "description"),
    embedding_fields=("name", "synonyms", "description"),
)

METRIC_CALIBER = NodeSpec(
    name="MetricCaliber",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC,
            "mcal",
            ("owner_id", "domain", "metric_name", "dataset", "time_window"),
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "metric_name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "dataset",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("expr", ValueKind.TEXT, nullable=False),
        PropertySpec(
            "time_window",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("additivity", ValueKind.TEXT),
        PropertySpec("is_default", ValueKind.BOOLEAN),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=(
        "metric_name",
        "dataset",
        "time_window",
    ),
    embedding_fields=(
        "metric_name",
        "dataset",
        "time_window",
    ),
)

DIMENSION = NodeSpec(
    name="Dimension",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "dim", ("owner_id", "domain", "name")),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("synonyms", ValueKind.TEXT_LIST),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("dimension_role", ValueKind.TEXT),
        PropertySpec("child_dimensions", ValueKind.TEXT_LIST),
        PropertySpec("priority", ValueKind.TEXT),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        *FEEDBACK_PROPERTIES,
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "synonyms", "description"),
    embedding_fields=("name", "synonyms", "description"),
)

DATASET = NodeSpec(
    name="Dataset",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "ds", ("owner_id", "domain", "name")),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "datasource_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
            owner=PropertyOwner.SCOPE,
        ),
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("source", ValueKind.TEXT, nullable=False),
        PropertySpec("updated_at", ValueKind.DATETIME),
        *FEEDBACK_PROPERTIES,
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "description"),
    embedding_fields=("name", "description"),
)

DATASET_FIELD = NodeSpec(
    name="DatasetField",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC,
            "dsf",
            ("owner_id", "domain", "dataset", "name"),
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "dataset",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("data_type", ValueKind.TEXT),
        PropertySpec("is_primary", ValueKind.BOOLEAN),
        PropertySpec("is_nullable", ValueKind.BOOLEAN),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        *FEEDBACK_PROPERTIES,
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("dataset", "name", "description"),
    embedding_fields=("dataset", "name", "description"),
)

VERIFIED_QUERY = NodeSpec(
    name="VerifiedQuery",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC,
            "vq",
            ("owner_id", "domain", "question_hash"),
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec(
            "domain",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "question", ValueKind.TEXT, display_policy=_ALWAYS, nullable=False
        ),
        PropertySpec(
            "question_hash",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "verified_sql",
            ValueKind.TEXT,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("question",),
    embedding_fields=("question",),
)

TABLE = NodeSpec(
    name="Table",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC, "tbl", ("owner_id", "datasource_id", "name")
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "datasource_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
            owner=PropertyOwner.SCOPE,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("comment", ValueKind.TEXT),
        PropertySpec("partition_keys", ValueKind.TEXT_LIST),
        PropertySpec("updated_at", ValueKind.DATETIME),
    ),
    fulltext_fields=("name", "comment"),
)

COLUMN = NodeSpec(
    name="Column",
    zone=Zone.METADATA,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC,
            "col",
            ("owner_id", "datasource_id", "table", "name"),
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "datasource_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
            owner=PropertyOwner.SCOPE,
        ),
        PropertySpec(
            "table",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec(
            "name",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("data_type", ValueKind.TEXT, nullable=False),
        PropertySpec("is_primary", ValueKind.BOOLEAN),
        PropertySpec("is_nullable", ValueKind.BOOLEAN),
        PropertySpec("comment", ValueKind.TEXT),
        PropertySpec("sample_values", ValueKind.TEXT_LIST, display_policy=_NEVER),
        PropertySpec("updated_at", ValueKind.DATETIME),
    ),
    fulltext_fields=("table", "name", "comment"),
)

ENTITY = NodeSpec(
    name="Entity",
    zone=Zone.KNOWLEDGE,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "ent", ("owner_id", "identity_hash")),
        PropertySpec("name", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("canonical_name", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("type", ValueKind.TEXT, nullable=False),
        PropertySpec(
            "identity_hash",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("aliases", ValueKind.TEXT_LIST),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("lifecycle_state", ValueKind.TEXT),
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        PropertySpec("source_ids", ValueKind.TEXT_LIST, display_policy=_ALWAYS),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "canonical_name", "aliases", "description", "type"),
    embedding_fields=("name", "canonical_name", "aliases", "description", "type"),
)

EVENT = NodeSpec(
    name="Event",
    zone=Zone.KNOWLEDGE,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "evt", ("owner_id", "identity_hash")),
        PropertySpec("name", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("type", ValueKind.TEXT, nullable=False),
        PropertySpec(
            "identity_hash",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        PropertySpec("description", ValueKind.TEXT),
        PropertySpec("scope", ValueKind.TEXT),
        PropertySpec("date_from", ValueKind.DATETIME),
        PropertySpec("date_to", ValueKind.DATETIME),
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        PropertySpec("source_ids", ValueKind.TEXT_LIST, display_policy=_ALWAYS),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("name", "type", "description"),
    embedding_fields=("name", "type", "description"),
)


USER = NodeSpec(
    name="User",
    zone=Zone.USER,
    operations=CREATE_READ_UPDATE,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "usr", ("workspace_id", "user_id")),
        PropertySpec(
            "user_id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        PropertySpec("profile", ValueKind.TEXT),
        PropertySpec("status", ValueKind.TEXT, display_policy=_NEVER),
        PropertySpec("created_at", ValueKind.DATETIME, display_policy=_NEVER),
        PropertySpec("updated_at", ValueKind.DATETIME, display_policy=_NEVER),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
)

USER_MEMORY = NodeSpec(
    name="UserMemory",
    zone=Zone.USER,
    operations=CRUD,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "mem", ("owner_id", "id")),
        PropertySpec(
            "id",
            ValueKind.TEXT,
            editable=False,
            display_policy=_ALWAYS,
            nullable=False,
        ),
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        PropertySpec("type", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("description", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("source_type", ValueKind.TEXT),
        PropertySpec("source_ref", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("source_refs", ValueKind.TEXT_LIST, display_policy=_NEVER),
        PropertySpec("evidence_at", ValueKind.TEXT, display_policy=_NEVER),
        PropertySpec("triggers", ValueKind.TEXT_LIST),
        PropertySpec("status", ValueKind.TEXT, display_policy=_NEVER),
        PropertySpec("created_at", ValueKind.DATETIME, display_policy=_NEVER),
        PropertySpec("updated_at", ValueKind.DATETIME, display_policy=_NEVER),
        EMBEDDING,
        EMBEDDING_HASH,
    ),
    fulltext_fields=("description", "triggers"),
    embedding_fields=("description", "triggers"),
)

HAS_DATASET = EdgeSpec(
    source=DOMAIN,
    relationship="HAS",
    target=DATASET,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

HAS_DIMENSION = EdgeSpec(
    source=DOMAIN,
    relationship="HAS",
    target=DIMENSION,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

HAS_METRIC = EdgeSpec(
    source=DOMAIN,
    relationship="HAS",
    target=METRIC,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

HAS_TABLE = EdgeSpec(
    source=DATA_SOURCE,
    relationship="HAS",
    target=TABLE,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

TABLE_HAS_COLUMN_COLUMN = EdgeSpec(
    source=TABLE,
    relationship="HAS",
    target=COLUMN,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

JOINS = EdgeSpec(
    source=TABLE,
    relationship="JOINS",
    target=TABLE,
    operations=CRUD,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("key", ValueKind.TEXT, display_policy=_ALWAYS),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("left_columns", ValueKind.TEXT_LIST, display_policy=_ALWAYS),
        PropertySpec("right_columns", ValueKind.TEXT_LIST, display_policy=_ALWAYS),
        PropertySpec("join_type", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
    ),
)

DATASET_HAS_COLUMN_DATASET_FIELD = EdgeSpec(
    source=DATASET,
    relationship="HAS",
    target=DATASET_FIELD,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

AVAILABLE_IN = EdgeSpec(
    source=DATASET,
    relationship="AVAILABLE_IN",
    target=DATA_SOURCE,
    operations=CREATE_READ_DELETE,
    properties=(WORKSPACE_ID, OWNER_ID, ZONE_PROPERTY, DATASOURCE_ID),
)

MAPS_TO_FIELD = EdgeSpec(
    source=DIMENSION,
    relationship="MAPS_TO_FIELD",
    target=DATASET_FIELD,
    operations=CRUD,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("expr", ValueKind.TEXT, nullable=False),
        PropertySpec("value_example", ValueKind.TEXT_LIST),
        PropertySpec("total_value_marker", ValueKind.TEXT),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
    ),
)

DIMENSION_BINDING = EdgeSpec(
    source=DIMENSION,
    relationship="DIMENSION_BINDING",
    target=DATASET,
    operations=CRUD,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("expr", ValueKind.TEXT, nullable=False),
        PropertySpec("value_example", ValueKind.TEXT_LIST),
        PropertySpec("total_value_marker", ValueKind.TEXT),
        PropertySpec("source", ValueKind.TEXT),
        PropertySpec("updated_at", ValueKind.DATETIME),
    ),
)

HAS_CALIBER = EdgeSpec(
    source=METRIC,
    relationship="HAS",
    target=METRIC_CALIBER,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

CALIBER_ON = EdgeSpec(
    source=METRIC_CALIBER,
    relationship="CALIBER_ON",
    target=DATASET,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

ANCHORED_TO = EdgeSpec(
    source=VERIFIED_QUERY,
    relationship="ANCHORED_TO",
    target=DOMAIN,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

_KNOWLEDGE_EDGE_SCOPE = (
    WORKSPACE_ID,
    OWNER_ID,
    ZONE_PROPERTY,
    DATASOURCE_ID,
)

PARTICIPATION = EdgeSpec(
    source=ENTITY,
    relationship="PARTICIPATION",
    target=EVENT,
    operations=CRUD,
    properties=(
        *_KNOWLEDGE_EDGE_SCOPE,
        PropertySpec("role", ValueKind.TEXT),
        PropertySpec("description", ValueKind.TEXT),
    ),
)

HIERARCHY = EdgeSpec(
    source=ENTITY,
    relationship="HIERARCHY",
    target=ENTITY,
    operations=CRUD,
    properties=(
        *_KNOWLEDGE_EDGE_SCOPE,
        PropertySpec("description", ValueKind.TEXT),
    ),
)

COMPOSITION = EdgeSpec(
    source=ENTITY,
    relationship="COMPOSITION",
    target=ENTITY,
    operations=CRUD,
    properties=(
        *_KNOWLEDGE_EDGE_SCOPE,
        PropertySpec("description", ValueKind.TEXT),
    ),
)

ENTITY_INFLUENCE_ENTITY = EdgeSpec(
    source=ENTITY,
    relationship="INFLUENCE",
    target=ENTITY,
    operations=CRUD,
    properties=(*_KNOWLEDGE_EDGE_SCOPE, PropertySpec("description", ValueKind.TEXT)),
)

ENTITY_INFLUENCE_EVENT = EdgeSpec(
    source=ENTITY,
    relationship="INFLUENCE",
    target=EVENT,
    operations=CRUD,
    properties=(*_KNOWLEDGE_EDGE_SCOPE, PropertySpec("description", ValueKind.TEXT)),
)

EVENT_INFLUENCE_ENTITY = EdgeSpec(
    source=EVENT,
    relationship="INFLUENCE",
    target=ENTITY,
    operations=CRUD,
    properties=(*_KNOWLEDGE_EDGE_SCOPE, PropertySpec("description", ValueKind.TEXT)),
)

EVENT_INFLUENCE_EVENT = EdgeSpec(
    source=EVENT,
    relationship="INFLUENCE",
    target=EVENT,
    operations=CRUD,
    properties=(
        *_KNOWLEDGE_EDGE_SCOPE,
        PropertySpec("description", ValueKind.TEXT),
    ),
)

ASSOCIATION = EdgeSpec(
    source=ENTITY,
    relationship="ASSOCIATION",
    target=ENTITY,
    operations=CRUD,
    properties=(
        *_KNOWLEDGE_EDGE_SCOPE,
        PropertySpec("description", ValueKind.TEXT),
    ),
)


HAS_MEMORY = EdgeSpec(
    source=USER,
    relationship="HAS",
    target=USER_MEMORY,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

SURFACE_DOMAIN = EdgeSpec(
    source=ENTITY,
    relationship="SURFACE_DOMAIN",
    target=DOMAIN,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("role", ValueKind.TEXT, editable=False),
        PropertySpec("notes", ValueKind.TEXT, editable=False),
        PropertySpec("merge_policy", ValueKind.TEXT, editable=False),
    ),
)

SURFACE_METRIC = EdgeSpec(
    source=ENTITY,
    relationship="SURFACE_METRIC",
    target=METRIC,
    operations=CRUD,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("role", ValueKind.TEXT),
        PropertySpec("notes", ValueKind.TEXT),
        PropertySpec("merge_policy", ValueKind.TEXT),
    ),
)

SURFACE_DIMENSION = EdgeSpec(
    source=ENTITY,
    relationship="SURFACE_DIMENSION",
    target=DIMENSION,
    operations=CRUD,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
        PropertySpec("role", ValueKind.TEXT),
        PropertySpec("notes", ValueKind.TEXT),
        PropertySpec("merge_policy", ValueKind.TEXT),
    ),
)


USER_MEMORY_REFERS_TO_METRIC = EdgeSpec(
    source=USER_MEMORY,
    relationship="REFERS_TO",
    target=METRIC,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

USER_MEMORY_REFERS_TO_DIMENSION = EdgeSpec(
    source=USER_MEMORY,
    relationship="REFERS_TO",
    target=DIMENSION,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

USER_MEMORY_REFERS_TO_COLUMN = EdgeSpec(
    source=USER_MEMORY,
    relationship="REFERS_TO",
    target=COLUMN,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

USER_MEMORY_REFERS_TO_ENTITY = EdgeSpec(
    source=USER_MEMORY,
    relationship="REFERS_TO",
    target=ENTITY,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

USER_MEMORY_REFERS_TO_EVENT = EdgeSpec(
    source=USER_MEMORY,
    relationship="REFERS_TO",
    target=EVENT,
    operations=CREATE_READ_DELETE,
    properties=(
        WORKSPACE_ID,
        OWNER_ID,
        ZONE_PROPERTY,
        DATASOURCE_ID,
    ),
)

MG_SYNC_STATE = NodeSpec(
    name="MGSyncState",
    zone=Zone.INTERNAL,
    internal=True,
    operations=CREATE_READ_UPDATE,
    properties=(
        KeySpec(KeyStrategy.DETERMINISTIC, "mg_sync", ("owner_id",)),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec("state", ValueKind.TEXT, display_policy=_NEVER, nullable=False),
    ),
)


RECOVERY_STACK_HEAD = NodeSpec(
    name="RecoveryStackHead",
    zone=Zone.INTERNAL,
    internal=True,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC, "recovery", ("workspace_id", "owner_id", "rid")
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "rid", ValueKind.TEXT, editable=False, nullable=False, display_policy=_NEVER
        ),
        PropertySpec(
            "recovery_state",  # Stack state: ACTIVE/COMMITTED/RECOVERING/ROLLED_BACK.
            ValueKind.TEXT,
            nullable=False,
            display_policy=_NEVER,
        ),
        PropertySpec(
            "updated_at",  # Written before reading to acquire the head's lock.
            ValueKind.TEXT,
            nullable=False,
            display_policy=_NEVER,
        ),
        PropertySpec(
            "head_seq", ValueKind.INTEGER, nullable=False, display_policy=_NEVER
        ),
        PropertySpec(
            "result_status",  # Business result: SUCCESS / DEGRADED / FAILED / KILLED.
            ValueKind.TEXT,
            display_policy=_NEVER,
        ),
        PropertySpec("message", ValueKind.TEXT, display_policy=_NEVER),
    ),
)

UNDO_BATCH = NodeSpec(
    name="UndoBatch",
    zone=Zone.INTERNAL,
    internal=True,
    operations=CRUD,
    properties=(
        KeySpec(
            KeyStrategy.DETERMINISTIC,
            "undo",
            ("workspace_id", "owner_id", "stack_key", "sequence"),
        ),
        OWNER_ID,
        WORKSPACE_ID,
        ZONE_PROPERTY,
        PropertySpec(
            "stack_key",
            ValueKind.TEXT,
            editable=False,
            nullable=False,
            display_policy=_NEVER,
        ),
        PropertySpec(
            "sequence",
            ValueKind.INTEGER,
            editable=False,
            nullable=False,
            display_policy=_NEVER,
        ),
        PropertySpec("payload", ValueKind.TEXT, nullable=False, display_policy=_NEVER),
    ),
)


GRAPH_SCHEMA = GraphSchema(
    version=19,
    nodes=(
        DATA_SOURCE,
        DOMAIN,
        METRIC,
        METRIC_CALIBER,
        DIMENSION,
        DATASET,
        DATASET_FIELD,
        VERIFIED_QUERY,
        TABLE,
        COLUMN,
        MG_SYNC_STATE,
        RECOVERY_STACK_HEAD,
        UNDO_BATCH,
        ENTITY,
        EVENT,
        USER,
        USER_MEMORY,
    ),
    edges=(
        HAS_DATASET,
        HAS_DIMENSION,
        HAS_METRIC,
        HAS_TABLE,
        TABLE_HAS_COLUMN_COLUMN,
        JOINS,
        DATASET_HAS_COLUMN_DATASET_FIELD,
        DIMENSION_BINDING,
        AVAILABLE_IN,
        MAPS_TO_FIELD,
        HAS_CALIBER,
        CALIBER_ON,
        ANCHORED_TO,
        PARTICIPATION,
        HIERARCHY,
        COMPOSITION,
        ENTITY_INFLUENCE_ENTITY,
        ENTITY_INFLUENCE_EVENT,
        EVENT_INFLUENCE_ENTITY,
        EVENT_INFLUENCE_EVENT,
        ASSOCIATION,
        HAS_MEMORY,
        SURFACE_DOMAIN,
        SURFACE_METRIC,
        SURFACE_DIMENSION,
        USER_MEMORY_REFERS_TO_METRIC,
        USER_MEMORY_REFERS_TO_DIMENSION,
        USER_MEMORY_REFERS_TO_COLUMN,
        USER_MEMORY_REFERS_TO_ENTITY,
        USER_MEMORY_REFERS_TO_EVENT,
    ),
)


__all__ = [
    "ANCHORED_TO",
    "ASSOCIATION",
    "AVAILABLE_IN",
    "CALIBER_ON",
    "COLUMN",
    "COMPOSITION",
    "CRUD",
    "DATASET",
    "DATASET_FIELD",
    "DATASET_HAS_COLUMN_DATASET_FIELD",
    "DATA_SOURCE",
    "DIMENSION",
    "DIMENSION_BINDING",
    "DOMAIN",
    "ENTITY",
    "ENTITY_INFLUENCE_ENTITY",
    "ENTITY_INFLUENCE_EVENT",
    "EVENT",
    "EVENT_INFLUENCE_ENTITY",
    "EVENT_INFLUENCE_EVENT",
    "GRAPH_SCHEMA",
    "HAS_CALIBER",
    "HAS_DATASET",
    "HAS_DIMENSION",
    "HAS_MEMORY",
    "HAS_METRIC",
    "HAS_TABLE",
    "HIERARCHY",
    "JOINS",
    "MAPS_TO_FIELD",
    "METRIC",
    "METRIC_CALIBER",
    "MG_SYNC_STATE",
    "PARTICIPATION",
    "RECOVERY_STACK_HEAD",
    "SURFACE_DIMENSION",
    "SURFACE_DOMAIN",
    "SURFACE_METRIC",
    "TABLE",
    "TABLE_HAS_COLUMN_COLUMN",
    "UNDO_BATCH",
    "USER",
    "USER_MEMORY",
    "USER_MEMORY_REFERS_TO_COLUMN",
    "USER_MEMORY_REFERS_TO_DIMENSION",
    "USER_MEMORY_REFERS_TO_ENTITY",
    "USER_MEMORY_REFERS_TO_EVENT",
    "USER_MEMORY_REFERS_TO_METRIC",
    "VERIFIED_QUERY",
]
