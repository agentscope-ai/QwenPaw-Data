"""Resolve datasource membership with scoped, set-based relation queries."""

from .ds_ownership import ds_ownership_cypher

CLOSURE_LABELS = (
    "Dataset",
    "MetricCaliber",
    "Metric",
    "DatasetField",
    "Dimension",
    "Domain",
    "VerifiedQuery",
)
_METRIC_WITH_CALIBER = "metric_with_caliber"


def closure_key_sets(queries, *, ds):
    out = {label: [] for label in (*CLOSURE_LABELS, _METRIC_WITH_CALIBER)}
    if not ds or not any(s.name == "Dataset" for s in queries.specs):
        return out
    patterns = (
        ("Dataset", "(d:Dataset)", "d", ("d",)),
        (
            "MetricCaliber",
            "(n:MetricCaliber)-[r:CALIBER_ON]->(d:Dataset)",
            "n",
            ("n", "r", "d"),
        ),
        (
            "Metric",
            "(n:Metric)-[r:HAS]->(c:MetricCaliber)-[q:CALIBER_ON]->(d:Dataset)",
            "n",
            ("n", "r", "c", "q", "d"),
        ),
        ("DatasetField", "(d:Dataset)-[r:HAS]->(n:DatasetField)", "n", ("d", "r", "n")),
        (
            "Dimension",
            "(n:Dimension)-[r:MAPS_TO_FIELD]->(f:DatasetField)<-[q:HAS]-(d:Dataset)",
            "n",
            ("n", "r", "f", "q", "d"),
        ),
        ("Domain", "(n:Domain)-[r:HAS]->(d:Dataset)", "n", ("n", "r", "d")),
        (
            "VerifiedQuery",
            "(n:VerifiedQuery)-[r:ANCHORED_TO]->(dom:Domain)-[q:HAS]->(d:Dataset)",
            "n",
            ("n", "r", "dom", "q", "d"),
        ),
        (
            _METRIC_WITH_CALIBER,
            "(n:Metric)-[r:HAS]->(c:MetricCaliber)",
            "n",
            ("n", "r", "c"),
        ),
    )
    branches = []
    for label, pattern, result, aliases in patterns:
        conditions = (
            queries.scope(*aliases)
            + " AND "
            + " AND ".join(f"{a}.zone = 'metadata'" for a in aliases)
        )
        if label != _METRIC_WITH_CALIBER:
            conditions += " AND d.datasource_id = $datasource_id"
        branches.append(
            f"MATCH {pattern} WHERE {conditions} "
            f"RETURN DISTINCT '{label}' AS label, {result}.key AS node_key"
        )
    for row in queries.read(
        branches,
        {"datasource_id": ds},
        columns=("label", "node_key"),
    ):
        out[row["label"]].append(row["node_key"])
    # Preserve the existing no-Dataset behavior, including orphan Metrics.
    if not out["Dataset"]:
        out[_METRIC_WITH_CALIBER] = []
    return out


def closure_predicate(alias, label):
    condition = (
        f"{alias}.key IN $closure.{label}"
        if label in CLOSURE_LABELS
        else ds_ownership_cypher(alias)
    )
    if label == "Metric":
        condition += f" OR NOT {alias}.key IN $closure.{_METRIC_WITH_CALIBER}"
    return f"($datasource_id = '' OR {condition})"
