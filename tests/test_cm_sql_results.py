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
