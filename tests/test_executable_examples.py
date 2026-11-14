from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_init_demo() -> ModuleType:
    path = ROOT / "examples" / "init_demo.py"
    spec = importlib.util.spec_from_file_location(
        "qwenpaw_data_example_init_demo", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_demo_is_repeatable_and_has_expected_aggregates(tmp_path: Path) -> None:
    demo = _load_init_demo()
    target = tmp_path / "nested" / "demo.sqlite"

    demo.seed_sqlite(target)
    demo.seed_sqlite(target)

    assert demo.query_sqlite(target) == demo.EXPECTED_DAILY_AVERAGES
    with sqlite3.connect(target) as connection:
        count = connection.execute("SELECT COUNT(*) FROM dws_gaap_di").fetchone()
    assert count == (demo.EXPECTED_ROW_COUNT,)


def test_demo_datasource_example_matches_local_compose_contract() -> None:
    payload = json.loads(
        (ROOT / "examples/demo/postgres/datasource.example.json").read_text(),
    )
    assert payload == {
        "datasource_name": "Demo PG - GAAP use case",
        "datasource_type": "postgresql",
        "config": {
            "host": "127.0.0.1",
            "port": 55432,
            "dbname": "qwenpaw_data_demo",
            "user": "qwenpaw_data",
            "password": "qwenpaw-data-demo",
        },
    }


def test_demo_environment_loader_preserves_explicit_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    demo = _load_init_demo()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEMO_FROM_FILE=loaded\nDEMO_EXPLICIT=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEMO_FROM_FILE", raising=False)
    monkeypatch.setenv("DEMO_EXPLICIT", "from-process")

    demo.load_repo_environment(env_file)

    assert os.environ["DEMO_FROM_FILE"] == "loaded"
    assert os.environ["DEMO_EXPLICIT"] == "from-process"


def test_demo_workbook_matches_current_import_contract():
    from semantic_config.services.excel_import_service import _read_sheets

    sheets, errors = _read_sheets(
        (ROOT / "examples/demo_semantic_config.xlsx").read_bytes()
    )
    assert errors == []
    assert len(sheets["dataset_column"]) == 7
    assert len(sheets["dataset_dimension"]) == 4
    assert sheets["dataset"][0]["source"] == "dws_gaap_di"
    assert (
        sheets["metric_formula"][0]["expr"]
        == "AVG(CASE WHEN ytd_gaap >= 10 THEN gaap_val END)"
    )
    assert sheets["metric_formula"][0]["time_window"] == "1d"


@pytest.mark.parametrize("exists", [False, True])
def test_demo_registers_before_import_and_binds_returned_id(monkeypatch, exists):
    demo = _load_init_demo()
    calls = []
    record = {
        "datasource_id": "postgresql-generated",
        "datasource_name": demo.DEMO_DATASOURCE_NAME,
        "datasource_type": "postgresql",
    }

    def request_json(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("method", "GET") == "GET":
            return {"records": [record] if exists else [], "total": int(exists)}
        return record

    def request_file(url, path, **kwargs):
        assert len(calls) == 2
        assert kwargs["fields"] == {"datasource_id": record["datasource_id"]}
        return {"success": True, "summary": {"metric": 1}}

    monkeypatch.setattr(demo, "_request_json", request_json)
    monkeypatch.setattr(demo, "_request_file", request_file)
    assert (
        demo.configure_demo_bundle("http://localhost:8765", 55432)
        == record["datasource_id"]
    )
    assert calls[1][1]["method"] == ("PUT" if exists else "POST")


def test_demo_refuses_ambiguous_existing_registration(monkeypatch):
    demo = _load_init_demo()
    monkeypatch.setattr(
        demo, "_request_json", lambda *a, **k: {"records": [], "total": 2}
    )
    with pytest.raises(RuntimeError, match="Multiple demo datasources"):
        demo.configure_demo_bundle("http://localhost:8765", 55432)


def test_demo_entrypoints_are_executable() -> None:
    entrypoints = (
        "examples/init_demo.py",
        "examples/init_demo.sh",
        "examples/init_demo.ps1",
        "examples/smoke_test.py",
    )
    for relative in entrypoints:
        assert (ROOT / relative).is_file(), relative
    if os.name != "nt":
        for relative in entrypoints:
            if not relative.endswith(".ps1"):
                assert (ROOT / relative).stat().st_mode & 0o111, relative

    powershell = (ROOT / "examples/init_demo.ps1").read_text(encoding="utf-8")
    assert "init_demo.py" in powershell
    assert '"compose", "-f"' in powershell
    assert "--postgres-dsn" in powershell
    assert '"--register"' in powershell


def _load_smoke(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    import smoke_test

    return smoke_test


def test_smoke_environment_does_not_reuse_caller_state(tmp_path, monkeypatch):
    smoke = _load_smoke(monkeypatch)
    for key in (
        "QWENPAW_DATA_DB_URL",
        "QWENPAW_DATA_ENV_FILE",
        "QWENPAW_DATA_AUTH_SECRET",
        "OPENAI_API_KEY",
        "HOLO_AGE_PASSWORD",
        "GRAPH_BACKEND",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "must-not-be-inherited")
    env = smoke._smoke_environment(
        tmp_path,
        8765,
        SimpleNamespace(
            neo4j_uri="bolt://127.0.0.1:7687",
            neo4j_user="neo4j",
            neo4j_password="fixture",
        ),
    )
    assert "must-not-be-inherited" not in env.values()
    assert (
        env["QWENPAW_DATA_DB_URL"] == f"sqlite+aiosqlite:///{tmp_path}/control.sqlite"
    )
    assert env["QWENPAW_DATA_AUTH_MODE"] == "local"
    assert env["GRAPH_BACKEND"] == "neo4j"
    assert not Path(env["QWENPAW_DATA_ENV_FILE"]).exists()


def test_smoke_host_bootstraps_shared_schema_and_stops(tmp_path, monkeypatch):
    smoke = _load_smoke(monkeypatch)
    env = smoke._smoke_environment(
        tmp_path,
        8765,
        SimpleNamespace(
            neo4j_uri="bolt://127.0.0.1:1",
            neo4j_user="neo4j",
            neo4j_password="fixture",
        ),
    )
    # A real loopback Host process, with no Context/graph/model running.
    with smoke._host_service(env, tmp_path, 30):
        with sqlite3.connect(
            f"file:{tmp_path / 'control.sqlite'}?mode=ro", uri=True
        ) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"users", "user_providers", "sessions", "chats", "chat_events"} <= tables
        assert smoke._host_json(env, "/health")["status"] == "ok"
    import socket
    from urllib.parse import urlparse

    address = urlparse(env["QWENPAW_DATA_HOST_URL"])
    with socket.socket() as sock:
        assert sock.connect_ex((address.hostname, address.port)) != 0
