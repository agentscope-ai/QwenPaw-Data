"""Project canonical facts into develop's Metadata HTTP fields."""

import json

from ...store import NodeRecord


def dataset_source(node: NodeRecord) -> dict:
    """Mirror develop's source expansion without persisting its redundant graph fields."""
    source = (node.properties.get("source") or "").strip()
    is_sql = source.upper().startswith(("SELECT", "WITH"))
    parents = "" if is_sql else source
    tables = [parents] if parents else []
    if parents.startswith("["):
        try:
            parsed = json.loads(parents)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            tables = [str(value).strip() for value in parsed]
    return {
        "dataset_type": "SQL" if is_sql else "OLAP",
        "parents": parents,
        "physical_tables": list(dict.fromkeys(value for value in tables if value)),
    }


def dimension_summary(node: NodeRecord) -> dict:
    return {
        "dimension_name": node.properties["name"],
        "aliases": list(node.properties.get("synonyms") or ()),
        # develop's display classification is not dimension_role (categorical/time).
        "dimension_type": "OLAP维度",
        "is_display_dimension": True,
    }


def dataset_summary(
    node: NodeRecord, domain: str, datasource_types: dict[str, str]
) -> dict:
    datasource_id = node.properties.get("datasource_id") or ""
    return {
        "dataset_name": node.properties["name"],
        "domain": domain,
        "description": node.properties.get("description") or "",
        "dataset_type": dataset_source(node)["dataset_type"],
        "datasource_id": datasource_id,
        "datasource_type": datasource_types.get(datasource_id, ""),
    }
