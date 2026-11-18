"""Contract of the report-reproduction workflow CLI.

The host no longer owns any report semantics, so this script is the only thing
standing between the agent and the artifact tree. What has to hold:

* every subcommand prints exactly one JSON object on stdout and never a
  traceback, because the caller is a model reading stdout;
* every write is confined to ``--artifact-dir``, and every identifier that
  becomes a path component is validated rather than silently rewritten;
* the machine verdict and the human verdict stay in separate hands:
  ``compare-section`` computes, ``apply-comparison`` decides;
* one chat advances one Section (or one target period), which is what makes
  per-Section human review possible at all;
* artifacts written by the previous host implementation keep working, so an
  in-flight session or a cron clone survives the move.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = (
    Path(__file__).resolve().parents[1] / "skills" / "workflows" / "report-reproduction"
)
SCRIPT = SKILL_ROOT / "scripts" / "report_cli.py"

KERNEL_MODULE = "qwenpaw_data.host.core.agent.tools.compare"


def _load_cli_module() -> Any:
    """Import the skill script by path: it ships as a file, not as a package."""
    spec = importlib.util.spec_from_file_location("_report_cli_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


def test_pinned_https_requires_tls12_and_preserves_hostname(monkeypatch):
    from unittest.mock import Mock
    from rr import parse

    context = Mock()
    socket = Mock()
    monkeypatch.setattr(parse.socket, "create_connection", Mock(return_value=socket))
    monkeypatch.setattr(parse.ssl, "create_default_context", Mock(return_value=context))
    connection = parse._PinnedHTTPSConnection("example.com", "93.184.216.34", 443)
    connection.connect()
    assert context.minimum_version == parse.ssl.TLSVersion.TLSv1_2
    context.wrap_socket.assert_called_once_with(socket, server_hostname="example.com")

GOLD_REPORT = """# 2026年8月经营周报

## 一、总体概览

本月实现营业收入 1234.5 万元，同比增长 12.3%。活跃用户数为 8900 人。

## 二、分渠道拆解

线上渠道贡献 900.1 万元，线下渠道贡献 334.4 万元。
"""

FIRST_DRAFT = """## 一、总体概览

本月实现营业收入 1234.5 万元，同比增长 12.3%。活跃用户数为 8900 人。
"""

SECOND_DRAFT = """## 二、分渠道拆解

线上渠道贡献 900.1 万元，线下渠道贡献 334.4 万元。
"""

#: A first Section whose revenue number contradicts the gold report, so the
#: machine verdict fails and only a human override can approve it.
MISMATCHED_FIRST_DRAFT = (
    "## 一、总体概览\n\n本月实现营业收入 1300.0 万元，同比增长 12.3%。"
    "活跃用户数为 8900 人。"
)

OVERRIDE_REASON = "用户确认收入口径含一次性并表，1300.0 为准"

REVENUE_SQL = (
    "SELECT SUM(amount) AS revenue\n"
    "FROM orders\n"
    "WHERE dt BETWEEN ${start_date} AND ${end_date};\n"
)

#: The query result the baseline evidence gate expects under ``baseline/data/``:
#: numbers a recomputation produced, deliberately unlike ``source.md`` / draft so
#: it is not mistaken for a copy of the report.
REVENUE_CSV = (
    "period,revenue_wan,yoy_pct,active_users\n"
    "2026-08,1234.5,12.3,8900\n"
    "2026-08-online,900.1,,,\n"
    "2026-08-offline,334.4,,,\n"
)


# --- harness ------------------------------------------------------------------


class Workspace:
    """An agent workspace laid out the way the console lays it out."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "workspace"
        self.artifact_dir = self.root / "artifacts" / "sess-1"
        self.artifact_dir.mkdir(parents=True)

    @property
    def product_root(self) -> Path:
        return self.artifact_dir / cli.PRODUCT_DIR

    def write(self, name: str, text: str) -> Path:
        """Put an input file inside the workspace, where the CLI may read it."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def gold(self) -> Path:
        return self.write("gold.md", GOLD_REPORT)

    def manifest(self) -> dict[str, Any]:
        return json.loads(
            (self.product_root / cli.MANIFEST_FILE).read_text(encoding="utf-8")
        )

    def section_ids(self) -> list[str]:
        return [entry["section"]["section_id"] for entry in self.manifest()["sections"]]

    def status_of(self, section_id: str) -> str:
        for entry in self.manifest()["sections"]:
            if entry["section"]["section_id"] == section_id:
                return entry["status"]
        raise AssertionError(f"{section_id} not in manifest")

    def comparison_of(self, section_id: str) -> dict[str, Any]:
        path = (
            self.product_root
            / "sections"
            / section_id
            / "baseline"
            / (cli.COMPARISON_FILE)
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def add_query(self, section_id: str, name: str, sql: str) -> Path:
        directory = self.product_root / "sections" / section_id / "queries"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(sql, encoding="utf-8")
        return path

    def add_data(self, section_id: str, name: str, content: str) -> Path:
        directory = self.product_root / "sections" / section_id / "baseline" / "data"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def land_evidence(self, section_id: str) -> None:
        """Land the query template + data the evidence gate needs to pass.

        A real reproduction proves its numbers came from the warehouse, so the
        happy-path Sections have to carry both artifacts before compare-section.
        """
        self.add_query(section_id, "revenue.sql", REVENUE_SQL)
        self.add_data(section_id, "revenue.csv", REVENUE_CSV)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


@pytest.fixture
def run(workspace: Workspace, capsys: pytest.CaptureFixture[str]):
    """Invoke the CLI in process and return ``(exit_code, stdout_payload)``.

    Parsing stdout unconditionally is the assertion: a subcommand that prints
    two objects, a warning, or a traceback breaks every caller that reads it.
    """

    def _run(*argv: str, chat_id: str = "chat-1") -> tuple[int, dict[str, Any]]:
        code = cli.EXIT_OK
        try:
            returned = cli.main(
                [
                    "--artifact-dir",
                    str(workspace.artifact_dir),
                    "--chat-id",
                    chat_id,
                    *argv,
                ]
            )
            code = cli.EXIT_OK if returned is None else int(returned)
        except SystemExit as excitation:
            code = int(excitation.code or 0)
        captured = capsys.readouterr()
        return code, json.loads(captured.out)

    return _run


def parse(workspace: Workspace, run, *, chat_id: str = "chat-plan") -> list[str]:
    """Initialize a session and return its Section ids in document order."""
    workspace.gold()
    code, payload = run(
        "parse", "--file", str(workspace.root / "gold.md"), chat_id=chat_id
    )
    assert code == cli.EXIT_OK, payload
    return [entry["section_id"] for entry in payload["sections"]]


def approve_first_two(workspace: Workspace, run) -> list[str]:
    """Drive both Sections to ``approved`` the way the agent has to."""
    first, second = parse(workspace, run)
    for chat_id, section_id, draft in (
        ("chat-s1", first, FIRST_DRAFT),
        ("chat-s2", second, SECOND_DRAFT),
    ):
        code, payload = run(
            "update-section",
            "--section-id",
            section_id,
            "--content",
            draft,
            chat_id=chat_id,
        )
        assert code == cli.EXIT_OK, payload
        workspace.land_evidence(section_id)
        code, payload = run(
            "compare-section", "--section-id", section_id, chat_id=chat_id
        )
        assert code == cli.EXIT_OK and payload["passed"], payload
        code, payload = run(
            "apply-comparison", "--section-id", section_id, chat_id=chat_id
        )
        assert code == cli.EXIT_OK and payload["status"] == "approved", payload
    return [first, second]


# --- invocation surface -------------------------------------------------------


def test_help_is_available_without_any_artifact() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--artifact-dir" in done.stdout
    assert "--chat-id" in done.stdout
    for name in cli._SUBCOMMANDS:
        assert name in done.stdout


def test_every_subcommand_has_its_own_help(workspace: Workspace) -> None:
    for name in cli._SUBCOMMANDS:
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--artifact-dir",
                str(workspace.artifact_dir),
                "--chat-id",
                "c",
                name,
                "--help",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout.startswith("usage:"), name


def test_missing_chat_id_is_an_argument_error(workspace: Workspace) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact-dir",
            str(workspace.artifact_dir),
            "compile-report",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "--chat-id" in done.stderr


def test_unknown_subcommand_is_an_argument_error(workspace: Workspace) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact-dir",
            str(workspace.artifact_dir),
            "--chat-id",
            "c",
            "parse-report",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "invalid choice" in done.stderr


def test_a_failure_is_json_on_stdout_and_never_a_traceback(
    workspace: Workspace,
) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact-dir",
            str(workspace.artifact_dir),
            "--chat-id",
            "c",
            "status",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == cli.EXIT_ERROR
    assert "Traceback" not in done.stderr
    payload = json.loads(done.stdout)
    assert set(payload) == {"ok", "error"}
    assert payload["ok"] is False
    assert "尚未初始化" in payload["error"]


def test_loading_the_cli_does_not_import_the_comparison_kernel() -> None:
    script = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPT)!r});"
        "module = importlib.util.module_from_spec(spec);"
        "sys.modules['m'] = module;"
        "spec.loader.exec_module(module);"
        f"assert {KERNEL_MODULE!r} not in sys.modules"
    )
    subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )


# --- chat id ------------------------------------------------------------------


@pytest.mark.parametrize(
    "chat_id",
    ["a/b", "..", "chat id", "chat\x00id", "会话-1"],
)
def test_chat_id_outside_the_whitelist_is_rejected(run, chat_id: str) -> None:
    code, payload = run("status", chat_id=chat_id)
    assert code == cli.EXIT_ERROR
    assert "--chat-id" in payload["error"]


def test_an_overlong_chat_id_is_rejected(run) -> None:
    code, payload = run("status", chat_id="c" * (cli.CHAT_ID_MAX_LENGTH + 1))
    assert code == cli.EXIT_ERROR
    assert "--chat-id" in payload["error"]


def test_a_whitelisted_chat_id_becomes_the_turn_file(workspace: Workspace, run) -> None:
    parse(workspace, run, chat_id="chat-plan_1")
    turn = workspace.product_root / "turns" / "chat-plan_1.json"
    assert turn.is_file()
    assert json.loads(turn.read_text(encoding="utf-8"))["mode"] == "planning"


# --- parse --------------------------------------------------------------------


def test_parse_builds_the_v3_artifact_tree(workspace: Workspace, run) -> None:
    sections = parse(workspace, run)
    assert len(sections) == 2

    manifest = workspace.manifest()
    assert manifest["schema_version"] == cli.MANIFEST_SCHEMA_VERSION
    assert manifest["title"] == "2026年8月经营周报"
    assert manifest["source_path"] == "source/original.md"
    assert (workspace.product_root / "source" / "original.md").is_file()
    assert (workspace.product_root / "evidence" / "global.json").is_file()

    for section_id in sections:
        assert (
            workspace.product_root / "sections" / section_id / "source.md"
        ).is_file()
        entry = manifest["sections"][sections.index(section_id)]["section"]
        assert entry["comparison_path"] == (
            f"sections/{section_id}/baseline/{cli.COMPARISON_FILE}"
        )
        assert entry["draft_path"] == f"sections/{section_id}/baseline/draft.md"
        assert "validation_path" not in entry


def test_parse_records_the_source_period_and_the_next_runnable_section(
    workspace: Workspace, run
) -> None:
    workspace.gold()
    code, payload = run(
        "parse",
        "--file",
        str(workspace.root / "gold.md"),
        "--source-period",
        "202608",
        "--target-period",
        "202609",
    )
    assert code == cli.EXIT_OK
    assert payload["source_period"] == "202608"
    assert payload["target_period"] == "202609"
    assert payload["next_runnable_section"] == payload["sections"][0]["section_id"]


def test_parse_needs_exactly_one_source(workspace: Workspace, run) -> None:
    workspace.gold()
    code, payload = run("parse")
    assert code == cli.EXIT_ERROR
    assert "--file" in payload["error"]

    code, payload = run(
        "parse",
        "--file",
        str(workspace.root / "gold.md"),
        "--url",
        "https://example.com/a.md",
    )
    assert code == cli.EXIT_ERROR
    assert "--url" in payload["error"]


def test_parse_rejects_a_source_outside_the_workspace(
    workspace: Workspace, run, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(GOLD_REPORT, encoding="utf-8")
    code, payload = run("parse", "--file", str(outside))
    assert code == cli.EXIT_ERROR
    assert "工作区" in payload["error"]


def test_parse_rejects_an_unsupported_suffix(workspace: Workspace, run) -> None:
    source = workspace.write("gold.pdf", "not a report")
    code, payload = run("parse", "--file", str(source))
    assert code == cli.EXIT_ERROR
    assert ".pdf" in payload["error"]


def test_parse_is_idempotent_for_an_existing_session(workspace: Workspace, run) -> None:
    sections = parse(workspace, run)
    first_revision = workspace.manifest()["revision"]

    code, payload = run(
        "parse", "--file", str(workspace.root / "gold.md"), chat_id="chat-other"
    )
    assert code == cli.EXIT_OK
    assert payload["revision"] == first_revision
    assert [entry["section_id"] for entry in payload["sections"]] == sections


def test_force_reinitialize_rebuilds_the_product_root(
    workspace: Workspace, run
) -> None:
    parse(workspace, run)
    marker = workspace.product_root / "sections" / "leftover.txt"
    marker.write_text("stale", encoding="utf-8")

    code, payload = run(
        "parse",
        "--file",
        str(workspace.root / "gold.md"),
        "--force-reinitialize",
    )
    assert code == cli.EXIT_OK
    assert payload["revision"] == 1
    assert not marker.exists()


def test_a_document_without_headings_is_reported_not_crashed(
    workspace: Workspace, run
) -> None:
    source = workspace.write("flat.md", "只有一行正文，没有任何标题。\n")
    code, payload = run("parse", "--file", str(source))
    assert code == cli.EXIT_ERROR
    assert "章节" in payload["error"]


# --- parse: SSRF --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.md",
        "example.com/a.md",
        "http://127.0.0.1:8080/a.html",
        "http://localhost/a.html",
        "http://10.1.2.3/a.html",
        "http://192.168.1.1/a.html",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/a.html",
    ],
)
def test_parse_refuses_a_url_that_is_not_a_public_http_document(
    workspace: Workspace, run, url: str
) -> None:
    code, payload = run("parse", "--url", url)
    assert code == cli.EXIT_ERROR
    assert payload["error"]
    assert not (workspace.product_root / "source").exists()


# --- turn scope ---------------------------------------------------------------


def test_planning_chat_may_not_advance_a_section(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-plan",
    )
    assert code == cli.EXIT_ERROR
    assert "单独的 chat" in payload["error"]
    assert workspace.status_of(first) == "pending"


def test_one_chat_advances_exactly_one_section(workspace: Workspace, run) -> None:
    first, second = parse(workspace, run)
    code, _ = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK

    code, payload = run(
        "update-section",
        "--section-id",
        second,
        "--content",
        SECOND_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert first in payload["error"]

    # The same Section stays allowed, so a retry does not need a new chat.
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload


def test_a_section_chat_may_not_freeze_the_recipe(workspace: Workspace, run) -> None:
    approve_first_two(workspace, run)
    first, _ = workspace.section_ids()
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)

    code, payload = run("freeze-recipe", chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert first in payload["error"]

    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_OK, payload


def test_a_baseline_chat_may_not_start_a_target_run(workspace: Workspace, run) -> None:
    approve_first_two(workspace, run)
    first, _ = workspace.section_ids()
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)
    assert run("freeze-recipe", chat_id="chat-freeze")[0] == cli.EXIT_OK

    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert "单独的 chat" in payload["error"]


def test_one_chat_runs_exactly_one_target_period(workspace: Workspace, run) -> None:
    sections = _freeze_baseline(workspace, run)
    code, _ = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_OK

    code, payload = run("prepare-run", "--period", "202610", chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert "202609" in payload["error"]

    # A run chat is not a baseline chat either.
    code, payload = run(
        "update-section",
        "--section-id",
        sections[0],
        "--content",
        FIRST_DRAFT,
        chat_id="chat-run",
    )
    assert code == cli.EXIT_ERROR
    assert "目标周期" in payload["error"]


def _freeze_baseline(workspace: Workspace, run) -> list[str]:
    """Approved Sections plus a frozen recipe: the state a run starts from."""
    sections = approve_first_two(workspace, run)
    workspace.add_query(sections[0], "revenue.sql", REVENUE_SQL)
    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_OK, payload
    return sections


# --- update-section -----------------------------------------------------------


def test_update_section_writes_the_baseline_draft(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK
    assert payload["scope"] == "baseline"
    assert payload["status"] == "validating"
    assert payload["draft_path"] == (
        f"{cli.PRODUCT_DIR}/sections/{first}/baseline/draft.md"
    )
    draft = workspace.product_root / "sections" / first / "baseline" / "draft.md"
    assert draft.read_text(encoding="utf-8") == FIRST_DRAFT.strip() + "\n"
    assert workspace.status_of(first) == "validating"


def test_update_section_needs_exactly_one_content_source(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    for argv in (
        ["update-section", "--section-id", first],
        [
            "update-section",
            "--section-id",
            first,
            "--content",
            "x",
            "--content-file",
            str(workspace.gold()),
        ],
    ):
        code, payload = run(*argv, chat_id="chat-s1")
        assert code == cli.EXIT_ERROR
        assert "--content" in payload["error"]


def test_update_section_reads_a_content_file_from_the_workspace(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    draft_file = workspace.write("draft.md", FIRST_DRAFT)
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content-file",
        str(draft_file),
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["status"] == "validating"


def test_needs_input_is_a_valid_parking_state(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        "## 一、总体概览\n\n待确认口径。",
        "--status",
        "needs_input",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK
    assert payload["status"] == "needs_input"
    assert workspace.manifest()["sections"][0]["error"] is None


def test_an_unknown_section_lists_the_known_ones(workspace: Workspace, run) -> None:
    first, second = parse(workspace, run)
    code, payload = run(
        "update-section", "--section-id", "nope", "--content", "x", chat_id="chat-s1"
    )
    assert code == cli.EXIT_ERROR
    assert first in payload["error"] and second in payload["error"]


def test_a_section_id_that_would_escape_the_layout_is_rejected(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    for section_id in ("../evil", "a/b", "..", ""):
        code, payload = run(
            "update-section",
            "--section-id",
            section_id,
            "--content",
            "x",
            chat_id="chat-s1",
        )
        assert code == cli.EXIT_ERROR, section_id
        assert "section_id" in payload["error"]
        # The rejected identifier must not have been claimed: a chat bound to
        # something the store refuses is a chat that can never make progress.
        turn = workspace.product_root / "turns" / "chat-s1.json"
        assert not turn.exists() or "section_id" not in json.loads(
            turn.read_text(encoding="utf-8")
        )

    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload


def test_a_frozen_baseline_needs_an_explicit_reopen(workspace: Workspace, run) -> None:
    sections = _freeze_baseline(workspace, run)
    first = sections[0]
    assert workspace.manifest()["baseline_frozen_at"]

    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-reopen",
    )
    assert code == cli.EXIT_ERROR
    assert "--reopen-baseline" in payload["error"]

    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        "--reopen-baseline",
        chat_id="chat-reopen",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["baseline_frozen_at"] is None
    assert payload["status"] == "validating"


def test_update_section_writes_a_run_draft_without_touching_the_baseline(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline(workspace, run)
    first = sections[0]
    assert (
        run("prepare-run", "--period", "202609", chat_id="chat-run")[0] == cli.EXIT_OK
    )

    run_draft = "## 一、总体概览\n\n本月实现营业收入 1300.0 万元。\n"
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--run-period",
        "202609",
        "--content",
        run_draft,
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["scope"] == "run"
    assert payload["status"] == "checking"
    assert payload["draft_path"] == (
        f"{cli.PRODUCT_DIR}/sections/{first}/runs/202609/draft.md"
    )
    assert payload["run"]["baseline_revision"] == workspace.manifest()["revision"]

    baseline = workspace.product_root / "sections" / first / "baseline" / "draft.md"
    assert baseline.read_text(encoding="utf-8") == FIRST_DRAFT.strip() + "\n"
    assert workspace.status_of(first) == "approved"


def test_a_run_draft_needs_a_prepared_period(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--run-period",
        "202609",
        "--content",
        "x",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_ERROR
    assert "prepare-run" in payload["error"]


@pytest.mark.parametrize("period", ["../evil", "", "a" * 65, "2026/09", ".hidden"])
def test_a_period_that_would_escape_the_layout_is_rejected(
    workspace: Workspace, run, period: str
) -> None:
    _freeze_baseline(workspace, run)
    code, payload = run("prepare-run", "--period", period, chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert "--period" in payload["error"] or "--run-period" in payload["error"]


# --- compare-section ----------------------------------------------------------


def test_compare_section_computes_a_verdict_without_moving_the_state(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["passed"] is True
    assert payload["numeric_coverage"] == 1.0
    assert payload["status"] == "validating"
    assert payload["status_changed"] is False
    assert payload["comparison_path"] == (
        f"{cli.PRODUCT_DIR}/sections/{first}/baseline/{cli.COMPARISON_FILE}"
    )
    # The verdict landed, the state machine did not move.
    assert workspace.comparison_of(first)["passed"] is True
    assert workspace.status_of(first) == "validating"


def test_compare_section_reports_the_offending_span(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 9999.9 万元，同比增长 12.3%。"
        "活跃用户数为 8900 人。",
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["passed"] is False
    assert payload["numeric_difference_count"] == 1
    row = payload["numeric_differences"][0]
    assert (row["reference"], row["target"]) == ("1234.5", "9999.9")
    assert row["note"] is None

    stored = workspace.comparison_of(first)
    draft = (
        workspace.product_root / "sections" / first / "baseline" / "draft.md"
    ).read_text(encoding="utf-8")
    span = stored["numeric_differences"][0]
    assert draft[span["target_start"] : span["target_end"]] == "9999.9"
    # The kernel leaves every reviewer field for this skill to own.
    assert stored["review"] is None
    assert stored["passed_with_review"] is False


def test_compare_section_does_not_read_the_previous_verdict_back(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    # A hand-edited verdict is recomputed from the documents, not trusted.
    path = (
        workspace.product_root / "sections" / first / "baseline" / cli.COMPARISON_FILE
    )
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["passed"] = False
    forged["notes"] = "伪造"
    path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["passed"] is True
    assert payload["notes"] != "伪造"


def test_compare_section_honours_a_wider_tolerance(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 1240.0 万元，同比增长 12.3%。"
        "活跃用户数为 8900 人。",
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    # 1240.0 against 1234.5 is a 0.45% gap: outside the default 0.1%.
    assert (
        run("compare-section", "--section-id", first, chat_id="chat-s1")[1]["passed"]
        is False
    )
    code, payload = run(
        "compare-section",
        "--section-id",
        first,
        "--tolerance",
        "0.01",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK
    assert payload["passed"] is True
    assert payload["tolerance"] == 0.01


def test_compare_section_without_evidence_fails_the_gate(
    workspace: Workspace, run
) -> None:
    """A draft whose numbers merely match the report is not a reproduction.

    With no landed query template and no data it produced, the gate judges the
    numbers as copied from the original report and refuses the verdict, even
    though the numeric kernel found zero differences. This is the exact shape of
    a Section pasted from ``source.md``.
    """
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    # Zero numeric differences, yet the verdict is false: the gate, not the
    # kernel, decides provenance.
    assert payload["numeric_difference_count"] == 0
    assert payload["passed"] is False
    gate = payload["evidence_gate"]
    assert gate["passed"] is False
    assert gate["provenance"] == cli.PROVENANCE_REFERENCE
    assert gate["violations"]
    # The feedback tells the agent what to land and to reflect, never to override.
    assert "证据门禁" in payload["next"]
    assert "queries/*.sql" in payload["next"]
    # A comparison never moves the state machine.
    assert workspace.status_of(first) == "validating"


def test_evidence_that_copies_the_draft_is_refused(workspace: Workspace, run) -> None:
    """Padding ``baseline/data/`` with a copy of the draft proves nothing."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        MISMATCHED_FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)
    # The "data" is the draft verbatim. It differs from source.md, so the
    # violation names draft.md specifically rather than the source copy.
    workspace.add_data(first, "revenue.csv", MISMATCHED_FIRST_DRAFT)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    gate = payload["evidence_gate"]
    assert gate["passed"] is False
    assert payload["passed"] is False
    assert any("draft.md" in violation for violation in gate["violations"])


def test_evidence_that_copies_the_source_is_refused(workspace: Workspace, run) -> None:
    """A ``source.md`` copy landed as data is the report itself, not a result."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    source = (workspace.product_root / "sections" / first / "source.md").read_text(
        encoding="utf-8"
    )
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)
    workspace.add_data(first, "revenue.csv", source)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    gate = payload["evidence_gate"]
    assert gate["passed"] is False
    assert payload["passed"] is False
    assert any("source.md" in violation for violation in gate["violations"])


def test_a_draft_declaring_irreproducible_fails_the_gate(
    workspace: Workspace, run
) -> None:
    """Real evidence cannot launder a draft that admits it did not recompute."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT + "\n" + cli.IRREPRODUCIBLE_TAG + "口径缺失，数值沿用原报告。\n",
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    gate = payload["evidence_gate"]
    assert gate["passed"] is False
    assert payload["passed"] is False
    assert any(cli.IRREPRODUCIBLE_TAG in violation for violation in gate["violations"])


def test_a_human_override_cannot_launder_a_failed_gate(
    workspace: Workspace, run
) -> None:
    """Copied numbers cannot be waved through by a self-filled override.

    This is the whole point of the gate: ``section 3`` of a real session was
    approved over a verbatim copy with no query behind it. With the gate failing,
    ``apply-comparison --approved-by-user`` must be refused outright.
    """
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--approved-by-user",
        "--reason",
        OVERRIDE_REASON,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "证据门禁" in payload["error"]
    assert "不能人工放行" in payload["error"]
    # The refusal happens before any state move: the Section is still validating.
    assert workspace.status_of(first) == "validating"


def test_applying_a_failed_gate_without_override_fails_the_section(
    workspace: Workspace, run
) -> None:
    """Without an override the gate failure simply fails the Section."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["status"] == "failed"
    assert payload["passed"] is False
    assert "证据门禁" in payload["next"]
    stored = workspace.comparison_of(first)
    assert stored["evidence_gate"]["passed"] is False
    assert stored["review"] is None


def test_real_evidence_passes_the_gate_and_records_provenance(
    workspace: Workspace, run
) -> None:
    """A query template plus the data it produced is judged a recomputation."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    gate = payload["evidence_gate"]
    assert gate["passed"] is True
    assert gate["provenance"] == cli.PROVENANCE_RECOMPUTED
    assert gate["violations"] == []
    assert payload["passed"] is True

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["status"] == "approved"
    assert workspace.comparison_of(first)["evidence_gate"]["provenance"] == (
        cli.PROVENANCE_RECOMPUTED
    )


@pytest.mark.parametrize("tolerance", ["-0.1", "1.5"])
def test_a_tolerance_outside_zero_to_one_is_rejected(
    workspace: Workspace, run, tolerance: str
) -> None:
    first, _ = parse(workspace, run)
    code, payload = run(
        "compare-section",
        "--section-id",
        first,
        "--tolerance",
        tolerance,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--tolerance" in payload["error"]


def test_a_non_numeric_tolerance_is_an_argument_error(workspace: Workspace) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact-dir",
            str(workspace.artifact_dir),
            "--chat-id",
            "c",
            "compare-section",
            "--section-id",
            "s",
            "--tolerance",
            "abc",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "--tolerance" in done.stderr


def test_compare_section_needs_the_host_kernel(
    workspace: Workspace, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    monkeypatch.setattr(cli, "_KERNEL", None)
    monkeypatch.setitem(sys.modules, KERNEL_MODULE, None)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert KERNEL_MODULE in payload["error"]


# --- apply-comparison ---------------------------------------------------------


def _validated_first_section(workspace: Workspace, run) -> str:
    """A first Section sitting in ``validating`` with its verdict computed."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    run("compare-section", "--section-id", first, chat_id="chat-s1")
    return first


def test_apply_comparison_is_the_only_thing_that_moves_the_state(
    workspace: Workspace, run
) -> None:
    first = _validated_first_section(workspace, run)
    assert workspace.status_of(first) == "validating"

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["status"] == "approved"
    assert payload["passed"] is True
    assert payload["approved_by_user"] is False
    assert workspace.status_of(first) == "approved"
    assert workspace.manifest()["sections"][0]["section"]["comparison_path"] == (
        f"sections/{first}/baseline/{cli.COMPARISON_FILE}"
    )


def test_a_remaining_difference_fails_the_section(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 9999.9 万元，同比增长 12.3%。"
        "活跃用户数为 8900 人。",
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK
    assert payload["status"] == "failed"
    assert payload["passed"] is False
    assert "修订" in payload["next"]
    assert workspace.status_of(first) == "failed"


def test_a_human_override_needs_the_users_own_words(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        MISMATCHED_FIRST_DRAFT,
        chat_id="chat-s1",
    )
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--approved-by-user",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--reason" in payload["error"]
    assert workspace.status_of(first) == "validating"

    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--reason",
        "无理由",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--approved-by-user" in payload["error"]

    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--approved-by-user",
        "--reason",
        "   ",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--reason" in payload["error"]


def test_a_human_override_is_disclosed_everywhere_it_is_read(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        MISMATCHED_FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    run("compare-section", "--section-id", first, chat_id="chat-s1")

    reason = OVERRIDE_REASON
    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--approved-by-user",
        "--reason",
        reason,
        "--notes",
        "已电话确认",
        "--criteria",
        '[{"criterion":"GMV 口径","met":true,"evidence":"取自 dws_gmv 日汇总"}]',
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK
    assert payload["status"] == "approved"
    assert payload["passed"] is True
    assert payload["approved_by_user"] is True

    stored = workspace.comparison_of(first)
    # The gap stays visible: an override records the acceptance, it never
    # rewrites the numbers that were actually compared.
    assert stored["passed_with_review"] is True
    assert stored["numeric_differences"][0]["matched"] is False
    assert reason in stored["numeric_differences"][0]["note"]
    assert stored["notes"].startswith("已电话确认\n")
    assert reason in stored["notes"]
    assert stored["criteria_analysis"] == [
        {"criterion": "GMV 口径", "met": True, "evidence": "取自 dws_gmv 日汇总"}
    ]
    assert stored["review"] == {
        "approved_by_user": True,
        "reason": reason,
        "reviewed_at": stored["review"]["reviewed_at"],
    }
    assert payload["review"]["approved_by_user"] is True


def test_a_machine_pass_carries_no_review_record(workspace: Workspace, run) -> None:
    first = _validated_first_section(workspace, run)
    run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    stored = workspace.comparison_of(first)
    assert stored["review"] is None
    assert stored["passed_with_review"] is False


def _overridden_first_section(workspace: Workspace, run) -> str:
    """A first Section approved by a human, not by its numbers."""
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        MISMATCHED_FIRST_DRAFT,
        chat_id="chat-s1",
    )
    workspace.land_evidence(first)
    run("compare-section", "--section-id", first, chat_id="chat-s1")
    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--approved-by-user",
        "--reason",
        OVERRIDE_REASON,
        "--notes",
        "已电话确认",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload
    assert workspace.status_of(first) == "approved"
    return first


def test_recomputing_over_a_human_override_is_refused(
    workspace: Workspace, run
) -> None:
    """An override has to outlive a re-comparison of the draft it accepted.

    ``compare-section`` rewrites ``comparison.json`` wholesale and the reviewer
    fields live in that same file, so without the refusal an approved Section
    would keep its state while losing every record of who accepted which gap.
    """
    first = _overridden_first_section(workspace, run)
    before = workspace.comparison_of(first)

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert "人工放行" in payload["error"]
    assert "update-section" in payload["error"]
    # Nothing was rewritten and the Section did not slip out of approved: the
    # refusal costs the agent a retry, never a disclosure.
    assert workspace.comparison_of(first) == before
    assert workspace.status_of(first) == "approved"


def test_revising_the_draft_lifts_the_recompute_refusal(
    workspace: Workspace, run
) -> None:
    """A new draft is a new decision, so the old acceptance stops blocking."""
    first = _overridden_first_section(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        "--reopen-baseline",
        chat_id="chat-s1",
    )

    code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_OK, payload
    assert payload["passed"] is True
    stored = workspace.comparison_of(first)
    # The override described the previous draft, so it is gone rather than
    # silently carried over onto numbers nobody accepted.
    assert stored["review"] is None
    assert stored["passed_with_review"] is False
    assert workspace.status_of(first) == "validating"


def test_a_machine_verdict_can_be_recomputed_freely(workspace: Workspace, run) -> None:
    """The refusal guards a disclosure, not the ordinary recompute loop."""
    first = _validated_first_section(workspace, run)
    run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert workspace.status_of(first) == "approved"

    for _ in range(2):
        code, payload = run("compare-section", "--section-id", first, chat_id="chat-s1")
        assert code == cli.EXIT_OK, payload
        assert payload["status_changed"] is False

    assert workspace.comparison_of(first)["review"] is None
    assert workspace.status_of(first) == "approved"


def test_apply_comparison_refuses_another_sections_verdict(
    workspace: Workspace, run
) -> None:
    first, second = parse(workspace, run)
    for chat_id, section_id, draft in (
        ("chat-s1", first, FIRST_DRAFT),
        ("chat-s2", second, "## 二、分渠道拆解\n\n线上渠道贡献 1.0 万元。\n"),
    ):
        run(
            "update-section",
            "--section-id",
            section_id,
            "--content",
            draft,
            chat_id=chat_id,
        )
        run("compare-section", "--section-id", section_id, chat_id=chat_id)

    # Swap the two verdicts: the paths recorded inside each file no longer match
    # the Section that ends up holding it.
    left = (
        workspace.product_root / "sections" / first / "baseline" / cli.COMPARISON_FILE
    )
    right = (
        workspace.product_root / "sections" / second / "baseline" / cli.COMPARISON_FILE
    )
    left_bytes, right_bytes = left.read_bytes(), right.read_bytes()
    left.write_bytes(right_bytes)
    right.write_bytes(left_bytes)

    for chat_id, section_id in (("chat-s1", first), ("chat-s2", second)):
        code, payload = run(
            "apply-comparison", "--section-id", section_id, chat_id=chat_id
        )
        assert code == cli.EXIT_ERROR
        assert "不是本 Section 的比对结果" in payload["error"]
        assert workspace.status_of(section_id) == "validating"


def test_apply_comparison_refuses_a_verdict_older_than_the_draft(
    workspace: Workspace, run
) -> None:
    first = _validated_first_section(workspace, run)
    draft = workspace.product_root / "sections" / first / "baseline" / "draft.md"
    draft.write_text(FIRST_DRAFT.replace("1234.5", "9999.9"), encoding="utf-8")

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert "在比对之后被修改过" in payload["error"]
    assert workspace.status_of(first) == "validating"


def test_apply_comparison_refuses_a_verdict_older_than_the_source(
    workspace: Workspace, run
) -> None:
    first = _validated_first_section(workspace, run)
    source = workspace.product_root / "sections" / first / "source.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n补充。\n", encoding="utf-8"
    )

    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert "在比对之后被修改过" in payload["error"]


def test_apply_comparison_needs_a_verdict_to_read(workspace: Workspace, run) -> None:
    first, _ = parse(workspace, run)
    run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    code, payload = run("apply-comparison", "--section-id", first, chat_id="chat-s1")
    assert code == cli.EXIT_ERROR
    assert "compare-section" in payload["error"]


def test_criteria_must_be_a_list_of_objects(workspace: Workspace, run) -> None:
    first = _validated_first_section(workspace, run)
    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--criteria",
        "[1,2]",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--criteria" in payload["error"]

    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--criteria",
        "not json",
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_ERROR
    assert "--criteria" in payload["error"]


def test_criteria_may_come_from_a_file(workspace: Workspace, run) -> None:
    first = _validated_first_section(workspace, run)
    path = workspace.write(
        "criteria.json", '[{"criterion":"口径","met":true,"evidence":"dws"}]'
    )
    code, payload = run(
        "apply-comparison",
        "--section-id",
        first,
        "--criteria",
        str(path),
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload
    assert workspace.comparison_of(first)["criteria_analysis"] == [
        {"criterion": "口径", "met": True, "evidence": "dws"}
    ]


# --- freeze-recipe ------------------------------------------------------------


def test_freeze_recipe_extracts_the_parameter_templates(
    workspace: Workspace, run
) -> None:
    first, second = _freeze_baseline_with(workspace, run, second_query=True)

    recipe = json.loads(
        (workspace.product_root / "recipe.json").read_text(encoding="utf-8")
    )
    assert recipe["report_id"] == workspace.manifest()["report_id"]
    assert [item["name"] for item in recipe["parameters"]] == [
        "channel_month",
        "end_date",
        "start_date",
    ]
    assert {query["section_id"] for query in recipe["queries"]} == {first, second}
    assert recipe["section_order"] == [first, second]
    assert workspace.manifest()["baseline_frozen_at"]


def _freeze_baseline_with(workspace: Workspace, run, *, second_query: bool) -> list:
    sections = approve_first_two(workspace, run)
    workspace.add_query(sections[0], "revenue.sql", REVENUE_SQL)
    if second_query:
        workspace.add_query(
            sections[1],
            "channel.sql",
            "SELECT channel, SUM(amount) FROM orders\n"
            "WHERE month = {{ channel_month }} GROUP BY channel;\n",
        )
    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_OK, payload
    return sections


def test_freeze_recipe_refuses_an_empty_recipe(workspace: Workspace, run) -> None:
    sections = approve_first_two(workspace, run)
    # The evidence gate now guarantees an approved Section carries a real .sql,
    # so an empty recipe only survives as a defence against a tampered or legacy
    # workspace: strip the landed queries and freeze-recipe must still refuse.
    for section_id in sections:
        for sql in (workspace.product_root / "sections" / section_id / "queries").glob(
            "*.sql"
        ):
            sql.unlink()

    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_ERROR
    assert ".sql" in payload["error"]
    assert not (workspace.product_root / "recipe.json").exists()

    code, payload = run("freeze-recipe", "--allow-empty-queries", chat_id="chat-freeze")
    assert code == cli.EXIT_OK, payload
    assert payload["query_count"] == 0


def test_freeze_recipe_refuses_an_unapproved_baseline(
    workspace: Workspace, run
) -> None:
    first, second = parse(workspace, run)
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)
    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_ERROR
    assert first in payload["error"] and second in payload["error"]


def test_freeze_recipe_refuses_a_query_that_leaves_the_product_root(
    workspace: Workspace, run
) -> None:
    sections = approve_first_two(workspace, run)
    outside = workspace.root.parent / "outside.sql"
    outside.write_text("SELECT 1;", encoding="utf-8")
    link = workspace.product_root / "sections" / sections[0] / "queries" / "evil.sql"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    code, payload = run("freeze-recipe", chat_id="chat-freeze")
    assert code == cli.EXIT_ERROR
    assert "越出" in payload["error"]
    assert not (workspace.product_root / "recipe.json").exists()


def test_freezing_twice_keeps_one_recipe(workspace: Workspace, run) -> None:
    _freeze_baseline_with(workspace, run, second_query=False)
    first = json.loads(
        (workspace.product_root / "recipe.json").read_text(encoding="utf-8")
    )
    code, payload = run("freeze-recipe", chat_id="chat-freeze2")
    assert code == cli.EXIT_OK, payload
    assert (
        json.loads((workspace.product_root / "recipe.json").read_text(encoding="utf-8"))
        == first
    )


# --- prepare-run --------------------------------------------------------------


def test_prepare_run_builds_a_period_state_that_points_at_the_baseline(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_OK
    assert payload["period"] == "202609"
    assert payload["run_manifest"] == (
        f"{cli.PRODUCT_DIR}/runs/202609/run_manifest.json"
    )
    assert payload["baseline_revision"] == workspace.manifest()["revision"]
    assert [entry["section_id"] for entry in payload["sections"]] == sections
    assert all(entry["status"] == "pending" for entry in payload["sections"])

    for section_id in sections:
        assert (
            workspace.product_root
            / "sections"
            / section_id
            / "runs"
            / "202609"
            / "data"
        ).is_dir()


def test_prepare_run_needs_a_frozen_recipe(workspace: Workspace, run) -> None:
    approve_first_two(workspace, run)
    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert "freeze-recipe" in payload["error"]


def test_prepare_run_is_idempotent_for_the_same_period(
    workspace: Workspace, run
) -> None:
    _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    revision = json.loads(
        (workspace.product_root / "runs" / "202609" / "run_manifest.json").read_text(
            encoding="utf-8"
        )
    )["revision"]

    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_OK
    assert payload["revision"] == revision


def test_a_run_is_rejected_once_the_baseline_moves(workspace: Workspace, run) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")

    run(
        "update-section",
        "--section-id",
        sections[0],
        "--content",
        FIRST_DRAFT,
        "--reopen-baseline",
        chat_id="chat-reopen",
    )
    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_ERROR
    assert "基线发生了变化" in payload["error"]

    # While the reopened Section is unapproved there is no run to rebase at all.
    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert sections[0] in payload["error"]

    # Approving it again restores the baseline, but the run was prepared against
    # a revision that no longer exists, so it stays refused until the caller
    # says out loud that the period state may be thrown away.
    run("compare-section", "--section-id", sections[0], chat_id="chat-reopen")
    code, payload = run(
        "apply-comparison", "--section-id", sections[0], chat_id="chat-reopen"
    )
    assert code == cli.EXIT_OK, payload

    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert "--force-reinitialize" in payload["error"]

    code, payload = run(
        "prepare-run",
        "--period",
        "202609",
        "--force-reinitialize",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["baseline_revision"] == workspace.manifest()["revision"]


def test_force_reinitialize_rebuilds_only_the_period_state(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    baseline_draft = (
        workspace.product_root / "sections" / sections[0] / "baseline" / "draft.md"
    )

    code, payload = run(
        "prepare-run",
        "--period",
        "202609",
        "--force-reinitialize",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["revision"] == 1
    assert payload["baseline_revision"] == workspace.manifest()["revision"]
    assert baseline_draft.read_text(encoding="utf-8") == FIRST_DRAFT.strip() + "\n"


def test_a_period_run_needs_every_section_approved(workspace: Workspace, run) -> None:
    first, second = parse(workspace, run)
    workspace.add_query(first, "revenue.sql", REVENUE_SQL)
    run("freeze-recipe", "--allow-empty-queries", chat_id="chat-freeze")
    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_ERROR
    assert first in payload["error"] and second in payload["error"]


# --- check-run ----------------------------------------------------------------


def test_check_run_reports_the_four_standing_checks(workspace: Workspace, run) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    run(
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 1300.0 万元。\n",
        chat_id="chat-run",
    )

    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert [check["name"] for check in payload["checks"]] == [
        "draft_present",
        "data_evidence_present",
        "query_recipe_present",
        "no_unresolved_parameters",
    ]
    assert payload["passed"] is False
    assert payload["status"] == "running"
    failed = {check["name"] for check in payload["checks"] if not check["passed"]}
    assert failed == {"data_evidence_present"}
    # A period the gold report never covered gets no GT comparison, so the note
    # only ever talks about the standing checks.
    assert payload["notes"] == "跨周期运行仍有数据、口径或完整性检查未通过。"


def test_check_run_passes_once_the_evidence_lands(workspace: Workspace, run) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    run(
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 1300.0 万元。\n",
        chat_id="chat-run",
    )
    evidence = (
        workspace.product_root
        / "sections"
        / sections[0]
        / "runs"
        / "202609"
        / "data"
        / "revenue.json"
    )
    evidence.write_text('{"revenue": 1300.0}\n', encoding="utf-8")

    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["passed"] is True
    assert payload["status"] == "approved"
    assert payload["run_checks_path"] == (
        f"{cli.PRODUCT_DIR}/sections/{sections[0]}/runs/202609/run_checks.json"
    )
    stored = json.loads(
        (
            workspace.product_root
            / "sections"
            / sections[0]
            / "runs"
            / "202609"
            / "run_checks.json"
        ).read_text(encoding="utf-8")
    )
    assert stored["passed"] is True
    assert stored["period"] == "202609"


def test_evidence_requirements_can_be_waived_explicitly(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    run(
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 1300.0 万元。\n",
        chat_id="chat-run",
    )
    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        "--skip-data-evidence",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["passed"] is True


def test_business_checks_are_appended_and_validated(workspace: Workspace, run) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    run(
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "## 一、总体概览\n\n本月实现营业收入 1300.0 万元。\n",
        chat_id="chat-run",
    )
    evidence = (
        workspace.product_root
        / "sections"
        / sections[0]
        / "runs"
        / "202609"
        / "data"
        / "revenue.json"
    )
    evidence.write_text('{"revenue": 1300.0}\n', encoding="utf-8")

    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        "--business-check",
        '{"name":"口径一致","passed":true,"details":"与基线一致"}',
        "--business-check",
        '{"name":"退款剔除","passed":false,"details":"缺少 is_refund_order 条件"}',
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["passed"] is False
    assert [check["name"] for check in payload["checks"]][-2:] == [
        "口径一致",
        "退款剔除",
    ]

    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        "--business-check",
        '{"name":"缺少 passed"}',
        chat_id="chat-run",
    )
    assert code == cli.EXIT_ERROR
    assert "--business-check" in payload["error"]


def test_an_unresolved_placeholder_fails_the_run_section(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    run(
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "## 一、总体概览\n\n统计区间 ${start_date} ~ ${end_date}。\n",
        chat_id="chat-run",
    )
    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        "--skip-data-evidence",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_OK
    assert payload["passed"] is False
    unresolved = next(
        check
        for check in payload["checks"]
        if check["name"] == "no_unresolved_parameters"
    )
    assert "${start_date}" in unresolved["details"]


def test_check_run_needs_a_prepared_period(workspace: Workspace, run) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    code, payload = run(
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
        chat_id="chat-run",
    )
    assert code == cli.EXIT_ERROR
    assert "prepare-run" in payload["error"]


# --- removed commands ---------------------------------------------------------


def test_removed_commands_fail_in_chinese(workspace: Workspace, run) -> None:
    code, payload = run("compile-report", chat_id="chat-final")
    assert code == cli.EXIT_ERROR
    assert "已移除" in payload["error"]
    code, payload = run("schedule-descriptor", chat_id="chat-final")
    assert code == cli.EXIT_ERROR
    assert "已移除" in payload["error"]
    assert set(payload) == {"ok", "error"}


# --- schedule-descriptor ------------------------------------------------------


def test_freeze_recipe_writes_schedule_descriptor_inline(
    workspace: Workspace, run
) -> None:
    """freeze-recipe 冻结时顺带写出 schedule_descriptor.json，正常无需再
    单独跑 schedule-descriptor；stdout 也直接暴露预填字段。"""
    _freeze_baseline_with(workspace, run, second_query=True)
    descriptor_file = workspace.product_root / "schedule_descriptor.json"
    assert descriptor_file.is_file()
    recipe_bytes = (workspace.product_root / "recipe.json").read_bytes()
    stored = json.loads(descriptor_file.read_text(encoding="utf-8"))
    assert stored["recipe_hash"] == hashlib.sha256(recipe_bytes).hexdigest()
    assert stored["parameters"] == {
        "start_date": "${window_start:%Y%m%d}",
        "end_date": "${period_end:%Y%m%d}",
        "channel_month": "${period:%Y%m}",
    }
    code, payload = run("freeze-recipe", chat_id="chat-refreeze")
    assert code == cli.EXIT_OK, payload
    assert payload["descriptor_path"] == (f"{cli.PRODUCT_DIR}/schedule_descriptor.json")
    assert payload["recipe_hash"] == stored["recipe_hash"]
    assert payload["period"] == stored["period"]
    assert payload["schedule_parameters"] == stored["parameters"]


def test_schedule_descriptor_maps_week_slots_by_position(
    workspace: Workspace, run
) -> None:
    templates = cli._infer_parameter_templates(
        [
            {"name": "w2_start"},
            {"name": "w2_end"},
            {"name": "w1_start"},
            {"name": "w1_end"},
            {"name": "biz_date"},
        ]
    )
    assert templates == {
        "w1_start": "${window_week_start:0:%Y%m%d}",
        "w1_end": "${window_week_end:0:%Y%m%d}",
        "w2_start": "${window_week_start:1:%Y%m%d}",
        "w2_end": "${window_week_end:1:%Y%m%d}",
        "biz_date": "${period:%Y%m%d}",
    }


def test_schedule_descriptor_maps_month_slots_by_position() -> None:
    templates = cli._infer_parameter_templates(
        [
            {"name": "ds_month"},
            {"name": "ds_month_1"},
            {"name": "ds_month_2"},
            {"name": "ds_month_3"},
            {"name": "ds_month_4"},
            {"name": "attr_ds"},
        ]
    )
    assert templates == {
        "ds_month": "${period:%Y%m}",
        "ds_month_1": "${window_month:0:%Y%m}",
        "ds_month_2": "${window_month:1:%Y%m}",
        "ds_month_3": "${window_month:2:%Y%m}",
        "ds_month_4": "${window_month:3:%Y%m}",
        "attr_ds": "${period:%Y%m%d}",
    }


def test_infer_period_spec_detects_week_month_and_default_shapes() -> None:
    assert cli._infer_period_spec([{"name": "w1_start"}, {"name": "w2_end"}]) == {
        "unit": "week",
        "offset": -1,
        "window_size": 2,
        "label_format": "%G-W%V",
    }
    assert cli._infer_period_spec(
        [{"name": f"ds_month_{index}"} for index in range(1, 5)]
    ) == {
        "unit": "month",
        "offset": -1,
        "window_size": 4,
        "label_format": "%Y%m",
    }
    assert cli._infer_period_spec([{"name": "channel_month"}]) == {
        "unit": "month",
        "offset": -1,
        "window_size": 1,
        "label_format": "%Y%m",
    }


# --- migrating what the host implementation left behind -----------------------


LEGACY_SECTION_ID = "r__01_a"

LEGACY_VALIDATION = {
    "section_id": LEGACY_SECTION_ID,
    "passed": False,
    "passed_with_review": True,
    "numeric_coverage": 0.5,
    "numeric_differences": [
        {
            "source": "100.0",
            "draft": "200.0",
            "relative_error": 1.0,
            "matched": False,
            "note": None,
            "draft_start": 12,
            "draft_end": 17,
        }
    ],
    "structure_differences": [],
    "criteria_analysis": [],
    "notes": "旧版备注",
    "tolerance": 0.001,
    "created_at": "2026-08-01T00:00:00+00:00",
}


def _legacy_manifest(*, schema_version: int, verdict_name: str, baseline: bool) -> dict:
    """A manifest as the host implementation wrote it, at either schema level."""
    prefix = (
        f"sections/{LEGACY_SECTION_ID}/baseline/"
        if baseline
        else (f"sections/{LEGACY_SECTION_ID}/")
    )
    return {
        "schema_version": schema_version,
        "revision": 7,
        "report_id": "r",
        "title": "r",
        "source_path": "source/original.md",
        "source_period": None,
        "target_period": None,
        "baseline_frozen_at": None,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "sections": [
            {
                "section": {
                    "section_id": LEGACY_SECTION_ID,
                    "title": "a",
                    "level": 2,
                    "source_path": f"sections/{LEGACY_SECTION_ID}/source.md",
                    "draft_path": f"{prefix}draft.md",
                    "spec_path": f"sections/{LEGACY_SECTION_ID}/spec.json",
                    "validation_path": f"{prefix}{verdict_name}",
                    "blocked_by": [],
                },
                "status": "approved",
                "attempt": 1,
                "updated_at": "2026-08-01T00:00:00+00:00",
                "error": None,
            }
        ],
    }


def _build_legacy_session(
    workspace: Workspace, *, schema_version: int, with_run: bool = True
) -> None:
    """Rebuild a session exactly as the previous host implementation left it."""
    baseline = schema_version >= 2
    prefix = "baseline/" if baseline else ""
    verdict_name = "validation.json"

    section_root = workspace.product_root / "sections" / LEGACY_SECTION_ID
    (section_root / prefix).mkdir(parents=True)
    (workspace.product_root / "source").mkdir(parents=True, exist_ok=True)
    (workspace.product_root / "source" / "original.md").write_text(
        "# r\n", encoding="utf-8"
    )
    (section_root / "source.md").write_text(
        "## a\n\n收入 100.0 万元。\n", encoding="utf-8"
    )
    (section_root / prefix / "draft.md").write_text(
        "## a\n\n收入 200.0 万元。\n", encoding="utf-8"
    )
    (section_root / prefix / verdict_name).write_text(
        json.dumps(LEGACY_VALIDATION, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (workspace.product_root / cli.MANIFEST_FILE).write_text(
        json.dumps(
            _legacy_manifest(
                schema_version=schema_version,
                verdict_name=verdict_name,
                baseline=baseline,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if with_run:
        run_root = workspace.product_root / "runs" / "202609"
        run_root.mkdir(parents=True)
        (run_root / "run_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "revision": 4,
                    "report_id": "r",
                    "period": "202609",
                    # Deliberately behind: migrating bumps the baseline revision
                    # and must not invalidate a run that was already prepared.
                    "baseline_revision": 3,
                    "recipe_hash": "abc",
                    "created_at": "2026-08-02T00:00:00+00:00",
                    "updated_at": "2026-08-02T00:00:00+00:00",
                    "sections": [
                        {
                            "section_id": LEGACY_SECTION_ID,
                            "status": "pending",
                            "attempt": 0,
                            "updated_at": "2026-08-02T00:00:00+00:00",
                            "error": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize("schema_version", [1, 2])
def test_a_legacy_session_is_migrated_on_first_load(
    workspace: Workspace, run, schema_version: int
) -> None:
    _build_legacy_session(workspace, schema_version=schema_version)
    code, payload = run("status", chat_id="chat-migrate")
    assert code == cli.EXIT_OK, payload

    manifest = workspace.manifest()
    assert manifest["schema_version"] == cli.MANIFEST_SCHEMA_VERSION
    entry = manifest["sections"][0]["section"]
    assert "validation_path" not in entry
    assert entry["comparison_path"] == (
        f"sections/{LEGACY_SECTION_ID}/baseline/{cli.COMPARISON_FILE}"
    )
    assert entry["draft_path"] == f"sections/{LEGACY_SECTION_ID}/baseline/draft.md"

    baseline = workspace.product_root / "sections" / LEGACY_SECTION_ID / "baseline"
    assert (baseline / "draft.md").is_file()
    assert (baseline / cli.COMPARISON_FILE).is_file()
    assert (baseline / "validation.json.bak").is_file()
    assert not (baseline / "validation.json").exists()
    # The v1 layout kept both directly under the Section.
    if schema_version == 1:
        section_root = workspace.product_root / "sections" / LEGACY_SECTION_ID
        assert not (section_root / "draft.md").exists()
        assert not (section_root / "validation.json").exists()


def test_migration_keeps_the_verdict_and_names_the_reviewer(
    workspace: Workspace, run
) -> None:
    _build_legacy_session(workspace, schema_version=2)
    assert run("status", chat_id="chat-migrate")[0] == cli.EXIT_OK

    stored = workspace.comparison_of(LEGACY_SECTION_ID)
    assert stored["passed_with_review"] is True
    assert stored["numeric_coverage"] == 0.5
    assert stored["notes"] == "旧版备注"
    assert stored["created_at"] == "2026-08-01T00:00:00+00:00"
    assert stored["target_path"] == (f"sections/{LEGACY_SECTION_ID}/baseline/draft.md")
    assert stored["reference_path"] == f"sections/{LEGACY_SECTION_ID}/source.md"

    # The renames are field renames, never a re-reading of the numbers.
    difference = stored["numeric_differences"][0]
    assert difference["reference"] == "100.0"
    assert difference["target"] == "200.0"
    assert difference["target_start"] == 12
    assert difference["target_end"] == 17
    assert "section_id" not in difference

    # An approval that predates the review record still has to be attributable.
    assert stored["review"]["approved_by_user"] is True
    assert stored["review"]["reason"]
    assert stored["review"]["reviewed_at"]

    # And it still counts as approved, so the run stays usable.
    assert workspace.status_of(LEGACY_SECTION_ID) == "approved"


def test_migration_rebases_a_run_that_was_already_prepared(
    workspace: Workspace, run
) -> None:
    """A run prepared before the migration must still be usable after it."""
    _build_legacy_session(workspace, schema_version=2)
    assert run("status", chat_id="chat-migrate")[0] == cli.EXIT_OK

    revision = workspace.manifest()["revision"]
    assert revision > 7
    run_manifest_path = workspace.product_root / "runs" / "202609" / "run_manifest.json"
    stored = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    assert stored["baseline_revision"] == revision

    # ``prepare-run`` is what would notice a run left behind by a migration: it
    # refuses anything whose baseline_revision trails the manifest. Freezing the
    # recipe properly would bump the revision again, so the frozen artifact is
    # written by hand and the run is pointed at it.
    workspace.add_query(LEGACY_SECTION_ID, "q.sql", REVENUE_SQL)
    recipe = workspace.product_root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "report_id": "r",
                "parameters": [],
                "queries": [
                    {
                        "section_id": LEGACY_SECTION_ID,
                        "query_path": f"sections/{LEGACY_SECTION_ID}/queries/q.sql",
                        "parameters": ["start_date", "end_date"],
                    }
                ],
                "section_order": [LEGACY_SECTION_ID],
                "output_path": "report.html",
            }
        ),
        encoding="utf-8",
    )
    stored["recipe_hash"] = hashlib.sha256(recipe.read_bytes()).hexdigest()
    run_manifest_path.write_text(json.dumps(stored), encoding="utf-8")

    code, payload = run("prepare-run", "--period", "202609", chat_id="chat-run")
    assert code == cli.EXIT_OK, payload
    assert payload["baseline_revision"] == revision


def test_migration_happens_once(workspace: Workspace, run) -> None:
    _build_legacy_session(workspace, schema_version=2)
    run("status", chat_id="chat-migrate")
    revision = workspace.manifest()["revision"]
    backup = (
        workspace.product_root
        / "sections"
        / LEGACY_SECTION_ID
        / "baseline"
        / "validation.json.bak"
    ).read_bytes()

    code, payload = run("status", chat_id="chat-migrate2")
    assert code == cli.EXIT_OK
    assert workspace.manifest()["revision"] == revision
    assert (
        workspace.product_root
        / "sections"
        / LEGACY_SECTION_ID
        / "baseline"
        / "validation.json.bak"
    ).read_bytes() == backup


def test_a_corrupt_legacy_verdict_aborts_the_migration(
    workspace: Workspace, run
) -> None:
    _build_legacy_session(workspace, schema_version=2)
    (
        workspace.product_root
        / "sections"
        / LEGACY_SECTION_ID
        / "baseline"
        / "validation.json"
    ).write_text("{ not json", encoding="utf-8")

    code, payload = run("status", chat_id="chat-migrate")
    assert code == cli.EXIT_ERROR
    assert "迁移中止" in payload["error"]
    # The manifest is left at the version it was found at.
    assert workspace.manifest()["schema_version"] == 2


def test_a_legacy_verdict_that_is_not_an_object_aborts_the_migration(
    workspace: Workspace, run
) -> None:
    _build_legacy_session(workspace, schema_version=2)
    (
        workspace.product_root
        / "sections"
        / LEGACY_SECTION_ID
        / "baseline"
        / "validation.json"
    ).write_text("[1, 2]", encoding="utf-8")

    code, payload = run("status", chat_id="chat-migrate")
    assert code == cli.EXIT_ERROR
    assert "迁移中止" in payload["error"]
    assert workspace.manifest()["schema_version"] == 2


def test_a_corrupt_manifest_is_reported_not_crashed(workspace: Workspace, run) -> None:
    workspace.product_root.mkdir(parents=True)
    (workspace.product_root / cli.MANIFEST_FILE).write_text(
        "{ broken", encoding="utf-8"
    )
    code, payload = run("status", chat_id="chat-x")
    assert code == cli.EXIT_ERROR
    assert cli.MANIFEST_FILE in payload["error"]


def test_a_manifest_that_is_not_an_object_is_reported(
    workspace: Workspace, run
) -> None:
    workspace.product_root.mkdir(parents=True)
    (workspace.product_root / cli.MANIFEST_FILE).write_text("[]", encoding="utf-8")
    code, payload = run("status", chat_id="chat-x")
    assert code == cli.EXIT_ERROR
    assert "JSON 对象" in payload["error"]


def test_a_corrupt_turn_file_does_not_break_the_session(
    workspace: Workspace, run
) -> None:
    first, _ = parse(workspace, run)
    turn = workspace.product_root / "turns" / "chat-s1.json"
    turn.parent.mkdir(parents=True, exist_ok=True)
    turn.write_text("{ broken", encoding="utf-8")

    code, payload = run(
        "update-section",
        "--section-id",
        first,
        "--content",
        FIRST_DRAFT,
        chat_id="chat-s1",
    )
    assert code == cli.EXIT_OK, payload
    assert json.loads(turn.read_text(encoding="utf-8"))["section_id"] == first


# --- scheduled reproduction ---------------------------------------------------


FORMAT_SPEC = {
    "source_css": "body { color: #111; }",
    "source": {"path": "source/original.md", "evidence": {"sample": "1234.5"}},
    "dom_contract": {"headings": True},
}


def _cron(workspace: Workspace, capsys, *argv: str, chat_id: str = "chat-cron"):
    try:
        returned = cli.main(
            [
                "--artifact-dir",
                str(workspace.artifact_dir),
                "--chat-id",
                chat_id,
                *argv,
            ]
        )
        code = cli.EXIT_OK if returned is None else int(returned)
    except SystemExit as excitation:
        code = int(excitation.code or 0)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def _prepare_export(workspace: Workspace, run) -> list[str]:
    sections = _freeze_baseline_with(workspace, run, second_query=True)
    first, second = sections
    (workspace.product_root / "sections" / first / "compute").mkdir()
    (workspace.product_root / "sections" / first / "compute" / "adjust.py").write_text(
        "def adjust(rows):\n    return rows\n", encoding="utf-8"
    )
    code, payload = run("freeze-recipe", chat_id="chat-refreeze")
    assert code == cli.EXIT_OK, payload
    recipe = json.loads(
        (workspace.product_root / "recipe.json").read_text(encoding="utf-8")
    )
    assert recipe["computes"][0]["path"].endswith("compute/adjust.py")
    draft = workspace.product_root / "sections" / first / "baseline" / "draft.md"
    draft.write_text(
        FIRST_DRAFT
        + "\n## 复现差异与数据缺口说明\n\n- 【数仓缺口】缺少退款标记。本期沿用全量，不得自行补数。\n"
        + "\n## 1.1 补充\n\n1. 列表仍在\n\n统计周期 2026年8月\n",
        encoding="utf-8",
    )
    (workspace.product_root / "sections" / first / "spec.json").write_text(
        json.dumps(
            {
                "metric_definitions": [{"text": "营业收入", "provenance": "重算"}],
                "required_data": ["orders"],
                "success_criteria": ["收入已给出"],
                "markdown_outline": ["总体"],
                "context": "原文摘录 1234.5",
                "inferred_questions": ["收入是多少"],
                "provenance_basis": "旧字段",
                "run_guards": [
                    {
                        "id": "S1.G1",
                        "rule": "不得用上月分区冒充本月",
                        "why": "分区错会静默通过",
                        "provenance": "用户确认",
                    }
                ],
                "datasource": "经营数仓",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (workspace.product_root / "sections" / second / "spec.json").write_text(
        json.dumps(
            {
                "metric_definitions": [
                    {"text": "渠道收入含税", "provenance": "用户确认"}
                ],
                "required_data": ["orders"],
                "success_criteria": [{"id": "S2.1", "text": "渠道已拆开"}],
                "markdown_outline": ["渠道"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (workspace.product_root / "format_spec.json").write_text(
        json.dumps(FORMAT_SPEC, ensure_ascii=False), encoding="utf-8"
    )
    return sections


def test_export_reference_keeps_only_the_next_run(workspace: Workspace, run) -> None:
    sections = _prepare_export(workspace, run)
    out = workspace.artifact_dir / "report-reproduction-export"
    code, payload = run(
        "export-reference",
        "--note",
        "首版定时参考",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["command"] == "export-reference"
    assert payload["result"]["baseline_period"] == "202608"
    reference = out / "reference"
    names = sorted(
        path.relative_to(reference).as_posix()
        for path in reference.rglob("*")
        if path.is_file()
    )
    assert names == sorted(
        [
            "REFERENCE.json",
            "recipe.json",
            "format_spec.json",
            f"sections/{sections[0]}/spec.json",
            f"sections/{sections[0]}/exemplar.md",
            f"sections/{sections[0]}/queries/revenue.sql",
            f"sections/{sections[0]}/compute/adjust.py",
            f"sections/{sections[1]}/spec.json",
            f"sections/{sections[1]}/exemplar.md",
            f"sections/{sections[1]}/queries/channel.sql",
            f"sections/{sections[1]}/queries/revenue.sql",
        ]
    )
    slim = json.loads(
        (reference / "sections" / sections[0] / "spec.json").read_text(encoding="utf-8")
    )
    assert "context" not in slim and "inferred_questions" not in slim
    assert "provenance_basis" not in slim
    assert slim["success_criteria"] == [{"id": "S1.1", "text": "收入已给出"}]
    assert slim["run_guards"][0]["id"] == "S1.G1"
    exported_format = json.loads(
        (reference / "format_spec.json").read_text(encoding="utf-8")
    )
    assert exported_format["source_css"]
    assert "source" not in exported_format
    recipe = json.loads((reference / "recipe.json").read_text(encoding="utf-8"))
    assert "status" not in json.dumps(recipe["sections"])
    assert recipe["datasource"] == "经营数仓"
    assert recipe["probe_tables"] == ["orders"]
    assert recipe["schedule"]["period"]["unit"] == "month"
    exemplar = (reference / "sections" / sections[0] / "exemplar.md").read_text(
        encoding="utf-8"
    )
    for number in ("1234.5", "12.3", "8900"):
        assert number not in exemplar
    assert "〔数〕" in exemplar
    assert "1.1" in exemplar
    assert "1. 列表仍在" in exemplar
    assert "2026年8月" in exemplar
    document = json.loads((reference / "REFERENCE.json").read_text(encoding="utf-8"))
    assert document["contract_version"] == 1
    assert document["note"] == "首版定时参考"
    assert "contract_version" not in document["prompt"]
    assert "定时执行指令" in document["prompt"]
    assert (
        "严格按既有管线执行，不要重新设计口径，不要询问，一路执行到报告产出。直接使用脚本，非必要无需安装依赖。"
        in document["prompt"]
    )
    assert "## 4. 必守规则" in document["prompt"]
    assert "## 5. 已知缺口" in document["prompt"]
    assert "1234.5" not in document["prompt"]
    assert (
        document["recipe_hash"]
        == hashlib.sha256((reference / "recipe.json").read_bytes()).hexdigest()
    )
    for relative, digest in document["files"].items():
        assert hashlib.md5((reference / relative).read_bytes()).hexdigest() == digest
    seed_files = sorted(
        path.relative_to(out / "seed").as_posix()
        for path in (out / "seed").rglob("*")
        if path.is_file()
    )
    assert seed_files == sorted(
        [
            "STATE.json",
            f"sections/{sections[0]}/runs/202608/data/revenue.csv",
            f"sections/{sections[1]}/runs/202608/data/revenue.csv",
        ]
    )
    state = json.loads((out / "seed" / "STATE.json").read_text(encoding="utf-8"))
    assert state["last_period"] == "202608"
    assert state["history"] == [{"period": "202608", "status": "delivered"}]
    prompt = document["prompt"]
    assert "publish_reference" not in prompt
    assert f"report-reproduction-cron/reference/{document['recipe_hash']}" in prompt
    assert "install-reference" in prompt
    assert "--artifact-dir" in prompt and "--chat-id" in prompt
    result = payload["result"]
    assert result["cron_job"] == {
        "session_policy": "reuse",
        "attachment_filenames": [result["bundle_path"]],
        "channel": "console",
    }
    bundle = workspace.root / result["bundle_path"]
    assert bundle.name == f"rr-bundle-{document['recipe_hash'][:12]}.zip"
    assert bundle.stat().st_size <= 20 * 1024 * 1024
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
    assert "reference/REFERENCE.json" in names
    assert "reference/recipe.json" in names
    assert "seed/STATE.json" in names
    assert "cron_message.md" not in names
    assert (out / "cron_message.md").read_text(encoding="utf-8") == prompt


def test_prompt_omits_empty_rule_and_gap_sections() -> None:
    from rr.export import render_prompt
    from rr.models import RunRecipe

    recipe = RunRecipe.model_validate(
        {
            "report_id": "r",
            "title": "空规则报告",
            "sections": [{"section_id": "s", "title": "总览", "blocked_by": []}],
            "schedule": {
                "period": {
                    "unit": "month",
                    "offset": -1,
                    "window_size": 1,
                    "label_format": "%Y%m",
                },
                "parameters": {},
            },
            "datasource": "",
            "probe_tables": [],
        }
    )
    prompt = render_prompt(recipe, {"s": {}}, {"s": "没有缺口块\n"})
    assert "## 4." not in prompt
    assert "## 5." not in prompt
    assert "沿用当前会话数据源" in prompt
    assert prompt.index("## 3. 章节") < prompt.index("## 6. 最终回复")


def test_export_refuses_an_empty_stylesheet(workspace: Workspace, run) -> None:
    _prepare_export(workspace, run)
    (workspace.product_root / "format_spec.json").write_text(
        json.dumps({"source_css": "  "}), encoding="utf-8"
    )
    code, payload = run(
        "export-reference",
        "--out",
        str(workspace.artifact_dir / "report-reproduction-export"),
        "--note",
        "缺样式",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_ERROR
    assert "source_css" in payload["error"]


def _install_cron(workspace: Workspace, run) -> tuple[Path, Path, list[str]]:
    """Export a bundle, stage it as a session upload, and install it."""
    sections = _prepare_export(workspace, run)
    out = workspace.artifact_dir / "report-reproduction-export"
    code, payload = run(
        "export-reference",
        "--out",
        str(out),
        "--note",
        "首版定时参考",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_OK, payload
    bundle = workspace.root / payload["bundle_path"]
    uploads = workspace.root / "uploads" / workspace.artifact_dir.name
    uploads.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle, uploads / bundle.name)
    code, installed = run(
        "install-reference",
        "--recipe-hash",
        payload["recipe_hash"],
        "--work-dir",
        "report-reproduction-cron",
        chat_id="chat-cron",
    )
    assert code == cli.EXIT_OK, installed
    reference = workspace.artifact_dir / installed["reference_dir"]
    work = workspace.artifact_dir / installed["work_dir"]
    return reference, work, sections


def test_export_reference_rejects_an_oversized_bundle(
    workspace: Workspace, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rr import export

    _prepare_export(workspace, run)
    monkeypatch.setattr(export, "BUNDLE_LIMIT_BYTES", 1)
    out = workspace.artifact_dir / "report-reproduction-export"
    code, payload = run(
        "export-reference",
        "--out",
        str(out),
        "--note",
        "太大",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_ERROR
    assert "20MB" in payload["error"]
    assert list(out.glob("rr-bundle-*.zip")) == []


def test_export_reference_refuses_an_output_outside_the_artifact_dir(
    workspace: Workspace, run
) -> None:
    _prepare_export(workspace, run)
    code, payload = run(
        "export-reference",
        "--out",
        str(workspace.root / "exported"),
        "--note",
        "越界",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_ERROR
    assert "产物目录" in payload["error"]


def test_install_reference_selects_the_bundle_by_hash(
    workspace: Workspace, run
) -> None:
    _prepare_export(workspace, run)
    out = workspace.artifact_dir / "report-reproduction-export"
    code, payload = run(
        "export-reference",
        "--out",
        str(out),
        "--note",
        "首版定时参考",
        "--baseline-period",
        "202608",
        chat_id="chat-export",
    )
    assert code == cli.EXIT_OK, payload
    recipe_hash = payload["recipe_hash"]
    bundle = workspace.root / payload["bundle_path"]
    uploads = workspace.root / "uploads" / workspace.artifact_dir.name
    uploads.mkdir(parents=True)
    renamed = uploads / "cron-deadbeef-1.zip"
    shutil.copy2(bundle, renamed)
    decoy = uploads / "rr-bundle-000000000000.zip"
    with zipfile.ZipFile(decoy, "w") as archive:
        archive.writestr(
            "reference/REFERENCE.json",
            json.dumps({"recipe_hash": "0" * 64}),
        )
    code, installed = run(
        "install-reference",
        "--recipe-hash",
        recipe_hash,
        "--work-dir",
        "report-reproduction-cron",
        chat_id="chat-cron",
    )
    assert code == cli.EXIT_OK, installed
    assert installed["bundle"] == renamed.name
    assert installed["seeded"] is True
    assert (workspace.artifact_dir / installed["reference_dir"]).is_dir()


def test_install_reference_rejects_zip_slip(workspace: Workspace, run) -> None:
    bundle = workspace.artifact_dir / "evil.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            "reference/REFERENCE.json",
            json.dumps({"recipe_hash": "a" * 64, "contract_version": 1, "files": {}}),
        )
        archive.writestr("reference/../../outside.txt", "nope")
    code, payload = run(
        "install-reference",
        "--recipe-hash",
        "a" * 64,
        "--work-dir",
        "report-reproduction-cron",
        "--bundle",
        bundle.name,
        chat_id="chat-cron",
    )
    assert code == cli.EXIT_ERROR
    assert "越界" in payload["error"]
    assert not (workspace.artifact_dir.parent / "outside.txt").exists()
    assert not (workspace.root / "outside.txt").exists()


def test_install_reference_does_not_overwrite_an_advanced_state(
    workspace: Workspace, run
) -> None:
    reference, work, _sections = _install_cron(workspace, run)
    state_path = work / "STATE.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_period"] = "202609"
    state["history"].append({"period": "202609", "status": "delivered"})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    recipe_hash = json.loads(
        (reference / "REFERENCE.json").read_text(encoding="utf-8")
    )["recipe_hash"]
    code, installed = run(
        "install-reference",
        "--recipe-hash",
        recipe_hash,
        "--work-dir",
        "report-reproduction-cron",
        chat_id="chat-cron",
    )
    assert code == cli.EXIT_OK, installed
    assert installed["seeded"] is False
    kept = json.loads(state_path.read_text(encoding="utf-8"))
    assert kept["last_period"] == "202609"
    assert kept["history"][-1]["period"] == "202609"


def test_relative_cron_paths_resolve_under_the_artifact_dir(
    workspace: Workspace, run, capsys: pytest.CaptureFixture[str]
) -> None:
    reference, work, _sections = _install_cron(workspace, run)
    assert work == workspace.artifact_dir / "report-reproduction-cron"
    recipe_hash = json.loads(
        (reference / "REFERENCE.json").read_text(encoding="utf-8")
    )["recipe_hash"]
    assert reference == work / "reference" / recipe_hash
    code, payload = _cron(
        workspace,
        capsys,
        "--reference-dir",
        f"report-reproduction-cron/reference/{recipe_hash}",
        "--work-dir",
        "report-reproduction-cron",
        "plan-run",
        "--run-date",
        "2026-10-15",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["status"] == "OK"
    assert payload["period"] == "202609"


def test_cron_mode_is_read_only_and_runs_a_period(
    workspace: Workspace, run, capsys: pytest.CaptureFixture[str]
) -> None:
    reference, work, sections = _install_cron(workspace, run)
    before = {
        path.relative_to(reference).as_posix(): hashlib.md5(
            path.read_bytes()
        ).hexdigest()
        for path in reference.rglob("*")
        if path.is_file()
    }
    for path in reference.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    flags = ["--reference-dir", str(reference), "--work-dir", str(work)]
    try:
        code, payload = _cron(workspace, capsys, *flags, "parse", "--file", "x")
        assert code == cli.EXIT_ERROR
        assert "定时模式" in payload["error"]

        code, payload = _cron(
            workspace, capsys, *flags, "plan-run", "--run-date", "2026-10-15"
        )
        assert code == cli.EXIT_OK, payload
        assert payload["status"] == "OK"
        assert payload["period"] == "202609"
        assert payload["prev_period"] == "202608"
        assert payload["bindings"]["start_date"] == "20260901"
        assert payload["bindings"]["end_date"] == "20260930"
        assert payload["bindings"]["channel_month"] == "202609"

        code, payload = _cron(
            workspace, capsys, *flags, "prepare-run", "--period", "202609"
        )
        assert code == cli.EXIT_OK, payload
        rendered = (
            work
            / "sections"
            / sections[0]
            / "runs"
            / "202609"
            / "queries"
            / "revenue.sql"
        ).read_text(encoding="utf-8")
        assert "${" not in rendered
        assert "20260901" in rendered and "20260930" in rendered
        channel = (
            work
            / "sections"
            / sections[1]
            / "runs"
            / "202609"
            / "queries"
            / "channel.sql"
        ).read_text(encoding="utf-8")
        assert "{{" not in channel and "202609" in channel

        drafts = (
            "## 一、总体概览\n\n本月营业收入 1300.0 万元。口径来源：重算。\n",
            "## 二、分渠道拆解\n\n线上 900 万元。口径来源：用户确认。\n",
        )
        for section_id, draft, check in (
            (sections[0], drafts[0], "S1.1"),
            (sections[1], drafts[1], "S2.1"),
        ):
            (work / "sections" / section_id / "runs" / "202609" / "data").mkdir(
                parents=True, exist_ok=True
            )
            (
                work / "sections" / section_id / "runs" / "202609" / "data" / "rows.csv"
            ).write_text("value\n1\n", encoding="utf-8")
            code, payload = _cron(
                workspace,
                capsys,
                *flags,
                "update-section",
                "--section-id",
                section_id,
                "--run-period",
                "202609",
                "--content",
                draft,
            )
            assert code == cli.EXIT_OK, payload
            code, payload = _cron(
                workspace,
                capsys,
                *flags,
                "check-run",
                "--section-id",
                section_id,
                "--period",
                "202609",
                "--business-check",
                json.dumps({"name": check, "passed": True, "details": "已核对"}),
            )
            assert code == cli.EXIT_OK, payload
            names = [item["name"] for item in payload["checks"]]
            assert "criteria_covered" in names
            assert "no_mask_left" in names
            assert "rendered_sql_present" in names
            assert "口径来源披露" in names
            assert payload["passed"] is True

        (work / "runs" / "202609" / "report.html").write_text(
            "<html>ok</html>", encoding="utf-8"
        )
        code, payload = _cron(
            workspace, capsys, *flags, "finish-run", "--period", "202609"
        )
        assert code == cli.EXIT_OK, payload
        assert (workspace.artifact_dir / "report.html").read_text(encoding="utf-8")
        assert (work / "archive" / "202609" / "report.html").is_file()
        state = json.loads((work / "STATE.json").read_text(encoding="utf-8"))
        assert state["last_period"] == "202609"
        assert state["history"][-1] == {"period": "202609", "status": "delivered"}
        code, payload = _cron(workspace, capsys, *flags, "status", "--period", "202609")
        assert payload["mode"] == "cron"
        assert payload["delivered"] is True
        assert payload["sections"]

        code, payload = _cron(
            workspace, capsys, *flags, "plan-run", "--run-date", "2026-11-15"
        )
        assert payload["status"] == "OK", payload
        assert payload["period"] == "202610"
        assert payload["prev_period"] == "202609"
        code, again = _cron(
            workspace, capsys, *flags, "plan-run", "--run-date", "2026-10-15"
        )
        assert again["status"] == "STOP"
        assert again["reason"] == "already_delivered"
        assert "已交付" in again["message"]
    finally:
        for path in reference.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
    after = {
        path.relative_to(reference).as_posix(): hashlib.md5(
            path.read_bytes()
        ).hexdigest()
        for path in reference.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_plan_run_stops_on_a_gap_or_a_future_state(
    workspace: Workspace, run, capsys: pytest.CaptureFixture[str]
) -> None:
    reference, work, _sections = _install_cron(workspace, run)
    flags = ["--reference-dir", str(reference), "--work-dir", str(work)]
    code, payload = _cron(
        workspace, capsys, *flags, "plan-run", "--run-date", "2026-11-15"
    )
    assert code == cli.EXIT_OK, payload
    assert payload["status"] == "STOP"
    assert payload["reason"] == "gap"
    assert "缺口" in payload["message"]

    state_path = work / "STATE.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_period"] = "202612"
    state["history"] = [{"period": "202612", "status": "delivered"}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, payload = _cron(
        workspace, capsys, *flags, "plan-run", "--run-date", "2026-10-15"
    )
    assert payload["status"] == "STOP"
    assert payload["reason"] == "state_ahead"
    assert "晚于" in payload["message"]


def test_prepare_run_rejects_a_tampered_reference(
    workspace: Workspace, run, capsys: pytest.CaptureFixture[str]
) -> None:
    reference, work, sections = _install_cron(workspace, run)
    sql = reference / "sections" / sections[0] / "queries" / "revenue.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8"
    )
    code, payload = _cron(
        workspace,
        capsys,
        "--reference-dir",
        str(reference),
        "--work-dir",
        str(work),
        "prepare-run",
        "--period",
        "202609",
    )
    assert code == cli.EXIT_ERROR
    assert "校验失败" in payload["error"]


def test_check_run_rejects_a_missing_criterion_and_a_mask(
    workspace: Workspace, run, capsys: pytest.CaptureFixture[str]
) -> None:
    reference, work, sections = _install_cron(workspace, run)
    flags = ["--reference-dir", str(reference), "--work-dir", str(work)]
    assert (
        _cron(workspace, capsys, *flags, "prepare-run", "--period", "202609")[0]
        == cli.EXIT_OK
    )
    (work / "sections" / sections[0] / "runs" / "202609" / "data").mkdir(
        parents=True, exist_ok=True
    )
    (
        work / "sections" / sections[0] / "runs" / "202609" / "data" / "rows.csv"
    ).write_text("v\n1\n", encoding="utf-8")
    code, payload = _cron(
        workspace,
        capsys,
        *flags,
        "update-section",
        "--section-id",
        sections[0],
        "--run-period",
        "202609",
        "--content",
        "收入 〔数〕 万元，未写口径。\n",
    )
    assert code == cli.EXIT_OK, payload
    code, payload = _cron(
        workspace,
        capsys,
        *flags,
        "check-run",
        "--section-id",
        sections[0],
        "--period",
        "202609",
    )
    assert code == cli.EXIT_OK
    by_name = {item["name"]: item for item in payload["checks"]}
    assert by_name["criteria_covered"]["passed"] is False
    assert by_name["no_mask_left"]["passed"] is False
    assert by_name["口径来源披露"]["passed"] is False
    assert payload["passed"] is False


def test_plan_run_renders_week_slots_from_the_earliest(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = workspace.artifact_dir / "week-ref"
    work = workspace.artifact_dir / "week-work"
    reference.mkdir()
    recipe = {
        "report_id": "weekly",
        "title": "周报",
        "sections": [{"section_id": "s", "title": "总览", "blocked_by": []}],
        "parameters": [],
        "queries": [],
        "computes": [],
        "schedule": {
            "period": {
                "unit": "week",
                "offset": -1,
                "window_size": 2,
                "label_format": "%G-W%V",
            },
            "parameters": {
                "w1_start": "${window_week_start:0:%Y%m%d}",
                "w1_end": "${window_week_end:0:%Y%m%d}",
                "w2_start": "${window_week_start:1:%Y%m%d}",
                "w2_end": "${window_week_end:1:%Y%m%d}",
            },
        },
        "datasource": "",
        "probe_tables": [],
    }
    recipe_path = reference / "recipe.json"
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
    (reference / "format_spec.json").write_text("{}\n", encoding="utf-8")
    files = {
        "recipe.json": hashlib.md5(recipe_path.read_bytes()).hexdigest(),
        "format_spec.json": hashlib.md5(
            (reference / "format_spec.json").read_bytes()
        ).hexdigest(),
    }
    (reference / "REFERENCE.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "report_id": "weekly",
                "title": "周报",
                "note": "周",
                "recipe_hash": hashlib.sha256(recipe_path.read_bytes()).hexdigest(),
                "files": files,
                "prompt": "周报 定时执行指令",
            }
        ),
        encoding="utf-8",
    )
    work.mkdir()
    (work / "STATE.json").write_text(
        json.dumps(
            {
                "report_id": "weekly",
                "last_period": "2026-W44",
                "history": [{"period": "2026-W44", "status": "delivered"}],
            }
        ),
        encoding="utf-8",
    )
    code, payload = _cron(
        workspace,
        capsys,
        "--reference-dir",
        str(reference),
        "--work-dir",
        str(work),
        "plan-run",
        "--run-date",
        "2026-11-12",
    )
    assert code == cli.EXIT_OK, payload
    assert payload["status"] == "OK"
    assert payload["period"] == "2026-W45"
    assert payload["bindings"] == {
        "w1_start": "20261026",
        "w1_end": "20261101",
        "w2_start": "20261102",
        "w2_end": "20261108",
    }


def test_diff_run_reports_a_changed_metric(workspace: Workspace, run) -> None:
    base = workspace.root / "base-work"
    candidate = workspace.root / "candidate-work"
    for root, number in ((base, "100"), (candidate, "120")):
        draft = root / "sections" / "s1" / "runs" / "202609" / "draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text(f"收入 {number} 万元，环比上升。\n", encoding="utf-8")
    code, payload = run(
        "diff-run",
        "--period",
        "202609",
        "--base-work-dir",
        str(base),
        "--candidate-work-dir",
        str(candidate),
    )
    assert code == cli.EXIT_OK, payload
    stored = json.loads((candidate / "diff.json").read_text(encoding="utf-8"))
    changed = stored["sections"][0]["changed_metrics"]
    assert changed
    assert changed[0]["reference"] == "100"
    assert changed[0]["candidate"] == "120"
    assert stored["sections"][0]["period_over_period_affected"] is True


def test_status_reports_baseline_release_and_cron_delivery(
    workspace: Workspace, run
) -> None:
    sections = approve_first_two(workspace, run)
    code, payload = run("status", chat_id="chat-audit")
    assert code == cli.EXIT_OK, payload
    assert payload["command"] == "status"
    assert payload["result"]["mode"] == "baseline"
    first = payload["sections"][0]
    assert first["section_id"] == sections[0]
    assert first["status"] == "approved"
    assert first["evidence_provenance"] == cli.PROVENANCE_RECOMPUTED
    assert first["released_by_user"] is False
    assert first["last_chat"] == "chat-s1"


def test_session_schemas_stay_compatible_with_the_frontend(
    workspace: Workspace, run
) -> None:
    sections = _freeze_baseline_with(workspace, run, second_query=False)
    manifest = workspace.manifest()
    assert {
        "schema_version",
        "revision",
        "report_id",
        "title",
        "source_path",
        "source_period",
        "target_period",
        "baseline_frozen_at",
        "created_at",
        "updated_at",
        "sections",
    } <= set(manifest)
    section = manifest["sections"][0]["section"]
    assert {
        "section_id",
        "title",
        "level",
        "source_path",
        "draft_path",
        "spec_path",
        "comparison_path",
        "blocked_by",
    } <= set(section)
    comparison = workspace.comparison_of(sections[0])
    assert {
        "passed",
        "passed_with_review",
        "numeric_coverage",
        "numeric_differences",
        "structure_differences",
        "criteria_analysis",
        "notes",
        "tolerance",
        "created_at",
        "review",
        "evidence_gate",
    } <= set(comparison)
    assert set(comparison["evidence_gate"]) >= {
        "passed",
        "provenance",
        "violations",
        "checked_at",
    }
    run("prepare-run", "--period", "202609", chat_id="chat-run")
    stored_run = json.loads(
        (workspace.product_root / "runs" / "202609" / "run_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert {
        "schema_version",
        "revision",
        "report_id",
        "period",
        "baseline_revision",
        "recipe_hash",
        "created_at",
        "updated_at",
        "sections",
    } <= set(stored_run)
    descriptor = json.loads(
        (workspace.product_root / "schedule_descriptor.json").read_text(
            encoding="utf-8"
        )
    )
    assert {
        "report_id",
        "title",
        "recipe_hash",
        "period",
        "parameters",
    } <= set(descriptor)
