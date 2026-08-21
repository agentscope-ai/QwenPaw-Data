"""Tests for the bundled demo assets."""

from __future__ import annotations

from context_manager.demo import (
    seed_postgres_sql,
    semantic_workbook_bytes,
)
from context_manager.demo.generate import EXPECTED_ROW_COUNT, demo_rows


def test_semantic_workbook_bytes_returns_nonempty_bytes() -> None:
    payload = semantic_workbook_bytes()
    assert isinstance(payload, bytes)
    assert len(payload) > 0


def test_seed_postgres_sql_contains_expected_schema_and_row_count() -> None:
    sql = seed_postgres_sql()
    assert "CREATE TABLE dws_gaap_di" in sql
    assert "DROP TABLE IF EXISTS gaap_metrics" in sql
    assert f"-- inserted {EXPECTED_ROW_COUNT} rows" in sql

    # Rough sanity check: each data row starts with "    ('" and ends with
    # ")," or ");" in the INSERT block.
    insert_lines = [
        line for line in sql.splitlines()
        if line.startswith("    ('") and line.endswith((",", ");"))
    ]
    assert len(insert_lines) == EXPECTED_ROW_COUNT


def test_demo_rows_are_deterministic_and_match_expected_count() -> None:
    rows = demo_rows()
    assert len(rows) == EXPECTED_ROW_COUNT
    assert rows == demo_rows()  # deterministic under fixed seed

    # Every row has 7 columns and non-empty identifiers.
    for row in rows:
        assert len(row) == 7
        assert row[0]  # ds
        assert row[4].startswith("u")  # user_id
