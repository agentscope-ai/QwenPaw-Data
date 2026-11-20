"""Derive Holo storage only from explicit canonical graph declarations."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

import psycopg
from psycopg import sql

from ...schema import GRAPH_SCHEMA, GraphSchema
from .catalog import HoloSchemaCatalog

__all__ = ["build_physical_schema", "ensure_schema"]


def _ensure_created(execute: Callable, statement: str, exists: str) -> bool:
    """Accept a concurrent creator only after the target catalog entry exists."""
    if execute(exists):
        return False
    try:
        execute(statement)
        return True
    except psycopg.Error as exc:
        if exc.sqlstate not in {"23505", "42P06", "42P07", "42701", "42710"}:
            raise
        # Another backend may still be publishing its graph/label metadata.
        deadline = time.monotonic() + 5
        while True:
            if execute(exists):
                return False
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def build_physical_schema(
    schema: GraphSchema = GRAPH_SCHEMA,
    *,
    embed_dim: int = 1024,
) -> HoloSchemaCatalog:
    """Declare one physical catalog without reading deployment settings."""
    return HoloSchemaCatalog(schema, embed_dim=embed_dim)


def ensure_schema(
    graph: str,
    catalog: HoloSchemaCatalog,
    execute: Callable[..., list[dict]],
    *,
    user_space: str | None = None,
) -> None:
    """Provision native storage outside application DML transactions."""
    graph_literal = sql.Literal(graph).as_string()
    created = _ensure_created(
        execute,
        f"CALL ag_catalog.hg_age_create_graph({graph_literal})",
        f"SELECT 1 FROM hologres.hg_ag_graph WHERE name = {graph_literal}",
    )
    if user_space is not None:
        pending = "initializing:" + user_space
        if created:
            execute(
                f"COMMENT ON SCHEMA {sql.Identifier(graph).as_string()} "
                f"IS {sql.Literal(pending).as_string()}"
            )
        else:
            # A concurrent creator publishes ownership immediately after CREATE.
            deadline = time.monotonic() + 5
            while True:
                marker = execute(
                    "SELECT obj_description(oid, 'pg_namespace') AS owner "
                    f"FROM pg_namespace WHERE nspname = {graph_literal}"
                )[0]["owner"]
                if marker == user_space:
                    return
                if marker == pending:
                    break
                if marker is not None or time.monotonic() >= deadline:
                    raise ValueError(
                        "graph name is already allocated outside this user space"
                    )
                time.sleep(0.05)
    tables = {
        row["table_name"]
        for row in execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = {sql.Literal(graph).as_string()}"
        )
    }
    for labels, procedure in (
        (catalog.vertex_labels, "hg_age_create_vlabel"),
        (catalog.edge_labels, "hg_age_create_elabel"),
    ):
        for label in labels:
            if label not in tables:
                _ensure_created(
                    execute,
                    f"CALL ag_catalog.{procedure}({graph_literal}, {sql.Literal(label).as_string()})",
                    "SELECT 1 FROM information_schema.tables "
                    f"WHERE table_schema = {graph_literal} "
                    f"AND table_name = {sql.Literal(label).as_string()}",
                )
    columns = {
        (row["table_name"], row["column_name"])
        for row in execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE table_schema = {sql.Literal(graph).as_string()}"
        )
    }
    indexes = {
        row["indexname"]
        for row in execute(
            f"SELECT indexname FROM pg_indexes WHERE schemaname = {sql.Literal(graph).as_string()}"
        )
    }
    for spec in catalog.schema.nodes:
        table = sql.Identifier(graph, spec.name).as_string()
        # TODO(transition:core-label-projection): retire after native labels or
        # replacement Core queries are accepted; existing rows are not backfilled.
        if spec.name == "Entity" and (spec.name, "is_core") not in columns:
            _ensure_created(
                execute,
                f"ALTER TABLE {table} ADD COLUMN is_core boolean",
                "SELECT 1 FROM information_schema.columns "
                f"WHERE table_schema = {graph_literal} AND table_name = 'Entity' "
                "AND column_name = 'is_core' AND data_type = 'boolean'",
            )
        if spec.fulltext_fields:
            if (spec.name, "content") not in columns:
                _ensure_created(
                    execute,
                    f"ALTER TABLE {table} ADD COLUMN content text",
                    "SELECT 1 FROM information_schema.columns "
                    f"WHERE table_schema = {graph_literal} "
                    f"AND table_name = {sql.Literal(spec.name).as_string()} "
                    "AND column_name = 'content' AND data_type = 'text'",
                )
            index = "cm_text_" + hashlib.sha256(spec.name.encode()).hexdigest()[:20]
            if index not in indexes:
                _ensure_created(
                    execute,
                    f"CREATE INDEX {sql.Identifier(index).as_string()} ON {table} "
                    "USING FULLTEXT (content) WITH (tokenizer = 'jieba')",
                    f"SELECT 1 FROM pg_indexes WHERE schemaname = {graph_literal} "
                    f"AND tablename = {sql.Literal(spec.name).as_string()} "
                    f"AND indexname = {sql.Literal(index).as_string()}",
                )
        if spec.embedding_fields:
            # TODO(transition:embedding-index-projection): 预计未来移除；
            # retire column/index after property indexing or a single-copy migration.
            if (spec.name, "plot_embedding") not in columns:
                _ensure_created(
                    execute,
                    f"ALTER TABLE {table} ADD COLUMN plot_embedding float4[] "
                    "CHECK (array_ndims(plot_embedding) = 1 AND "
                    f"array_length(plot_embedding, 1) = {catalog.embed_dim})",
                    "SELECT 1 FROM information_schema.columns "
                    f"WHERE table_schema = {graph_literal} "
                    f"AND table_name = {sql.Literal(spec.name).as_string()} "
                    "AND column_name = 'plot_embedding' AND udt_name = '_float4'",
                )
            vectors = json.dumps(
                {
                    "plot_embedding": {
                        "algorithm": "HGraph",
                        "distance_method": "Cosine",
                        "builder_params": {
                            "base_quantization_type": "fp32",
                            "max_degree": 64,
                            "ef_construction": 400,
                        },
                    }
                }
            )
            execute(
                f"ALTER TABLE {table} SET (vectors = {sql.Literal(vectors).as_string()})"
            )
        if spec.fulltext_fields or spec.embedding_fields:
            execute(f"VACUUM {table}")
    if user_space is not None:
        # All-Pod scans discover the space only after its full catalog is ready.
        execute(
            f"COMMENT ON SCHEMA {sql.Identifier(graph).as_string()} "
            f"IS {sql.Literal(user_space).as_string()}"
        )
