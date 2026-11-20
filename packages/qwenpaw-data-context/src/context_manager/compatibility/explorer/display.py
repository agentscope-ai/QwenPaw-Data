"""Apply the current Schema's display policy at the Explorer response boundary."""

from collections.abc import Mapping
from datetime import date, datetime

from ...schema import GRAPH_SCHEMA, DisplayPolicy

HIDDEN_FIELDS = frozenset(
    {"sql"}
    | {
        p.name
        for spec in (*GRAPH_SCHEMA.nodes, *GRAPH_SCHEMA.edges)
        for p in spec.properties
        if p.display_policy is DisplayPolicy.NEVER
    }
)


def json_value(value):
    if isinstance(value, Mapping):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
