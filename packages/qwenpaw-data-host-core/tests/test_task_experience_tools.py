from types import SimpleNamespace
from unittest.mock import AsyncMock

from qwenpaw_data.host.core.agent.task_experience import build_task_experience_tools


async def test_experience_tools_resolve_current_runtime_per_call():
    first = SimpleNamespace(
        report_analysis_progress=AsyncMock(), publish_analysis_artifact=AsyncMock()
    )
    second = SimpleNamespace(
        report_analysis_progress=AsyncMock(), publish_analysis_artifact=AsyncMock()
    )
    context = {"analysis_experience": first}
    tools = {tool.name: tool for tool in build_task_experience_tools(lambda: context)}
    await tools["report_analysis_progress"](stage="analyze")
    first.report_analysis_progress.assert_awaited_once_with("analyze")
    context["analysis_experience"] = second
    await tools["publish_analysis_artifact"](
        path="march.html", role="primary", kind="data/report"
    )
    second.publish_analysis_artifact.assert_awaited_once_with(
        "march.html", "primary", "data/report", "chat"
    )
    first.publish_analysis_artifact.assert_not_awaited()
