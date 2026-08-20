# DataPaw GAAP Demo Assets

This document describes the bundled GAAP demo assets shipped inside
`datapaw-context`. The assets are used by the docker-compose one-shot demo in
the main QwenPaw repository.

## Asset inventory

| File | Description | Consumer |
|---|---|---|
| `context_manager/demo/assets/demo_semantic_config.xlsx` | Semantic-layer workbook (domains, metrics, dimensions, bindings) | `datapaw semantic import` |
| `context_manager/demo/assets/seed-postgres.sql` | Idempotent PostgreSQL seed script: schema + 475 GAAP rows | `psql` or `datapaw demo seed` |

## Why the assets live here

The demo must work without a local `QwenPaw-Data` source checkout. Shipping the
assets inside `datapaw-context` means any environment with the PyPI package can
locate them via `importlib.resources`:

```python
from context_manager.demo.loader import seed_postgres_sql, semantic_workbook_bytes

sql = seed_postgres_sql()
workbook_bytes = semantic_workbook_bytes()
```

`datapaw-context` was chosen because the context service consumes both assets:
the SQL seed populates the relational datasource, and the workbook is imported
into the semantic config before being woven into Neo4j.

## Regenerating the seed SQL

The seed SQL is derived from the canonical demo generator in
`examples/init_demo.py`. When the demo schema or data changes, refresh the
bundled SQL file:

```bash
cd packages/datapaw-context
PYTHONPATH=src python -m context_manager.demo.generate
```

This overwrites `src/context_manager/demo/assets/seed-postgres.sql` with the
latest schema and exactly `EXPECTED_ROW_COUNT` (475) rows.

## Adding new assets

1. Place the file under `src/context_manager/demo/assets/`.
2. Expose a loader in `context_manager/demo/loader.py`.
3. Update this document.
4. Bump `datapaw-context` and publish a new wheel.

## Release note

Demo assets were introduced in `datapaw-context==0.2.1`.
