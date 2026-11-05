import json

import pytest
from qwenpaw_data.host.core.sql_results import save_sql_result, load_sql_result


def test_reference_roundtrip_and_no_preview_or_secrets(tmp_path):
    csv = tmp_path / "query.csv"
    csv.write_text("value\n1\n")
    result = {
        "exec_status": "success",
        "sql": "SELECT value FROM t",
        "truncated": False,
        "total_row_count": 1,
        "rows": [[1]],
        "access_token": "private",
    }
    ref = save_sql_result(result, artifact_dir=tmp_path, csv_path=csv)
    resolved = load_sql_result(ref, artifact_dir=tmp_path)
    assert resolved["file_path"] == str(csv) and resolved["total_row_count"] == 1
    assert "rows" not in resolved and "access_token" not in resolved
    with pytest.raises(ValueError, match="not available in this session"):
        load_sql_result(ref, artifact_dir=tmp_path / "other-session")
    csv.write_text("value\n999\n")
    with pytest.raises(ValueError, match="has changed"):
        load_sql_result(ref, artifact_dir=tmp_path)


def test_reference_rejects_traversal_and_symlinks(tmp_path):
    with pytest.raises(ValueError):
        load_sql_result("../../secrets", artifact_dir=tmp_path)
    csv = tmp_path / "query.csv"
    csv.write_text("x\n1\n")
    ref = save_sql_result({}, artifact_dir=tmp_path, csv_path=csv)
    record = tmp_path / "data/results" / f"{ref}.json"
    content = json.loads(record.read_text())
    content["csv"] = "../outside.csv"
    record.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        load_sql_result(ref, artifact_dir=tmp_path)
    content["csv"] = "link.csv"
    (tmp_path / "link.csv").symlink_to(csv)
    record.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        load_sql_result(ref, artifact_dir=tmp_path)
