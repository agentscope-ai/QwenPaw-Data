from __future__ import annotations

from pathlib import Path

import pytest

from context_manager.api import cm_sql_results
from context_manager.api.executor import ExecResult
from qwenpaw_data.context.errors import ResourceBudgetExceeded


def test_sql_cache_expires_when_backing_file_disappears(tmp_path: Path) -> None:
    result_file = tmp_path / "result.csv"
    result_file.write_text("value\n1\n")
    cache = cm_sql_results.SqlResultCache()
    result = ExecResult(sql="SELECT 1", columns=["value"], rows=[[1]], row_count=1)

    cache.put("SELECT 1", 10, result, "success", "/result.csv", result_file)
    assert cache.get("SELECT 1", 10) is not None

    result_file.unlink()
    assert cache.get("SELECT 1", 10) is None


def test_csv_budget_failure_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm_sql_results, "sql_downloads_dir", lambda: tmp_path)
    monkeypatch.setenv("QWENPAW_DATA_MAX_RESPONSE_MB", "1")

    with pytest.raises(ResourceBudgetExceeded):
        cm_sql_results.save_sql_results_to_csv(["value"], [["x" * (2 * 1024 * 1024)]])

    assert list(tmp_path.iterdir()) == []


def test_old_import_contract_path_is_compatible() -> None:
    from context_manager.api.import_models import ImportRequest as OldImportRequest
    from context_manager.contracts.import_models import ImportRequest

    assert OldImportRequest is ImportRequest


def test_sql_cache_touch_renews_at_most_once() -> None:
    """A popular query must not renew its TTL forever (stale rows served as fresh)."""
    cache = cm_sql_results.SqlResultCache()
    result = ExecResult(sql="SELECT 1", columns=["value"], rows=[[1]], row_count=1)
    cache.put("SELECT 1", 10, result, "success", None, Path("x.csv"), ttl=1000)
    key = cache._make_key("SELECT 1", 10)
    e = cache._store[key]
    first_created = e.created_at

    e.touch()
    assert e.touched
    assert e.created_at >= first_created  # renewed once
    renewed = e.created_at

    e.touch()
    assert e.created_at == renewed  # second touch is a no-op


def test_sql_cache_does_not_cross_datasources(tmp_path: Path) -> None:
    cache = cm_sql_results.SqlResultCache()
    path = tmp_path / "result.csv"
    path.write_text("value\n1\n")
    result = ExecResult(
        sql="SELECT * FROM t", columns=["value"], rows=[[1]], row_count=1
    )
    cache.put(result.sql, 10, result, "success", "/result.csv", path, datasource_id="a")
    assert cache.get(result.sql, 10, datasource_id="a") is not None
    assert cache.get(result.sql, 10, datasource_id="b") is None
    assert cache.get(result.sql, 10) is None


def test_popular_cache_eventually_expires_and_cannot_revive(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(cm_sql_results.time, "time", lambda: clock[0])
    path = tmp_path / "data.csv"
    path.write_text("a\n1\n")
    cache = cm_sql_results.SqlResultCache()
    result = ExecResult(sql="SELECT 1")
    entry = cache.put(
        result.sql, 10, result, "success", None, path, ttl=10, datasource_id="a"
    )
    for now in [9, 15, 18]:
        clock[0] = now
        assert cache.get(result.sql, 10, datasource_id="a") is entry
        entry.touch()
    clock[0] = 20
    entry.touch()
    assert cache.get(result.sql, 10, datasource_id="a") is None
    expired = cache.put(result.sql, 10, result, "success", None, path, ttl=10)
    clock[0] = 31
    expired.touch()
    assert expired.is_expired()
