#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end tests for the DataPaw CLI.

The script invokes the real CLI entrypoint in subprocesses with an isolated
DATAPAW_HOME. The CLI loads its model configuration from dotenv. The script
requires MCP configuration and a pre-provisioned CM datasource id.

Usage:
    python scripts/e2e_cli.py --query "Analyze this dataset..."
    python scripts/e2e_cli.py --python /path/to/python --query "Analyze this dataset..."
    python scripts/e2e_cli.py --output-dir tmp/e2e-cli --query "Analyze this dataset..."
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MCP_CONFIG_ENV = "DATAPAW_E2E_MCP_CONFIG"
DATASOURCE_ID_ENV = "DATAPAW_E2E_DATASOURCE_ID"
MASKED_SECRET = "******"
SENSITIVE_CONFIG_FIELDS = frozenset(
    {"password", "access_key_id", "access_key_secret", "sts_token"},
)


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def info(message: str) -> None:
    print(f"{Colors.BLUE}==>{Colors.NC} {message}")


def success(message: str) -> None:
    print(f"{Colors.GREEN}OK{Colors.NC} {message}")


def warning(message: str) -> None:
    print(f"{Colors.YELLOW}WARN{Colors.NC} {message}")


def fail(message: str) -> None:
    print(f"{Colors.RED}FAIL{Colors.NC} {message}")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def src_paths(root: Path) -> list[Path]:
    return [
        root / "packages" / "datapaw-host-core" / "src",
        root / "packages" / "datapaw-cli" / "src",
        root / "packages" / "datapaw-context" / "src",
    ]


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_env(names: list[str] | tuple[str, ...]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise AssertionError("missing required env vars: " + ", ".join(missing))


def required_env_names() -> tuple[str, ...]:
    return (
        MCP_CONFIG_ENV,
        DATASOURCE_ID_ENV,
    )


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class StepResult:
    name: str
    status: str
    duration_s: float
    error: str = ""


class CliE2ERunner:
    def __init__(
        self,
        *,
        root: Path,
        python: Path,
        keep_temp: bool = False,
        output_dir: Path | None = None,
        verbose: bool = False,
    ) -> None:
        self.root = root
        self.python = python
        self.keep_temp = keep_temp or output_dir is not None
        self.output_dir = output_dir
        self.verbose = verbose
        if output_dir is None:
            self.temp_dir = Path(tempfile.mkdtemp(prefix="datapaw-cli-e2e-"))
        else:
            self.temp_dir = output_dir.expanduser().resolve()
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.home = self.temp_dir / "home"
        self.work = self.temp_dir / "work"
        self.home.mkdir(exist_ok=True)
        self.work.mkdir(exist_ok=True)

    def close(self) -> None:
        if self.keep_temp:
            label = "output directory" if self.output_dir is not None else "temp directory"
            warning(f"kept {label}: {self.temp_dir}")
            return
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        package_path = os.pathsep.join(str(path) for path in src_paths(self.root))
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            package_path
            if not existing_pythonpath
            else f"{package_path}{os.pathsep}{existing_pythonpath}"
        )
        env["DATAPAW_HOME"] = str(self.home)
        return env

    def run_cli(
        self,
        args: list[str],
        *,
        expected: int = 0,
        timeout: int | None = 30,
    ) -> CommandResult:
        command = [
            str(self.python),
            "-c",
            (
                "import sys; "
                "from datapaw.cli import main; "
                "raise SystemExit(main(sys.argv[1:]))"
            ),
            *args,
        ]
        if self.verbose:
            info("running: datapaw " + " ".join(args))
        completed = subprocess.run(
            command,
            cwd=self.root,
            env=self.env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        result = CommandResult(
            args=args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if result.returncode != expected:
            self._print_result(result)
            raise AssertionError(
                f"datapaw {' '.join(args)} returned "
                f"{result.returncode}, expected {expected}",
            )
        if self.verbose:
            self._print_result(result)
        return result

    @staticmethod
    def _print_result(result: CommandResult) -> None:
        print(f"$ datapaw {' '.join(result.args)}")
        print(f"exit: {result.returncode}")
        if result.stdout:
            print("--- stdout ---")
            print(result.stdout.rstrip())
        if result.stderr:
            print("--- stderr ---")
            print(result.stderr.rstrip())

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.work / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def session_files(self) -> set[Path]:
        session_root = (
            self.home / "host" / "workspace" / "sessions" / "console"
        )
        if not session_root.exists():
            return set()
        return set(session_root.glob("*.json"))

    def assert_new_session_file(self, before: set[Path], label: str) -> None:
        created = self.session_files() - before
        assert_true(created, f"{label} did not create a session file")
        if self.verbose:
            for path in sorted(created):
                info(f"{label} session: {path}")

    def mcp_config(self) -> list[dict[str, Any]]:
        require_env((MCP_CONFIG_ENV,))
        try:
            config = json.loads(os.environ[MCP_CONFIG_ENV])
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{MCP_CONFIG_ENV} must be a JSON array") from exc
        if not isinstance(config, list) or not config:
            raise AssertionError(f"{MCP_CONFIG_ENV} must be a non-empty JSON array")
        if not all(isinstance(item, dict) for item in config):
            raise AssertionError(f"{MCP_CONFIG_ENV} entries must be JSON objects")
        return config

    def datasource_id(self) -> str:
        require_env((DATASOURCE_ID_ENV,))
        value = os.environ[DATASOURCE_ID_ENV].strip()
        if not value:
            raise AssertionError(f"{DATASOURCE_ID_ENV} must not be empty")
        return value

    def test_help(self) -> None:
        result = self.run_cli(["--help"])
        stdout = result.stdout
        for token in ("plan", "execute", "run", "chat", "datasource"):
            assert_true(token in stdout, f"--help missing command {token}")
        assert_true("mcp" not in stdout, "--help unexpectedly exposes mcp")
        assert_true("data-source" not in stdout, "--help unexpectedly exposes data-source")
        assert_true("serve" not in stdout, "--help unexpectedly exposes serve")

        for command in ("plan", "execute", "run", "chat"):
            self.run_cli([command, "--help"])
        self.run_cli(["datasource", "--help"])
        datasource_help = self.run_cli(["datasource", "list", "--help"])
        assert_true(
            "--base-url" not in datasource_help.stdout,
            "datasource list unexpectedly exposes --base-url",
        )
        success("help output covers expected commands")

    def test_datasource_list(self, *, datasource_id: str) -> None:
        result = self.run_cli(["datasource", "list"])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError("datasource list did not return JSON") from exc

        items = payload.get("items") if isinstance(payload, dict) else None
        assert_true(isinstance(items, list), "datasource list missing items")
        assert_true(payload.get("total") == len(items), "datasource total mismatch")
        assert_true(
            any(item.get("datasource_id") == datasource_id for item in items),
            f"configured datasource id not returned: {datasource_id}",
        )
        for item in items:
            config = item.get("config")
            if not isinstance(config, dict):
                continue
            for field in SENSITIVE_CONFIG_FIELDS:
                value = config.get(field)
                assert_true(
                    value in (None, "", MASKED_SECRET),
                    f"datasource list exposed unmasked {field}",
                )
        success("CM datasource discovery completed")

    def setup_mcp_config(self) -> None:
        """Write MCP config directly; the public CLI no longer exposes ``mcp import``."""
        mcp_config = self.mcp_config()
        target = self.home / "host" / "workspace" / ".mcp"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(mcp_config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        imported = json.loads(target.read_text(encoding="utf-8"))
        assert_true(len(imported) == len(mcp_config), "target .mcp entry count mismatch")
        assert_true(
            [item.get("name") for item in imported] == [item.get("name") for item in mcp_config],
            "target .mcp client names mismatch",
        )
        success("wrote env-provided MCP config to workspace")

    def test_model_plan(self, query: str, *, datasource_id: str) -> Path:
        before = self.session_files()
        output = self.work / "plan.yaml"
        self.run_cli(
            ["plan", query, "--output", str(output), "--datasource-id", datasource_id],
            timeout=None,
        )
        assert_true(output.is_file(), "plan command did not write output file")
        text = output.read_text(encoding="utf-8")
        assert_true("nodes:" in text, "plan output does not look like SOP YAML")
        self.assert_new_session_file(before, "plan")
        success("model-backed plan completed")
        return output

    def test_model_execute(self, sop_path: Path, *, datasource_id: str) -> None:
        before = self.session_files()
        self.run_cli(
            ["execute", str(sop_path), "--no-stream", "--datasource-id", datasource_id],
            timeout=None,
        )
        self.assert_new_session_file(before, "execute")
        success("model-backed execute completed")

    def test_model_run(self, query: str, *, datasource_id: str) -> None:
        before = self.session_files()
        self.run_cli(
            ["run", query, "--no-stream", "--datasource-id", datasource_id],
            timeout=None,
        )
        self.assert_new_session_file(before, "run")
        success("model-backed run completed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DataPaw CLI end-to-end tests.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable used to invoke the CLI subprocesses.",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Real user query passed to `datapaw plan`.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the isolated DATAPAW_HOME temp directory after the run.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write DATAPAW_HOME and work outputs under this directory; implies --keep-temp.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print subprocess stdout/stderr for passing commands.",
    )
    args = parser.parse_args()
    args.query = args.query.strip()
    if not args.query:
        parser.error("--query must not be empty")
    return args


def format_duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f}m ({seconds:.1f}s)"
    return f"{seconds:.2f}s"


def print_test_results(results: list[StepResult]) -> None:
    print("")
    info("Test results")
    if not results:
        print("No test steps recorded.")
        return

    name_width = max(len("test"), *(len(result.name) for result in results))
    print(f"{'test'.ljust(name_width)}  status  duration")
    print(f"{'-' * name_width}  ------  --------")
    for result in results:
        status = "PASS" if result.status == "passed" else "FAIL"
        color = Colors.GREEN if result.status == "passed" else Colors.RED
        print(
            f"{result.name.ljust(name_width)}  "
            f"{color}{status.ljust(6)}{Colors.NC}  "
            f"{format_duration(result.duration_s)}",
        )

    passed = sum(1 for result in results if result.status == "passed")
    failed = len(results) - passed
    total = sum(result.duration_s for result in results)
    print(f"total: {passed} passed, {failed} failed, {format_duration(total)}")
    for result in results:
        if result.error:
            print(f"failure: {result.name}: {result.error}")


def main() -> int:
    args = parse_args()
    root = repo_root()
    runner = CliE2ERunner(
        root=root,
        python=args.python,
        keep_temp=args.keep_temp,
        output_dir=args.output_dir,
        verbose=args.verbose,
    )

    info("DataPaw CLI E2E")
    info(f"repo: {root}")
    info(f"python: {args.python}")
    info(f"DATAPAW_HOME: {runner.home}")

    results: list[StepResult] = []

    def run_step(name: str, func: Any) -> Any:
        info(f"test: {name}")
        started = time.perf_counter()
        try:
            value = func()
        except Exception as exc:
            results.append(
                StepResult(
                    name=name,
                    status="failed",
                    duration_s=time.perf_counter() - started,
                    error=str(exc),
                ),
            )
            raise
        results.append(
            StepResult(
                name=name,
                status="passed",
                duration_s=time.perf_counter() - started,
            ),
        )
        return value

    try:
        run_step("environment", lambda: require_env(required_env_names()))
        run_step("help", runner.test_help)
        run_step("mcp config", runner.setup_mcp_config)
        datasource_id = runner.datasource_id()
        run_step(
            "datasource list",
            lambda: runner.test_datasource_list(datasource_id=datasource_id),
        )
        plan_path = run_step(
            "plan",
            lambda: runner.test_model_plan(args.query, datasource_id=datasource_id),
        )
        run_step(
            "execute",
            lambda: runner.test_model_execute(plan_path, datasource_id=datasource_id),
        )
        run_step(
            "run",
            lambda: runner.test_model_run(args.query, datasource_id=datasource_id),
        )
    except Exception as exc:  # pylint: disable=broad-except
        fail(str(exc))
        if not runner.keep_temp:
            warning(f"rerun with --keep-temp to inspect {runner.temp_dir}")
        return 1
    finally:
        print_test_results(results)
        runner.close()

    success("all CLI E2E checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
