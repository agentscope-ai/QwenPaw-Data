#!/usr/bin/env python3
"""Deterministic smoke test for the datasource and semantic CLI commands.

Complements ``smoke_test.py`` (task-execution surface) by exercising the
configuration-management surface end to end against a real DataBridge,
PostgreSQL, and Neo4j — no model API key required: the weave step only
needs the graph store, and its embedding indexing is non-fatal without
an embedding endpoint.

Chain: datasource test/create/get/update → workbook import → semantic
CRUD with partial update → dataset-level batch delete → weave submit
--wait → cleanup, asserting masked credentials and fail-closed deletes
along the way.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from init_demo import load_repo_environment, seed_postgres
from smoke_test import (
    _free_port,
    _postgres_config,
    _run_cli,
    _stop_process,
    _wait_for_health,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKBOOK = REPO_ROOT / "examples" / "demo_semantic_config.xlsx"
WORKBOOK_DATASOURCE_ID = "postgresql-demo-gaap"


def _cli_json(env: dict[str, str], *args: str, timeout: float = 90) -> Any:
    completed = _run_cli(env, *args, timeout=timeout)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"CLI did not emit JSON for {' '.join(args)}:\n{completed.stdout}",
        ) from exc


def _run_cli_expect_failure(
    env: dict[str, str], *args: str, timeout: float = 90
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "datapaw.cli.main", *args]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        # No TTY stdin: interactive confirmations must fail closed instead of
        # blocking on input() when the smoke test runs from a terminal.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode == 0:
        raise RuntimeError(
            f"{' '.join(command)} unexpectedly succeeded:\n{completed.stdout}",
        )
    return completed


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _check_datasource_lifecycle(env: dict[str, str], config: dict[str, Any]) -> str:
    config_json = json.dumps(config)

    tested = _cli_json(
        env,
        "datasource", "test",
        "--type", "postgresql",
        "--config", config_json,
    )
    _expect(tested.get("success") is True, f"ad-hoc connection test failed: {tested}")

    created = _cli_json(
        env,
        "datasource", "create",
        "--name", "semantic-smoke-pg",
        "--type", "postgresql",
        "--config", config_json,
        "--test",
    )
    datasource_id = created.get("datasource_id") or ""
    _expect(bool(datasource_id), f"create returned no datasource_id: {created}")
    _expect(
        created.get("config", {}).get("password") in (None, "******"),
        "create echoed a plaintext password",
    )

    shown = _cli_json(env, "datasource", "get", datasource_id, "--show-config")
    _expect(
        config["password"] not in json.dumps(shown),
        "get --show-config leaked the plaintext password",
    )

    renamed = _cli_json(
        env, "datasource", "update", datasource_id, "--name", "semantic-smoke-renamed",
    )
    _expect(
        renamed.get("datasource_name") == "semantic-smoke-renamed",
        f"update did not rename the datasource: {renamed}",
    )
    after = _cli_json(env, "datasource", "get", datasource_id, "--show-config")
    _expect(
        after.get("config", {}).get("dbname") == config["dbname"],
        "update dropped the stored connection config",
    )

    saved = _cli_json(env, "datasource", "test", datasource_id)
    _expect(saved.get("success") is True, f"saved connection test failed: {saved}")

    # Deletion is fail-closed without --yes outside a TTY.
    refused = _run_cli_expect_failure(env, "datasource", "delete", datasource_id)
    _expect("--yes" in refused.stderr, f"non-TTY delete was not refused: {refused.stderr}")
    return datasource_id


def _check_semantic_crud(env: dict[str, str]) -> None:
    imported = _cli_json(env, "semantic", "import", "--file", str(WORKBOOK))
    _expect(imported.get("success") is True, f"workbook import failed: {imported}")
    summary = imported.get("summary") or {}
    _expect(summary.get("metric") == 1, f"unexpected import summary: {summary}")

    metrics = _cli_json(
        env, "semantic", "metric", "list", "--datasource-id", WORKBOOK_DATASOURCE_ID,
    )
    _expect(metrics.get("total") == 1, f"expected 1 imported metric: {metrics}")
    domain_id = metrics["items"][0]["domain_id"]

    created = _cli_json(
        env,
        "semantic", "metric", "create",
        "--datasource-id", WORKBOOK_DATASOURCE_ID,
        "--domain-id", str(domain_id),
        "--name", "smoke_metric",
        "--unit", "CNY",
        "--description", "original",
    )
    metric_id = created["id"]

    # Partial update must keep the fields that were not sent (regression for
    # the repository-level full-column UPDATE bug).
    updated = _cli_json(
        env,
        "semantic", "metric", "update", str(metric_id),
        "--description", "changed",
    )
    _expect(updated.get("description") == "changed", f"update failed: {updated}")
    _expect(
        updated.get("metric_name") == "smoke_metric" and updated.get("unit") == "CNY",
        f"partial update erased omitted fields: {updated}",
    )

    _cli_json(env, "semantic", "metric", "delete", str(metric_id), "--yes")

    bindings = _cli_json(
        env, "semantic", "binding", "list", "--datasource-id", WORKBOOK_DATASOURCE_ID,
    )
    _expect(bindings.get("total", 0) > 0, "workbook import created no bindings")
    dataset_id = bindings["items"][0]["dataset_id"]
    _cli_json(
        env, "semantic", "binding", "delete", "--dataset-id", str(dataset_id), "--yes",
    )
    remaining = _cli_json(
        env, "semantic", "binding", "list", "--dataset-id", str(dataset_id),
    )
    _expect(remaining.get("total") == 0, f"batch delete left bindings: {remaining}")

    # Restore the workbook state so the weave publishes the full bundle.
    _cli_json(env, "semantic", "import", "--file", str(WORKBOOK))


def _check_weave(env: dict[str, str], *, timeout: float) -> None:
    task = _cli_json(
        env,
        "semantic", "weave", "submit",
        "--datasource-id", WORKBOOK_DATASOURCE_ID,
        "--mode", "FULL",
        "--name", "semantic-smoke-weave",
        "--wait", "--timeout", str(timeout),
        timeout=timeout + 60,
    )
    _expect(
        str(task.get("status") or "").lower() == "success",
        f"weave did not succeed: {task}",
    )

    listed = _cli_json(env, "semantic", "weave", "list")
    _expect(
        any(
            record.get("task_id") == task.get("task_id")
            for record in listed.get("items", [])
        ),
        f"submitted weave task missing from list: {listed}",
    )


def _cleanup(env: dict[str, str], datasource_id: str) -> None:
    _cli_json(env, "datasource", "delete", datasource_id, "--yes")

    # The workbook datasource is referenced by a domain, so deletion must be
    # rejected by the fail-closed reference check.
    refused = _run_cli_expect_failure(
        env, "datasource", "delete", WORKBOOK_DATASOURCE_ID, "--yes",
    )
    _expect(
        "数据源被引用" in refused.stderr or "referenced" in refused.stderr.lower(),
        f"referenced datasource delete was not rejected: {refused.stderr}",
    )


def run_smoke(args: argparse.Namespace) -> None:
    seed_postgres(args.postgres_dsn)
    postgres = _postgres_config(args.postgres_dsn)
    cm_port = _free_port()

    with tempfile.TemporaryDirectory(prefix="datapaw-semantic-smoke-") as temp_name:
        temp_root = Path(temp_name)
        home = temp_root / "home"
        log_path = temp_root / "databridge.log"
        env = dict(os.environ)
        env.update(
            {
                "DATAPAW_HOME": str(home),
                "DATAPAW_CM_BASE_URL": f"http://127.0.0.1:{cm_port}",
                "NEO4J_URI": args.neo4j_uri,
                "NEO4J_USER": args.neo4j_user,
                "NEO4J_PASSWORD": args.neo4j_password,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "DATAPAW_API_TOKEN": "",
                "DATAPAW_API_KEYS": "",
                "DATAPAW_CLIENT_API_TOKEN": "",
                "NEO4J_DATABASE": "",
            },
        )

        with log_path.open("w", encoding="utf-8") as log_file:
            bridge = subprocess.Popen(
                [
                    sys.executable,
                    str(
                        REPO_ROOT
                        / "packages"
                        / "datapaw-context"
                        / "scripts"
                        / "serve.py"
                    ),
                    "--port",
                    str(cm_port),
                    "--log-level",
                    "warning",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_health(
                    f"http://127.0.0.1:{cm_port}/api/health",
                    bridge,
                    args.startup_timeout,
                )
                datasource_id = _check_datasource_lifecycle(env, postgres)
                _check_semantic_crud(env)
                if args.skip_weave:
                    print("weave step skipped (--skip-weave)")
                else:
                    _check_weave(env, timeout=args.weave_timeout)
                _cleanup(env, datasource_id)
            except BaseException as exc:
                try:
                    log_file.flush()
                    bridge_log = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    bridge_log = "<unavailable>"
                raise RuntimeError(
                    f"{exc}\nDataBridge log:\n{bridge_log[-8000:]}"
                ) from exc
            finally:
                _stop_process(bridge)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-dsn",
        default=("postgresql://datapaw:datapaw-demo@127.0.0.1:55432/datapaw_demo"),
    )
    parser.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument(
        "--neo4j-password",
        default=os.getenv("NEO4J_PASSWORD", "datapaw-demo"),
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--weave-timeout", type=float, default=240.0)
    parser.add_argument(
        "--skip-weave",
        action="store_true",
        help="Skip the weave publish step (no graph store available)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_repo_environment()
    try:
        run_smoke(parse_args(argv))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"DataPaw semantic CLI smoke failed: {exc}", file=sys.stderr)
        return 1
    print("DataPaw semantic CLI smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
