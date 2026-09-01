# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.utils.workspace import list_session_files
from qwenpaw_data.host.core.algo.biztrace.workspace_index import (
    ArtifactProposal,
    ArtifactVerifier,
    infer_artifact_meta,
    is_displayable_artifact,
    resolve_artifact,
)


def _listing(*rel_paths: str) -> list[dict[str, Any]]:
    return [
        {
            "rel_path": rel_path,
            "size_bytes": 1,
            "mime_type": "text/plain",
            "modified_at": "2026-08-02T00:00:00+00:00",
        }
        for rel_path in rel_paths
    ]


def test_workspace_listing_skips_hidden_directories(tmp_path: Path) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "report.md").write_text("# 报告", encoding="utf-8")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "state.json").write_text("{}", encoding="utf-8")

    listed = list_session_files(tmp_path)

    assert [info["rel_path"] for info in listed] == ["analysis/report.md"]
    assert listed[0]["mime_type"] == "text/markdown"
    assert listed[0]["size_bytes"] > 0


def test_listing_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert list_session_files(tmp_path / "nope") == []


def test_artifact_metadata_is_inferred_from_the_path() -> None:
    assert infer_artifact_meta("query.sql", "sql/query.sql")[:2] == (
        "query_script",
        "supporting",
    )
    assert infer_artifact_meta("result.csv", "out/result.csv")[:2] == (
        "dataset",
        "final",
    )
    assert infer_artifact_meta("step1.csv", "steps/step1.csv")[:2] == (
        "dataset",
        "intermediate",
    )
    assert infer_artifact_meta("report.md", "out/report.md")[:2] == ("report", "final")
    assert infer_artifact_meta("kpi_dashboard.html", "out/kpi_dashboard.html")[:2] == (
        "dashboard",
        "final",
    )
    assert infer_artifact_meta("export.py", "scripts/export.py")[:2] == (
        "other",
        "supporting",
    )
    assert infer_artifact_meta("problem.json", "problem.json")[:2] == (
        "other",
        "supporting",
    )


def test_only_query_scripts_and_final_products_reach_a_card() -> None:
    assert is_displayable_artifact(
        kind="query_script", role="supporting", relative_path="sql/q.sql"
    )
    assert is_displayable_artifact(
        kind="dataset", role="final", relative_path="out/r.csv"
    )
    assert not is_displayable_artifact(
        kind="dataset", role="intermediate", relative_path="steps/s.csv"
    )
    assert not is_displayable_artifact(
        kind="other", role="supporting", relative_path="scripts/x.py"
    )
    assert not is_displayable_artifact(
        kind="report", role="final", relative_path=None
    )


def test_a_proposal_resolves_only_when_one_file_fits() -> None:
    files = {"report.md": "analysis/report.md", "data.csv": "analysis/data.csv"}

    assert resolve_artifact("report.md", files) == "report.md"
    assert resolve_artifact("`analysis/report.md`", files) == "report.md"
    assert resolve_artifact("「report.md」", files) == "report.md"
    assert resolve_artifact("summary.md", files) is None


def _label(name: str, description: str, kind: str = "", role: str = "") -> (
    ArtifactProposal
):
    return ArtifactProposal(
        name=name, description=description, kind=kind, role=role
    )


def test_verification_needs_both_the_write_and_the_listing() -> None:
    verifier = ArtifactVerifier(lambda: _listing("analysis/report.md"))

    artifacts = verifier.verify(
        [
            _label("report.md", "分析报告", "report", "final"),
            _label("data.csv", "结果数据", "dataset", "final"),
            _label("ghost.md", "并不存在", "report", "final"),
        ],
        files={"report.md": "analysis/report.md", "data.csv": "analysis/data.csv"},
    )

    assert artifacts is not None
    # data.csv was written but is gone from the workspace; ghost.md never was.
    assert [item.name for item in artifacts] == ["report.md"]
    assert artifacts[0].relative_path == "analysis/report.md"
    assert verifier.unverified == 2


def test_the_listing_supplies_the_path_even_when_the_tool_used_an_absolute_one() -> (
    None
):
    verifier = ArtifactVerifier(lambda: _listing("analysis/report.md"))

    artifacts = verifier.verify(
        [_label("report.md", "分析报告", "report", "final")],
        files={"report.md": "/tmp/run/report.md"},
    )

    assert artifacts is not None
    assert artifacts[0].relative_path == "analysis/report.md"


def test_a_broken_lister_drops_every_artifact() -> None:
    def lister() -> list[dict[str, Any]]:
        raise OSError("workspace is gone")

    verifier = ArtifactVerifier(lister)

    assert (
        verifier.verify(
            [_label("report.md", "分析报告", "report", "final")],
            files={"report.md": "analysis/report.md"},
        )
        is None
    )


def test_a_candidate_nobody_labelled_is_still_backfilled() -> None:
    verifier = ArtifactVerifier(lambda: _listing("out/report.md", "sql/query.sql"))

    artifacts = verifier.verify(
        [_label("report.md", "分析报告", "report", "final")],
        files={"report.md": "out/report.md", "query.sql": "sql/query.sql"},
    )

    assert artifacts is not None
    inferred, labelled = artifacts
    assert (labelled.name, labelled.description) == ("report.md", "分析报告")
    assert (inferred.name, inferred.kind, inferred.role) == (
        "query.sql",
        "query_script",
        "supporting",
    )
    assert inferred.description == "查询脚本"
    assert verifier.unverified == 0


def test_an_intermediate_dataset_is_kept_out_of_the_card() -> None:
    verifier = ArtifactVerifier(lambda: _listing("steps/step1.csv", "out/final.csv"))

    artifacts = verifier.verify(
        [
            _label("step1.csv", "中间结果", "dataset", "intermediate"),
            _label("final.csv", "结果数据", "dataset", "final"),
        ],
        files={"step1.csv": "steps/step1.csv", "final.csv": "out/final.csv"},
    )

    assert artifacts is not None
    assert [item.name for item in artifacts] == ["final.csv"]
    # Filtered for display, not unverified: the file is really there.
    assert verifier.unverified == 0


def test_a_nonsense_label_falls_back_to_what_the_path_says() -> None:
    verifier = ArtifactVerifier(lambda: _listing("out/result.csv"))

    artifacts = verifier.verify(
        [_label("result.csv", "结果数据", "spreadsheet", "definitive")],
        files={"result.csv": "out/result.csv"},
    )

    assert artifacts is not None
    assert (artifacts[0].kind, artifacts[0].role) == ("dataset", "final")
