"""Verify atomic CSV publication, workspace isolation and expiration."""

import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.sql.artifact import (
    save_sql_result,
    sql_result_path,
)

from cm.infrastructure.access.support import authorization


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    return tmp_path, GraphScopeAuthority().issue_read_scope(authorization())


@pytest.mark.parametrize(
    "changed",
    [
        {"tenant_id": "other"},
        {"workspace_id": "other"},
    ],
)
def test_sql_files_are_isolated_by_tenant_and_workspace(artifacts, changed):
    _, scope = artifacts
    result_id = save_sql_result(("value",), ((1,),), scope=scope)
    other = GraphScopeAuthority().issue_read_scope(authorization(**changed))
    with pytest.raises(FileNotFoundError):
        sql_result_path(result_id, scope=other)
    assert sql_result_path(result_id, scope=scope).is_file()


def test_download_rejects_symlink_files_in_the_workspace(artifacts):
    root, scope = artifacts
    result_id = save_sql_result(("x",), ((1,),), scope=scope)
    path = sql_result_path(result_id, scope=scope)
    private = root / "private.csv"
    private.write_text("private")
    path.unlink()
    path.symlink_to(private)
    with pytest.raises(FileNotFoundError):
        sql_result_path(result_id, scope=scope)


def test_failed_save_removes_temporary_file(artifacts):
    root, scope = artifacts

    def rows():
        yield (1,)
        raise OSError("save failed")

    with pytest.raises(OSError, match="save failed"):
        save_sql_result(("value",), rows(), scope=scope)
    assert not [path for path in root.rglob("*") if path.is_file()]
