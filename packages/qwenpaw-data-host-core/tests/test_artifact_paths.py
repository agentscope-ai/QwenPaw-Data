from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.artifact_paths import ArtifactPathContext
from qwenpaw_data.host.core.orchestration.state import (
    RuntimeStateManager,
    _validate_business_view_html,
)


def test_business_view_html_allows_chartless_shared_template(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.html"
    report_path.write_text(
        """<!DOCTYPE html>
<html><head><script src="echarts.min.js"></script></head>
<body><h1>Revenue table</h1><table><tr><td>42</td></tr></table></body></html>
""",
        encoding="utf-8",
    )

    _validate_business_view_html(report_path)


def test_artifact_path_context_normalizes_relative_and_docker_paths(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "artifacts" / "session-1"
    base_dir.mkdir(parents=True)
    context = ArtifactPathContext(
        host_artifact_dir=base_dir,
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )

    relative = context.resolve_ref("graph-1/./node-1/result.csv")
    docker = context.resolve_ref(
        "/workspace/artifacts/session-1/graph-1/node-1/result.csv",
    )

    assert relative.relative_path == "graph-1/node-1/result.csv"
    assert relative.host_path == base_dir / "graph-1/node-1/result.csv"
    assert docker == relative


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/tmp/result.csv",
        "/workspace/result.csv",
        "/workspace/artifacts/other/result.csv",
        "../result.csv",
        "graph/../../result.csv",
    ],
)
def test_artifact_path_context_rejects_out_of_scope_paths(
    tmp_path: Path,
    path: str,
) -> None:
    context = ArtifactPathContext(
        host_artifact_dir=tmp_path / "artifacts" / "session-1",
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )

    with pytest.raises(ValueError):
        context.resolve_ref(path)


def test_artifact_path_context_normalizes_local_absolute_path(
    tmp_path: Path,
) -> None:
    session_dir = (tmp_path / "custom" / "artifacts" / "session-1").resolve()
    context = ArtifactPathContext(
        host_artifact_dir=session_dir,
        model_artifact_dir=session_dir,
    )

    resolved = context.resolve_ref(session_dir / "graph" / "result.csv")

    assert resolved.relative_path == "graph/result.csv"
    assert resolved.host_path == session_dir / "graph" / "result.csv"


def test_artifact_path_context_normalizes_windows_absolute_path(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "artifacts" / "session-1"
    context = ArtifactPathContext(
        host_artifact_dir=session_dir,
        model_artifact_dir=r"C:\workspace\artifacts\session-1",
    )

    resolved = context.resolve_ref(
        r"C:\workspace\artifacts\session-1\graph\result.csv",
    )

    assert resolved.relative_path == "graph/result.csv"
    assert resolved.host_path == session_dir.resolve() / "graph" / "result.csv"


def test_artifact_path_context_rejects_host_absolute_path(tmp_path: Path) -> None:
    base_dir = tmp_path / "artifacts" / "session-1"
    context = ArtifactPathContext(
        host_artifact_dir=base_dir,
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )

    with pytest.raises(ValueError, match="absolute artifact path"):
        context.resolve_ref(str(base_dir / "result.csv"))


def test_artifact_path_context_rejects_symlink_escape(tmp_path: Path) -> None:
    base_dir = tmp_path / "artifacts" / "session-1"
    base_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base_dir / "escape").symlink_to(outside, target_is_directory=True)
    context = ArtifactPathContext(
        host_artifact_dir=base_dir,
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )

    with pytest.raises(ValueError, match="escapes"):
        context.resolve_ref("escape/result.csv")


@pytest.mark.asyncio
async def test_update_subtask_validates_files_transactionally(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "artifacts" / "session-1"
    output_dir = base_dir / "graph" / "node"
    output_dir.mkdir(parents=True)
    (output_dir / "empty.csv").write_bytes(b"")
    context = ArtifactPathContext(
        host_artifact_dir=base_dir,
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )
    runtime = RuntimeStateManager(artifact_path_context=context)
    await runtime.create_plan(
        name="test",
        description="test",
        expected_outcome="test",
        nodes=[
            {
                "node_id": "node",
                "name": "node",
                "description": "test",
                "expected_outcome": "test",
            },
        ],
    )
    await runtime.update_subtask("node", "in_progress")
    notify = AsyncMock()
    runtime._notify_graph_change = notify

    failed = await runtime.update_subtask(
        "node",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "empty.csv",
                "path": "graph/node/empty.csv",
                "mime_type": "text/csv",
            },
            {
                "name": "missing.csv",
                "path": "graph/node/missing.csv",
                "mime_type": "text/csv",
            },
        ],
    )

    node = runtime.get_current_in_progress_node()
    assert failed.state is ToolResultState.ERROR
    failed_text = "".join(
        getattr(block, "text", "") for block in failed.content
    )
    assert "graph/node/missing.csv" in failed_text
    assert str(base_dir) not in failed_text
    assert node is not None
    assert node.state == "in_progress"
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    directory = await runtime.update_subtask(
        "node",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "node",
                "path": "graph/node",
                "mime_type": "inode/directory",
            },
        ],
    )

    assert directory.state is ToolResultState.ERROR
    assert runtime.get_current_in_progress_node() is node
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    completed = await runtime.update_subtask(
        "node",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "empty.csv",
                "path": (
                    "/workspace/artifacts/session-1/graph/./node/empty.csv"
                ),
                "mime_type": "text/csv",
            },
        ],
    )

    assert completed.state is not ToolResultState.ERROR
    assert runtime._graph_nodes()[0].output.files[0].path == (
        "graph/node/empty.csv"
    )
    assert runtime.artifacts[0].path == "graph/node/empty.csv"
    assert runtime.artifacts[0].size_bytes == 0
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_subtask_rejects_static_images_in_html_artifacts(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "artifacts" / "session-1"
    output_dir = base_dir / "graph" / "report"
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.html"
    report_path.write_text(
        '<html><body><IMG src="chart.png" alt="chart"></body></html>',
        encoding="utf-8",
    )
    context = ArtifactPathContext(
        host_artifact_dir=base_dir,
        model_artifact_dir=Path("/workspace/artifacts/session-1"),
    )
    runtime = RuntimeStateManager(artifact_path_context=context)
    await runtime.create_plan(
        name="test",
        description="test",
        expected_outcome="test",
        nodes=[
            {
                "node_id": "report",
                "name": "report",
                "description": "test",
                "expected_outcome": "test",
            },
        ],
    )
    await runtime.update_subtask("report", "in_progress")
    notify = AsyncMock()
    runtime._notify_graph_change = notify

    rejected = await runtime.update_subtask(
        "report",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "report.html",
                "path": "graph/report/report.html",
                "mime_type": "text/html; charset=utf-8",
            },
        ],
    )

    assert rejected.state is ToolResultState.ERROR
    rejected_text = "".join(
        getattr(block, "text", "") for block in rejected.content
    )
    assert "must not contain <img> tags" in rejected_text
    assert "ECharts" in rejected_text
    node = runtime.get_current_in_progress_node()
    assert node is not None
    assert node.state == "in_progress"
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    report_path.write_text(
        """<!DOCTYPE html>
<html><head><script src="echarts.min.js"></script></head>
<body><h2>示例章节标题</h2><p>示例内容</p></body></html>
""",
        encoding="utf-8",
    )
    placeholder = await runtime.update_subtask(
        "report",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "report.html",
                "path": "graph/report/report.html",
                "mime_type": "text/html",
            },
        ],
    )

    assert placeholder.state is ToolResultState.ERROR
    placeholder_text = "".join(
        getattr(block, "text", "") for block in placeholder.content
    )
    assert "sample placeholder section" in placeholder_text
    assert node.state == "in_progress"
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    report_path.write_text(
        """<!DOCTYPE html>
<html><body><div id="chart"></div><div>{table_html}</div>
<script>
const chart = echarts.init(document.getElementById("chart"));
const option = {line.dump_options()};
chart.setOption(option);
</script></body></html>
""",
        encoding="utf-8",
    )
    unresolved = await runtime.update_subtask(
        "report",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "report.html",
                "path": "graph/report/report.html",
                "mime_type": "text/html",
            },
        ],
    )

    assert unresolved.state is ToolResultState.ERROR
    unresolved_text = "".join(
        getattr(block, "text", "") for block in unresolved.content
    )
    assert "unresolved report template placeholders" in unresolved_text
    assert "{table_html}" in unresolved_text
    assert node.state == "in_progress"
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    report_path.write_text(
        """<!DOCTYPE html>
<html><body><div id="chart"></div>
<script>window.addEventListener("resize", () => window.__echartsInstances);</script>
</body></html>
""",
        encoding="utf-8",
    )
    incomplete_chart = await runtime.update_subtask(
        "report",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "report.html",
                "path": "graph/report/report.html",
                "mime_type": "text/html",
            },
        ],
    )

    assert incomplete_chart.state is ToolResultState.ERROR
    incomplete_text = "".join(
        getattr(block, "text", "") for block in incomplete_chart.content
    )
    assert "echarts.init" in incomplete_text
    assert "setOption" in incomplete_text
    assert node.state == "in_progress"
    assert node.output is None
    assert runtime.artifacts == []
    notify.assert_not_awaited()

    report_path.write_text(
        """<!DOCTYPE html>
<html><body><div id="chart"></div>
<script>
const chart = echarts.init(document.getElementById("chart"));
chart.setOption({xAxis: {data: ["2026-03-01"]}, series: [{data: [42]}]});
</script></body></html>
""",
        encoding="utf-8",
    )
    completed = await runtime.update_subtask(
        "report",
        "done",
        reasoning="done",
        summary="done",
        files=[
            {
                "name": "report.html",
                "path": "graph/report/report.html",
                "mime_type": "text/html",
            },
        ],
    )

    assert completed.state is not ToolResultState.ERROR
    assert runtime._graph_nodes()[0].output.files[0].path == (
        "graph/report/report.html"
    )
    assert runtime.artifacts[0].path == "graph/report/report.html"
    notify.assert_awaited_once()
