"""Contract of the generic document comparison kernel and its command line.

Two things must hold for every consumer (a skill script, a cron job, or an ad
hoc command):

* the numeric pairing is one-to-one and each difference carries the exact span
  of the compared target token, so a highlighter marks the right number instead
  of guessing by string lookup;
* the command confines every path to ``--root``, writes the result atomically
  and stays free of the agent runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core.agent.tools.compare import (
    EXIT_ERROR,
    EXIT_OK,
    ComparisonResult,
    compare_documents,
    compare_numbers,
    compare_structure,
    extract_numbers,
    main,
)

MODULE = "qwenpaw_data.host.core.agent.tools.compare"

REFERENCE = (
    "# 概览\n\n收入 1,234.56 万元，同比 +12.50%。\n\n"
    "| 指标 | 本月 |\n| --- | --- |\n| 收入 | 1234.56 |\n"
)


# --- kernel: token extraction -------------------------------------------------


def test_iso_dates_are_not_tokenized() -> None:
    tokens = extract_numbers("W32: 2026-08-03 ~ 2026-08-09 · ds=20260812")
    assert [token.raw for token in tokens] == ["20260812"]


def test_negative_and_percent_numbers_still_parse() -> None:
    tokens = extract_numbers("偏差 -1,326.38，占比 73.30%，用户 1,000")
    assert [token.raw for token in tokens] == ["-1,326.38", "73.30%", "1,000"]
    # Thousands separators are dropped and a percent sign divides by 100.
    assert [token.value for token in tokens] == [-1326.38, 0.733, 1000.0]


def test_chinese_amounts_without_spaces_keep_exact_spans() -> None:
    target = "😀收入200万元，成本50元，占比12.5%。标识item_045与W32不参与比较"
    tokens = extract_numbers(target)
    assert [token.raw for token in tokens] == ["200", "50", "12.5%"]
    _, differences = compare_numbers(target.replace("200", "100"), target)
    assert [(row.reference, row.target) for row in differences] == [("100", "200")]
    assert target[differences[0].target_start : differences[0].target_end] == "200"


def test_appended_numeric_notes_do_not_shift_changed_table_rows() -> None:
    reference = (
        "收入100，成本100\n"
        + "\n".join(f"| 指标_{i:02d} | {100 + i} |" for i in range(1, 31))
        + "\n代码 100 100 100"
    )
    target = (
        "收入200，成本200\n"
        + "\n".join(f"| 指标_{i:02d} | {200 + i} |" for i in range(1, 31))
        + "\n代码 100 100 100\n缺口 1 450 2 3%"
    )
    _, differences = compare_numbers(reference, target)
    assert [(row.reference, row.target) for row in differences] == [
        ("100", "200"),
        ("100", "200"),
        *[(str(100 + i), str(200 + i)) for i in range(1, 31)],
    ]
    assert all(
        target[row.target_start : row.target_end] == row.target for row in differences
    )


# --- kernel: monotonic pairing and highlight spans ----------------------------


def test_difference_carries_the_span_of_the_compared_target_token() -> None:
    target = "收入 120 元 成本 50 元 备注 100"
    _, differences = compare_numbers("收入 100 元 成本 50 元", target)
    assert len(differences) == 1
    row = differences[0]
    assert (row.reference, row.target) == ("100", "120")
    assert row.target_start is not None and row.target_end is not None
    # The span must be the paired token, not the later repeated "100".
    assert target[row.target_start : row.target_end] == "120"


def test_pairing_is_one_to_one_even_with_repeated_target_values() -> None:
    target = "a 11 b 11 c 11"
    _, differences = compare_numbers("a 10 b 20", target)
    assert [(row.reference, row.target) for row in differences] == [
        ("10", "11"),
        ("20", "11"),
    ]
    spans = [(row.target_start, row.target_end) for row in differences]
    # Distinct, ordered, non-overlapping spans: each mark lands on its own token.
    assert spans == [(2, 4), (7, 9)]
    for start, end in spans:
        assert target[start:end] == "11"


def test_anchors_stop_drift_when_the_target_has_extra_numbers() -> None:
    reference = "总收入 100 万元 活跃 200 人"
    target = "口径说明 999 总收入 100 万元 活跃 210 人"
    coverage, differences = compare_numbers(reference, target)
    # "100" anchors, so the extra 999 cannot shift the rest of the document.
    assert [(row.reference, row.target) for row in differences] == [("200", "210")]
    assert coverage == 0.5
    assert target[differences[0].target_start : differences[0].target_end] == "210"


def test_missing_target_counterpart_has_no_span() -> None:
    _, differences = compare_numbers("收入 100 万元 活跃 200 人", "收入 100 万元")
    assert [(row.reference, row.target) for row in differences] == [("200", None)]
    assert differences[0].target_start is None
    assert differences[0].target_end is None
    assert differences[0].relative_error is None


def test_leftover_target_numbers_are_ignored() -> None:
    coverage, differences = compare_numbers("用户 1", "用户 1 备注 2")
    assert coverage == 1.0
    assert differences == []


def test_reference_without_numbers_yields_full_coverage() -> None:
    coverage, differences = compare_numbers("没有任何数字的参考文档", "目标 42")
    assert coverage == 1.0
    assert differences == []


# --- kernel: structure --------------------------------------------------------


def test_structure_gaps_are_worded_without_report_vocabulary() -> None:
    differences = compare_structure(REFERENCE, "收入 1200.00 万元")
    descriptions = [difference.description for difference in differences]
    kinds = [difference.kind for difference in differences]
    assert kinds == ["headings", "tables", "content"]
    assert "目标文档缺少 Markdown 标题结构" in descriptions
    assert "参考文档包含 1 个表格，目标文档仅包含 0 个" in descriptions
    assert "目标文档内容明显短于参考文档，可能遗漏结论或解释" in descriptions
    for description in descriptions:
        assert "复现稿" not in description
        assert "原 Section" not in description


def test_a_missing_table_is_a_warning_and_does_not_fail() -> None:
    result = compare_documents(REFERENCE, REFERENCE.replace("| --- | --- |\n", ""))
    assert [difference.kind for difference in result.structure_differences] == [
        "tables"
    ]
    assert result.structure_differences[0].severity == "warning"
    assert result.passed is True


def test_passed_requires_no_numeric_difference_and_no_error_gap() -> None:
    assert compare_documents(REFERENCE, REFERENCE).passed is True
    # One wrong number is enough to fail, even with an intact structure.
    assert (
        compare_documents(REFERENCE, REFERENCE.replace("1,234.56", "1200.00")).passed
        is False
    )
    # An error-severity gap fails even when every number matches.
    assert (
        compare_documents(REFERENCE, REFERENCE.replace("# 概览", "概览")).passed
        is False
    )


def test_tolerance_widens_the_numeric_verdict() -> None:
    reference = "# 毛利率\n\n毛利率 27.90%，环比提升 1.20 个百分点，处于合理区间内。"
    target = "# 毛利率\n\n毛利率 28.10%，环比提升 1.20 个百分点，处于合理区间内。"
    # 0.72% relative error: outside the default tolerance, inside a 1% one.
    assert compare_documents(reference, target).passed is False
    assert compare_documents(reference, target, tolerance=0.01).passed is True


def test_reviewer_fields_are_left_for_their_owner_to_write() -> None:
    result = compare_documents(REFERENCE, REFERENCE.replace("1,234.56", "1200.00"))
    assert result.passed_with_review is False
    assert result.criteria_analysis == []
    assert result.notes != ""
    assert all(difference.note is None for difference in result.numeric_differences)


def test_result_records_schema_version_tolerance_and_timestamp() -> None:
    result = compare_documents(REFERENCE, REFERENCE, tolerance=0.01)
    assert result.schema_version == 1
    assert result.tolerance == 0.01
    assert result.created_at.endswith("+00:00")
    assert result.target_path is None and result.reference_path is None


def test_result_model_rejects_unknown_fields() -> None:
    # The generic result carries no report vocabulary such as `section_id`.
    with pytest.raises(ValidationError):
        ComparisonResult.model_validate({"passed": True, "section_id": "s1"})


# --- command line -------------------------------------------------------------


def _write(root: Path) -> None:
    (root / "source.md").write_text(REFERENCE, encoding="utf-8")
    (root / "draft.md").write_text(
        REFERENCE.replace("# 概览", "概览").replace("1,234.56 万元", "1200.00 万元"),
        encoding="utf-8",
    )


def test_command_writes_result_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path)
    code = main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "draft.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_OK

    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["passed"] is False
    assert summary["numeric_difference_count"] == 1
    assert summary["structure_difference_counts"]["error"] == 1
    assert summary["output"] == "out/comparison.json"

    stored = ComparisonResult.model_validate_json(
        (tmp_path / "out" / "comparison.json").read_text(encoding="utf-8")
    )
    # Recorded relative to the confinement root, so the file stays portable.
    assert stored.target_path == "draft.md"
    assert stored.reference_path == "source.md"
    assert stored.passed is False


def test_command_writes_atomically_without_temp_leftovers(tmp_path: Path) -> None:
    _write(tmp_path)
    main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "draft.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    leftovers = [
        path.name
        for path in (tmp_path / "out").iterdir()
        if path.name != "comparison.json"
    ]
    assert leftovers == []


def test_command_confines_absolute_paths_to_root(tmp_path: Path) -> None:
    _write(tmp_path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text(REFERENCE, encoding="utf-8")
    code = main(
        [
            "--root",
            str(tmp_path),
            "--target",
            str(outside),
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR
    assert not (tmp_path / "out").exists()


def test_command_confines_parent_directory_escapes(tmp_path: Path) -> None:
    root = tmp_path / "session"
    root.mkdir()
    _write(root)
    (tmp_path / "secret.md").write_text(REFERENCE, encoding="utf-8")
    code = main(
        [
            "--root",
            str(root),
            "--target",
            "../secret.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR
    assert not (root / "out").exists()


def test_command_rejects_a_missing_file(tmp_path: Path) -> None:
    _write(tmp_path)
    code = main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "nope.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR
    assert not (tmp_path / "out").exists()


def test_command_rejects_a_directory_as_document(tmp_path: Path) -> None:
    _write(tmp_path)
    code = main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "out",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR


def test_command_rejects_a_non_utf8_document(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    code = main(
        [
            "--root",
            str(tmp_path),
            "--target",
            "binary.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR
    assert not (tmp_path / "out").exists()


def test_command_rejects_a_tolerance_outside_zero_to_one(tmp_path: Path) -> None:
    _write(tmp_path)
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--target",
                "draft.md",
                "--reference",
                "source.md",
                "--output",
                "out/comparison.json",
                "--tolerance",
                "2",
            ]
        )
        == EXIT_ERROR
    )
    assert not (tmp_path / "out").exists()


def test_command_rejects_a_root_that_is_not_a_directory(tmp_path: Path) -> None:
    _write(tmp_path)
    code = main(
        [
            "--root",
            str(tmp_path / "missing"),
            "--target",
            "draft.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_ERROR


def test_command_resolves_relative_paths_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "--target",
            "draft.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ]
    )
    assert code == EXIT_OK
    stored = json.loads(
        (tmp_path / "out" / "comparison.json").read_text(encoding="utf-8")
    )
    # Without a confinement root the compared paths are recorded as resolved.
    assert stored["target_path"] == str(tmp_path / "draft.md")


def test_command_help_is_available() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


# --- dependency boundary ------------------------------------------------------


def test_kernel_module_stays_free_of_agent_runtime_imports() -> None:
    # The kernel file itself must not import the agent runtime. Importing it
    # *through the package* now pulls agentscope on purpose -- ``agent`` and
    # ``agent.tools`` use eager ``__init__`` exports -- so the dependency
    # guarantee lives at the source level rather than the import path.
    import importlib.util

    spec = importlib.util.find_spec(MODULE)
    source = Path(spec.origin).read_text(encoding="utf-8")
    runtime_imports = [
        line
        for line in source.splitlines()
        if line.strip().startswith(("import agentscope", "from agentscope"))
    ]
    assert runtime_imports == []


def test_kernel_module_is_runnable_as_a_command(tmp_path: Path) -> None:
    _write(tmp_path)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--root",
            str(tmp_path),
            "--target",
            "draft.md",
            "--reference",
            "source.md",
            "--output",
            "out/comparison.json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout)["ok"] is True
    assert (tmp_path / "out" / "comparison.json").is_file()


def test_agent_tools_package_exports_the_local_tools() -> None:
    # ``agent.tools`` now uses eager top-level exports; the lazy ``__getattr__``
    # shell was dropped together with CompareTool.
    import qwenpaw_data.host.core.agent.tools as tools

    assert set(tools.__all__) == {"AskUserQuestionTool", "CronJobTool"}
    assert not hasattr(tools, "CompareTool")
    assert tools.AskUserQuestionTool is not None
    assert tools.CronJobTool is not None


def test_the_recommender_is_told_raw_exports_are_only_comparable_to_each_other() -> (
    None
):
    from qwenpaw_data.host.core.algo.compare.prompts import RECOMMEND_SYSTEM_PROMPT

    assert "data/raw/" in RECOMMEND_SYSTEM_PROMPT
    assert "execute_read" in RECOMMEND_SYSTEM_PROMPT
