"""Verify workspace term persistence and one-time JSON initialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from context_manager.schema import (
    DEFAULT_GLOSSARY_FILE,
    ENTITY,
    GRAPH_SCHEMA,
    Glossary,
    UnknownTermError,
)
from sqlalchemy import create_engine

from cm.infrastructure.storage.schema_support import make_glossary


def _seed_glossary(
    path: Path,
    term_groups: list[dict[str, object]] | None = None,
) -> Glossary:
    """Seed one workspace in a test-local database."""
    path.write_text(
        json.dumps(
            {"version": 1, "term_groups": term_groups or []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return make_glossary(
        GRAPH_SCHEMA,
        path,
        engine=create_engine(f"sqlite:///{path.with_suffix('.db')}"),
    )


def test_mutations_persist_across_connections_without_changing_the_seed(
    tmp_path: Path,
) -> None:
    """Reopening a workspace retains its terms, including an intentionally empty set."""
    path = tmp_path / "terms.json"
    glossary = _seed_glossary(path)

    created = glossary.upsert_term(
        ENTITY,
        "type",
        "company",
        description="公司",
    )
    labeled = glossary.set_graph_labels(
        ENTITY,
        "type",
        "company",
        frozenset({"Core"}),
    )
    updated = glossary.upsert_term(
        ENTITY,
        "type",
        "company",
        description="公司或法人主体",
    )

    assert created.graph_labels == frozenset()
    assert labeled.graph_labels == frozenset({"Core"})
    assert updated.description == "公司或法人主体"
    assert updated.graph_labels == frozenset({"Core"})
    reloaded = make_glossary(
        GRAPH_SCHEMA,
        path,
        engine=create_engine(f"sqlite:///{path.with_suffix('.db')}"),
    )
    assert reloaded.resolve_term(ENTITY, "type", "company") == updated

    assert glossary.remove_term(ENTITY, "type", "company") is True
    assert glossary.remove_term(ENTITY, "type", "company") is False
    assert json.loads(path.read_text(encoding="utf-8"))["term_groups"] == []
    assert reloaded.list_terms(ENTITY, "type") == ()
    # Even a nonempty default seed must not repopulate an initialized workspace.
    reopened = make_glossary(
        GRAPH_SCHEMA,
        engine=create_engine(f"sqlite:///{path.with_suffix('.db')}"),
    )
    assert reopened.list_terms(ENTITY, "type") == ()


@pytest.mark.parametrize(
    "other_tenant,other_workspace",
    [
        ("tenant-1", "workspace-2"),
        ("tenant-2", "workspace-1"),
    ],
)
def test_workspace_terms_are_shared_without_leaking_to_other_workspaces(
    tmp_path,
    other_tenant,
    other_workspace,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'terms.db'}")
    first = make_glossary(GRAPH_SCHEMA, engine=engine)
    peer = make_glossary(GRAPH_SCHEMA, engine=engine)
    other = make_glossary(
        GRAPH_SCHEMA,
        engine=engine,
        tenant_id=other_tenant,
        workspace_id=other_workspace,
    )
    seed_before = DEFAULT_GLOSSARY_FILE.read_bytes()
    first.upsert_term(ENTITY, "type", "regulation", description="法规")
    assert peer.resolve_term(ENTITY, "type", "regulation").description == "法规"
    with pytest.raises(UnknownTermError):
        other.resolve_term(ENTITY, "type", "regulation")
    peer.upsert_term(ENTITY, "type", "regulation", description="法律法规")
    assert first.resolve_term(ENTITY, "type", "regulation").description == "法律法规"
    assert DEFAULT_GLOSSARY_FILE.read_bytes() == seed_before
