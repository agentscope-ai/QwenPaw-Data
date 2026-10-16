"""Private Data tools for declaring progress and business deliverables."""

from typing import Literal

from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk


def build_task_experience_tools(context_getter):
    def runtime():
        # Resolve per call: an Agent/toolkit may survive multiple chat turns.
        value = (context_getter() or {}).get("analysis_experience")
        if value is None:
            raise RuntimeError("analysis experience is unavailable")
        return value

    async def report_analysis_progress(
        stage: Literal["read_data", "confirm_scope", "analyze", "publish_report"],
    ) -> ToolChunk:
        """Declare the current analysis stage when work actually changes.

        Use read_data for gathering evidence, confirm_scope for resolving the
        analytical definition, analyze for computations/reasoning, and
        publish_report only while preparing the chosen user deliverable.
        Intermediate file creation alone is not publication or completion.
        """
        await runtime().report_analysis_progress(stage)
        return ToolChunk(content=[TextBlock(text="Analysis stage recorded.")])

    async def publish_analysis_artifact(
        path: str,
        role: Literal["primary", "supporting", "diagnostic", "source"],
        kind: Literal["data/report", "data/chart", "data/dataset", "data/diagnostic"],
        visibility: Literal["chat", "app_only"] = "chat",
    ) -> ToolChunk:
        """Publish an existing session artifact as an intentional deliverable.

        Before the final answer, publish the finished report as primary and
        only useful charts as supporting. Use app_only for source datasets,
        drafts, logs and diagnostics. Files are otherwise retained privately;
        file names/extensions never imply publication. path is relative to the
        session artifacts directory. This verifies bytes; it cannot create files.
        """
        await runtime().publish_analysis_artifact(path, role, kind, visibility)
        return ToolChunk(content=[TextBlock(text="Verified artifact published.")])

    return [
        FunctionTool(report_analysis_progress),
        FunctionTool(publish_analysis_artifact),
    ]
